"""Client for the legacy M365 Copilot Playwright worker (app/m365_worker/worker.py).

The worker speaks newline-delimited JSON on stdin/stdout. This is the Python
port of the Worker class from the old extension.js: one request in flight,
per-action timeouts, and cancel = kill the process (restarted lazily).
"""
import json
import subprocess
import threading
import uuid

from .. import paths
from ..mcp import child_env

TIMEOUTS = {"chat": 900, "login": 360, "status": 90, "logout": 60, "ping": 60, "sessions.list": 30, "shutdown": 10}


class M365Error(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class M365Worker:
    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()        # one request at a time
        self.cond = threading.Condition()
        self.replies = {}
        self.status = {"state": "stopped", "authenticated": False}

    # -- process -------------------------------------------------------------
    def _start(self):
        if self.proc and self.proc.poll() is None:
            return
        paths.M365_SESSIONS.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(
            [paths.PYTHON, "-u", str(paths.M365_WORKER)],
            cwd=str(paths.M365_WORKER.parent),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=child_env({"PYTHONUNBUFFERED": "1", "M365_SESSIONS_DIR": str(paths.M365_SESSIONS)}),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        proc = self.proc
        threading.Thread(target=self._read, args=(proc,), daemon=True).start()
        threading.Thread(target=lambda: [None for _ in proc.stderr], daemon=True).start()

    def _read(self, proc):
        for raw in proc.stdout:
            try:
                msg = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            with self.cond:
                self.replies[msg.get("id")] = msg
                self.cond.notify_all()
        with self.cond:
            self.cond.notify_all()

    def ask(self, action, payload=None):
        with self.lock:
            self._start()
            proc = self.proc
            req_id = str(uuid.uuid4())
            proc.stdin.write((json.dumps({"id": req_id, "action": action, "payload": payload or {}}) + "\n").encode("utf-8"))
            proc.stdin.flush()
            timeout = TIMEOUTS.get(action, 120)
            with self.cond:
                ok = self.cond.wait_for(lambda: req_id in self.replies or proc.poll() is not None, timeout)
                msg = self.replies.pop(req_id, None)
            if msg is None:
                if not ok:
                    self.kill()
                    raise M365Error("M365_TIMEOUT", f"M365 {action} timed out after {timeout}s")
                raise M365Error("M365_WORKER_EXITED", "M365 worker stopped (cancelled or crashed)")
            if not msg.get("ok"):
                err = msg.get("error") or {}
                raise M365Error(err.get("code", "M365_ERROR"), err.get("message", "M365 worker error"))
            return msg.get("result") or {}

    def kill(self):
        proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            try:
                if hasattr(subprocess, "CREATE_NO_WINDOW"):
                    subprocess.run(["taskkill", "/pid", str(proc.pid), "/T", "/F"], capture_output=True)
                else:
                    proc.kill()
            except OSError:
                pass

    # -- high level ----------------------------------------------------------
    def check(self):
        try:
            result = self.ask("status")
            self.status = {"state": result.get("state"), "authenticated": bool(result.get("authenticated"))}
        except Exception as exc:  # worker cannot start: missing playwright/chromium
            self.status = {"state": "error", "authenticated": False, "error": str(exc)}
        return self.status

    def login(self):
        result = self.ask("login", {"timeout_seconds": 300})
        self.status = {"state": "ready" if result.get("authenticated") else "setup_required",
                       "authenticated": bool(result.get("authenticated"))}
        return self.status

    def chat(self, prompt, files=(), session_id=None, headless=True, model=None):
        payload = {
            "model": "copilot-365-web",
            "messages": [{"role": "user", "content": prompt, "attachedFiles": [str(f) for f in files]}],
            "headless": bool(headless),
        }
        if model:
            payload["m365_model"] = model
        if session_id:
            payload["session"] = {"id": session_id}
            payload["create_session"] = True
        return str(self.ask("chat", payload).get("content") or "")

    def shutdown(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.ask("shutdown")
            except Exception:
                pass
        self.kill()


worker = M365Worker()
