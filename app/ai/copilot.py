"""GitHub Copilot CLI provider (`copilot -p ... --output-format json`).

Material is never inlined into the command line (Windows caps it at ~32k
chars): files live in the run folder and the prompt tells the agent which ones
to read. Shell and write tools are denied, so the agent can only read.
The CLI is launched as `node <pkg>/npm-loader.js` so the prompt never passes
through cmd.exe quoting.
"""
import json
import shutil
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from ..mcp import child_env
from . import stopflag

PACKAGE = "@github/copilot"
# One process slot per caller: the pipeline and the assistant chat each run one CLI process at a time,
# side by side, and stopping one never kills the other.
_slots_lock = threading.Lock()
_slots = {}
_observer = ContextVar("copilot_observer", default=None)


@contextmanager
def observe(callback):
    """Attach run metadata at the caller without changing other providers or worker threads."""
    token = _observer.set(callback)
    try:
        yield
    finally:
        _observer.reset(token)


class _Progress:
    """Allowlisted, public CLI events only; never persist opaque thinking or tool arguments/results.

    Schema: https://docs.github.com/en/copilot/how-tos/copilot-sdk/features/streaming-events
    Streaming snapshots replace the same block, so deltas and the final event are not duplicated.
    """
    def __init__(self, callback):
        self.callback = callback
        self.call = uuid.uuid4().hex
        self.blocks, self.sent, self.tools = {}, {}, {}
        self.seen = False

    def emit(self, kind, text, key=None):
        if not self.callback or not text:
            return
        try:
            self.callback({"id": self.call + ":" + str(key or uuid.uuid4().hex), "call_id": self.call,
                           "kind": kind, "text": str(text)[:16000], "time": time.time()})
        except Exception:
            pass  # Observability must not fail the model call.

    def line(self, line):
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            return
        if not isinstance(event, dict) or not isinstance(event.get("data"), dict):
            return
        kind, data = event.get("type"), event["data"]
        text, key = "", event.get("id")
        if kind in ("assistant.reasoning", "assistant.reasoning_delta"):
            key = "reasoning:" + str(data.get("reasoningId") or "current")
            content = data.get("deltaContent" if kind.endswith("_delta") else "content")
            if not isinstance(content, str):
                return
            text = (self.blocks.get(key, "") + content) if kind.endswith("_delta") else content
            self.blocks[key] = text[:16000]
            self.seen = True
            if kind.endswith("_delta") and time.monotonic() - self.sent.get(key, 0) < .25:
                return
            self.sent[key] = time.monotonic()
            kind = "reasoning"
        elif kind == "assistant.message":
            if isinstance(data.get("reasoningText"), str) and data["reasoningText"][:16000] not in self.blocks.values():
                self.emit("reasoning", data["reasoningText"], "message-reasoning:" + str(data.get("messageId") or key))
                self.seen = True
            if data.get("toolRequests") or data.get("tool_requests") or data.get("phase") == "thinking":
                text, kind = data.get("content"), "progress"
            # The final answer already has its artifact; do not duplicate it here.
        elif kind == "assistant.intent":
            text, kind = data.get("intent"), "progress"
        elif kind == "tool.execution_start":
            self.tools[data.get("toolCallId")] = str(data.get("toolName") or "tool")[:160]
            text = self.tools[data.get("toolCallId")]
        elif kind == "tool.execution_complete":
            text = self.tools.get(data.get("toolCallId"), "tool") + (" ✓" if data.get("success") else " ✗")
        elif kind == "tool.execution_progress":
            text = data.get("progressMessage")
        elif kind == "session.error":
            text = data.get("message")
        if isinstance(text, str) and text.strip():
            self.seen = True
            self.emit(kind, text, key)

    def finish(self):
        for key, text in self.blocks.items():
            self.emit("reasoning", text, key)
        if not self.seen:
            self.emit("unavailable", "This CLI/model did not emit public reasoning or progress events.")


_USAGE_FIELDS = {
    "input_tokens": "inputTokens",
    "output_tokens": "outputTokens",
    "cache_read_tokens": "cacheReadTokens",
    "cache_write_tokens": "cacheWriteTokens",
    "reasoning_tokens": "reasoningTokens",
}


def _count(value):
    try:
        return max(0, int(value)) if not isinstance(value, bool) else 0
    except (TypeError, ValueError, OverflowError):
        return 0


def _parse_output(text):
    """Extract the final text and per-model usage events from Copilot JSONL output."""
    messages = []
    final_messages = []
    usage = {key: 0 for key in _USAGE_FIELDS}
    usage["model_calls"] = 0
    saw_event = False
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        if event_type == "assistant.message":
            saw_event = True
            content = data.get("content")
            if isinstance(content, str) and content.strip():
                messages.append(content.strip())
                if not data.get("toolRequests") and not data.get("tool_requests"):
                    final_messages.append(content.strip())
        elif event_type == "assistant.usage":
            saw_event = True
            usage["model_calls"] += 1
            for key, source in _USAGE_FIELDS.items():
                usage[key] += _count(data.get(source))

    if not saw_event:
        return text.strip(), None
    return "\n\n".join(final_messages or messages).strip(), usage


def _notify_usage(on_usage, usage):
    if not on_usage or not usage or not usage.get("model_calls"):
        return
    try:
        on_usage(usage)
    except Exception:
        pass  # Accounting must never make an otherwise successful AI call fail.


def _slot(name):
    with _slots_lock:
        return _slots.setdefault(name, {"lock": threading.Lock(), "proc": None})


def _execute(args, cwd, timeout, cancellable, slot="pipeline", on_line=None):
    state = _slot(slot)
    with state["lock"]:
        if cancellable and stopflag.requested.is_set():
            raise CopilotError("Cancelled")
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                cwd=str(cwd), env=child_env({"NO_COLOR": "1"}),
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        state["proc"] = proc
        if cancellable and stopflag.requested.is_set():  # Stop landed between the check and the launch
            cancel(slot)
        timed_out = False
        try:
            if on_line is None:
                out, err = proc.communicate(timeout=timeout)
            else:
                out_parts, err_parts = [], []

                def drain_stdout():
                    for line in iter(proc.stdout.readline, b""):
                        out_parts.append(line)
                        on_line(line.decode("utf-8", "replace"))

                def drain_stderr():
                    err_parts.append(proc.stderr.read())

                readers = [threading.Thread(target=drain_stdout, daemon=True),
                           threading.Thread(target=drain_stderr, daemon=True)]
                for reader in readers:
                    reader.start()
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    cancel(slot)
                    proc.wait()
                    timed_out = True
                finally:
                    for reader in readers:
                        reader.join()
                    proc.stdout.close()
                    proc.stderr.close()
                out, err = b"".join(out_parts), b"".join(err_parts)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            timed_out = True
            out, err = exc.output or b"", exc.stderr or b""
        finally:
            state["proc"] = None
    return proc, out, err, timed_out


def _unsupported(detail, option):
    message = str(detail or "").lower()
    return option in message and any(
        text in message for text in ("unknown option", "unrecognized option", "unknown argument"))


def _unsupported_json_output(detail):
    return _unsupported(detail, "--output-format")


def _without(args, option):
    """`args` minus every `option VALUE` pair."""
    out, skip = [], False
    for arg in args:
        if skip:
            skip = False
        elif arg == option:
            skip = True
        else:
            out.append(arg)
    return out


class CopilotError(RuntimeError):
    pass


def _npm_root():
    npm = shutil.which("npm")
    if not npm:
        return None
    try:
        out = subprocess.run([npm, "root", "-g"], capture_output=True, text=True, timeout=30,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None
    except (OSError, subprocess.SubprocessError):
        return None


def command():
    """argv prefix that launches the CLI, or None when it is not installed.

    Prefers the npm package (node + npm-loader.js, no cmd.exe quoting); falls
    back to any `copilot` executable on PATH (winget / standalone installs).
    """
    node = shutil.which("node")
    root = _npm_root()
    if node and root:
        pkg = root / "@github" / "copilot"
        try:
            bin_rel = json.loads((pkg / "package.json").read_text(encoding="utf-8"))["bin"]["copilot"]
            if (pkg / bin_rel).exists():
                return [node, str(pkg / bin_rel)]
        except (OSError, ValueError, KeyError, TypeError):
            pass
    exe = shutil.which("copilot")
    return [exe] if exe else None


def version():
    cmd = command()
    if not cmd:
        return None
    try:
        out = subprocess.run([*cmd, "--version"], capture_output=True, text=True, timeout=60,
                             env=child_env(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return (out.stdout or out.stderr).strip().splitlines()[0] if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


def ask(prompt, cwd, model="", timeout=900, cancellable=False, effort=None, context=None, on_usage=None,
        extra_dirs=(), slot="pipeline", on_event=None):
    """Run one read-only prompt. `extra_dirs` are readable besides `cwd` (`--add-dir`); `slot` names the
    process slot (see _slots) so independent callers do not queue behind each other."""
    cmd = command()
    if not cmd:
        raise CopilotError("Copilot CLI is not installed. Open Setup.")
    progress = _Progress(on_event or _observer.get())
    args = [*cmd, "-p", prompt, "--output-format", "json", "--no-auto-update", "--stream", "on" if progress.callback else "off",
            "--allow-all-tools", "--deny-tool", "shell", "--deny-tool", "write",
            "--disable-builtin-mcps", "-C", str(cwd)]
    if not progress.callback:
        args.append("-s")
    for folder in dict.fromkeys(str(d) for d in extra_dirs or () if d):
        args += ["--add-dir", folder]
    if model:
        args += ["--model", model]
    if context:
        args += ["--context", context]
    if effort:
        args += ["--effort", effort]
    progress.emit("call.started", model or "Copilot CLI")
    for _ in range(3):  # an older CLI may lack an optional flag: drop it and try again
        on_line = progress.line if progress.callback and "--output-format" in args else None
        try:
            proc, out, err, timed_out = _execute(args, cwd, timeout, cancellable, slot, on_line=on_line)
        except Exception as exc:
            progress.finish()
            progress.emit("call.failed", str(exc)[:500])
            raise
        raw = (out or b"").decode("utf-8", "replace")
        text, usage = _parse_output(raw)
        detail = err.decode("utf-8", "replace").strip() or text or raw.strip()
        if timed_out or proc.returncode == 0:
            break
        option = next((o for o in ("--output-format", "--add-dir") if o in args and _unsupported(detail, o)), None)
        if not option:
            break
        args = _without(args, option)
        if option == "--output-format":
            args[args.index("--stream") + 1] = "off"
            args.append("-s")
    progress.finish()
    progress.emit("call.failed" if timed_out or proc.returncode or not text else "call.completed",
                  "Timed out" if timed_out else "Empty response" if not text and not proc.returncode else "CLI exited with code " + str(proc.returncode))
    _notify_usage(on_usage, usage)
    if timed_out:
        raise CopilotError(f"Copilot CLI timed out after {timeout}s")
    if proc.returncode != 0:
        detail = (err.decode("utf-8", "replace").strip() or text or raw.strip())[-1500:]
        if not detail:
            detail = "no output (cancelled?)"
        if any(k in detail.lower() for k in ("/login", "authenticat", "not logged", "unauthorized")):
            raise CopilotError("Copilot CLI is not signed in (Setup → Copilot sign-in). " + detail)
        raise CopilotError(f"Copilot CLI failed (exit {proc.returncode}): {detail}")
    if not text:
        raise CopilotError("Copilot CLI returned an empty response")
    return text


def cancel(slot="pipeline"):
    proc = _slot(slot).get("proc")
    if proc and proc.poll() is None:
        try:
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                subprocess.run(["taskkill", "/pid", str(proc.pid), "/T", "/F"], capture_output=True)
            else:
                proc.kill()
        except OSError:
            pass


def probe():
    """Smallest real request: proves install + login + network. Raises CopilotError."""
    import tempfile
    with tempfile.TemporaryDirectory(prefix="ta-copilot-") as tmp:
        return ask("Reply with exactly the word OK and nothing else.", tmp, timeout=180)


def open_login_terminal():
    """Open a visible terminal running `copilot login` (OAuth browser flow); returns the process."""
    cmd = command()
    if not cmd:
        raise CopilotError("Copilot CLI is not installed")
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    return subprocess.Popen([*cmd, "login"], creationflags=flags, env=child_env())
