"""Local HTTP server (stdlib only, so the UI comes up before any pip install).

  GET  /                       -> web/index.html      GET /static/*  -> web/*
  GET  /api/state              -> settings, steps, runs, busy
  GET  /api/events             -> Server-Sent Events (run / log / busy / doctor_log)
  GET  /api/doctor             -> checks            POST /api/doctor/fix {id} | /fix-all | /m365-login | /copilot-login | /mcp-test
  POST /api/mcp/tokens         -> save the 2-field token form
  POST /api/settings           -> patch settings
  POST /api/pick {kind}        -> native dialog
  POST /api/runs               -> create {issue, log_files[], source_dir, gate?, auto_attachments?, use_codegraph?,
                                  use_infographic?, auto_publish_to_collab?, log_shift_minutes?, logsleuth?, start?}
  GET  /api/runs/<id>          -> run
  POST /api/runs/<id>/start    -> {from?, only?, until?, resume?, publish?}
  POST /api/runs/<id>/skip     -> {step}: stop that step if running, then continue from the next
  POST /api/runs/<id>/inputs   -> any of the create options, to change them on an existing run
  POST /api/runs/<id>/gate     -> answer the evidence gate {names[], time_start?, time_end?, shift_minutes?, skip_logs?, folder?}
  POST /api/runs/<id>/timezone -> answer the time-zone question {mode: auto|suggested|manual|bypass, shift_minutes?,
                                  time_start?, time_end?, ticket_utc_offset?}
  POST /api/runs/<id>/attachments -> download {names[], folder?} without running the pipeline
  GET  /api/runs/<id>/attachment-files -> inventory of downloaded and extracted files
  GET  /api/runs/<id>/reasoning  -> recent public Copilot CLI reasoning/progress events
  POST /api/runs/<id>/extract     -> unpack an inventoried archive {path}
  POST /api/runs/<id>/delete      -> remove one history item, preserving active work and downloads
  GET  /api/runs/<id>/files/<name>  PUT same -> raw artifact / save edit
  POST /api/runs/<id>/graph-query {q}   POST /api/runs/<id>/open-folder
  POST /api/stop
  GET  /api/assistant          -> chat messages, busy, local bug tickets
  POST /api/assistant/ask {text?, run_id?, kind: chat|scan, reason?, trigger?}   POST /api/assistant/stop | /clear
  POST /api/assistant/action {message_id, index}   POST /api/assistant/open-bugs

Automation: docs/automation.md (this API and the `python -m app run` command line).
"""
import json
import mimetypes
import os
import queue
import re
import subprocess
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from . import assistant, attachments, doctor, events, explorer, mcp, paths, picker, pipeline, settings
from .ai import copilot
from .ai.m365 import worker as m365_worker
from .evidence import graph as codegraph
from .evidence import logsleuth
from .i18n import t

TYPES = {".md": "text/markdown; charset=utf-8", ".json": "application/json; charset=utf-8",
         ".xhtml": "text/plain; charset=utf-8", ".txt": "text/plain; charset=utf-8", ".log": "text/plain; charset=utf-8",
         ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".svg": "image/svg+xml"}


def _background(fn, *args):
    def run():
        try:
            fn(*args)
        except Exception as exc:
            assistant.record_error(getattr(fn, "__name__", "background"), exc)
            events.publish("doctor_log", line=f"✗ {exc}")
        finally:
            events.publish("doctor_changed")
    threading.Thread(target=run, daemon=True).start()


def _open_folder(path):
    if os.name == "nt":
        explorer.open_folder(path)
    else:
        subprocess.Popen(["xdg-open" if os.uname().sysname != "Darwin" else "open", str(path)])


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64  # browsers open several parallel connections (+ SSE); the default 5 resets some


class Handler(BaseHTTPRequestHandler):
    server_version = "TicketAnalyzer/2"
    # Keep-alive: the browser reuses a few connections instead of opening one per file,
    # which avoided sporadic ERR_CONNECTION_RESET on Windows during page load.
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    # -- helpers --------------------------------------------------------------
    def _send(self, status, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else (
            body.encode("utf-8") if isinstance(body, str) else json.dumps(body, ensure_ascii=False, default=str).encode("utf-8"))
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        return raw.decode("utf-8")

    def _json(self):
        text = self._body()
        return json.loads(text) if text.strip() else {}

    def _file(self, path):
        if not path.is_file():
            return self._send(404, {"error": "not found"})
        ctype = TYPES.get(path.suffix.lower()) or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self._send(200, path.read_bytes(), ctype)

    def _guard(self):
        # Only the local UI may call the API (blocks drive-by requests from other sites).
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if origin and urlparse(origin).netloc != host:
            self._send(403, {"error": "forbidden origin"})
            return False
        return True

    # -- routes ---------------------------------------------------------------
    def do_GET(self):
        url = urlparse(self.path)
        p = unquote(url.path)
        try:
            if p in ("/", "/index.html"):
                return self._file(paths.WEB / "index.html")
            if p.startswith("/static/"):
                target = (paths.WEB / p[len("/static/"):]).resolve()
                if paths.WEB.resolve() not in target.parents:
                    return self._send(404, {"error": "not found"})
                return self._file(target)
            if p == "/api/events":
                return self._events()
            if p == "/api/state":
                return self._send(200, state())
            if p == "/api/doctor":
                return self._send(200, doctor.checks())
            if p == "/api/logsleuth/catalogs":
                return self._send(200, {"catalogs": logsleuth.list_catalogs()})
            if p == "/api/assistant":
                return self._send(200, assistant.state())
            m = re.fullmatch(r"/api/runs/([^/]+)/reasoning", p)
            if m:
                run = pipeline.get(m.group(1))
                return self._send(200, {"events": pipeline.reasoning_events(run["id"])}) if run else self._send(404, {"error": "run not found"})
            m = re.fullmatch(r"/api/runs/([^/]+)/attachment-files", p)
            if m:
                run = pipeline.get(m.group(1))
                return self._send(200, {"files": pipeline.attachment_files(run)}) if run else self._send(404, {"error": "run not found"})
            m = re.fullmatch(r"/api/runs/([^/]+)", p)
            if m:
                run = pipeline.get(m.group(1))
                return self._send(200, pipeline.view(run)) if run else self._send(404, {"error": "run not found"})
            m = re.fullmatch(r"/api/runs/([^/]+)/files/([^/]+)", p)
            if m:
                return self._file(pipeline.read_file(m.group(1), m.group(2)))
            self._send(404, {"error": "not found"})
        except Exception as exc:
            assistant.record_error("GET " + p, exc)
            self._send(500, {"error": str(exc)})

    def do_PUT(self):
        if not self._guard():
            return
        m = re.fullmatch(r"/api/runs/([^/]+)/files/([^/]+)", unquote(urlparse(self.path).path))
        if not m:
            return self._send(404, {"error": "not found"})
        try:
            self._send(200, pipeline.view(pipeline.write_file(m.group(1), m.group(2), self._body())))
        except Exception as exc:
            self._send(400, {"error": str(exc)})

    def do_POST(self):
        if not self._guard():
            return
        p = unquote(urlparse(self.path).path)
        try:
            body = self._json()
            if p == "/api/settings":
                if pipeline.executor.busy and "ai_mode" in body:
                    raise RuntimeError(t("err.mode_busy"))
                saved = settings.save(body)
                events.publish("settings", settings=saved)
                return self._send(200, saved)
            if p == "/api/pick":
                return self._send(200, {"paths": picker.pick(body.get("kind"))})
            if p == "/api/logsleuth/catalog-info":
                return self._send(200, logsleuth.catalog_info(body.get("path") or ""))
            if p == "/api/mcp/tokens":
                mcp.save_tokens(body)
                _background(doctor.warm_mcp)
                return self._send(200, mcp.status())
            if p == "/api/doctor/fix":
                _background(doctor.run_fix, body.get("id"))
                return self._send(202, {"ok": True})
            if p == "/api/doctor/fix-all":
                _background(doctor.fix_all)
                return self._send(202, {"ok": True})
            if p == "/api/doctor/mcp-test":
                _background(doctor.warm_mcp)
                return self._send(202, {"ok": True})
            if p == "/api/doctor/m365-check":
                _background(lambda: doctor.checks(probe_m365=True))
                return self._send(202, {"ok": True})
            if p == "/api/doctor/m365-login":
                _background(lambda: (events.publish("doctor_log", line=t("dr.m365_opening")),
                                     m365_worker.login(), doctor._cache.update(m365=m365_worker.status)))
                return self._send(202, {"ok": True})
            if p == "/api/doctor/copilot-login":
                if not copilot.command():
                    raise RuntimeError("Copilot CLI is not installed")
                _background(doctor.copilot_login_flow)
                return self._send(202, {"ok": True})
            if p == "/api/doctor/copilot-check":
                _background(doctor.verify_copilot)
                return self._send(202, {"ok": True})
            if p == "/api/stop":
                pipeline.executor.stop()
                return self._send(200, {"ok": True})
            if p == "/api/assistant/ask":
                return self._send(200, assistant.ask(body.get("text"), body.get("run_id"), body.get("kind") or "chat",
                                                     body.get("reason") or "", body.get("trigger")))
            if p == "/api/assistant/action":
                return self._send(200, assistant.run_action(body.get("message_id"), body.get("index"), _open_folder))
            if p == "/api/assistant/stop":
                assistant.stop()
                return self._send(200, {"ok": True})
            if p == "/api/assistant/clear":
                return self._send(200, {"ok": True, **assistant.clear()})
            if p == "/api/assistant/open-bugs":
                assistant.BUGS.mkdir(parents=True, exist_ok=True)
                _open_folder(assistant.BUGS)
                return self._send(200, {"ok": True})
            if p == "/api/runs/clear":
                keep = pipeline.executor.run_id if pipeline.executor.busy else None
                removed = pipeline.clear_runs(keep)
                events.publish("runs_cleared", keep=keep)
                return self._send(200, {"removed": removed, "kept": keep})
            if p == "/api/runs":
                if pipeline.executor.busy and body.get("start", True):
                    raise RuntimeError(t("err.busy"))  # before creating a run that could never start
                run = pipeline.create(body.get("issue"), body.get("log_files") or [], body.get("source_dir") or "",
                                      body.get("auto_attachments"), body.get("download_dir") or "",
                                      body.get("use_codegraph"), body.get("logsleuth"),
                                      body.get("use_infographic"), body.get("continue_on_optional_error"),
                                      body.get("auto_publish_to_collab"), body.get("gate"),
                                      body.get("log_shift_minutes"), body.get("data_dir") or "")
                if body.get("start", True):
                    pipeline.executor.start(run["id"], until=body.get("until"))
                return self._send(200, pipeline.view(run))
            m = re.fullmatch(r"/api/runs/([^/]+)/(start|skip|inputs|gate|timezone|log-scan|graph-query|open-folder|attachments|extract|delete|reset)", p)
            if m:
                run_id, action = m.groups()
                if action == "skip":
                    pipeline.executor.skip_step(run_id, body.get("step"))
                    return self._send(202, {"ok": True})
                if action == "reset":
                    return self._send(200, pipeline.view(pipeline.reset_workflow(run_id)))
                if action == "delete":
                    return self._send(200, {"removed": pipeline.delete_run(run_id)})
                if action == "extract":
                    pipeline.executor.extract_attachment(run_id, body.get("path"))
                    return self._send(202, {"ok": True})
                if action == "start":
                    pipeline.executor.start(run_id, body.get("from"), bool(body.get("only")), bool(body.get("publish")),
                                            until=body.get("until"), resume=bool(body.get("resume")))
                    return self._send(200, {"ok": True})
                if action == "inputs":
                    return self._send(200, pipeline.view(pipeline.update_inputs(
                        run_id, body.get("log_files"), body.get("source_dir"), body.get("auto_attachments"),
                        body.get("download_dir"), body.get("use_codegraph"), body.get("logsleuth"),
                        body.get("use_infographic"), body.get("continue_on_optional_error"),
                        body.get("auto_publish_to_collab"), body.get("gate"),
                        body["log_shift_minutes"] if "log_shift_minutes" in body else ..., body.get("data_dir"))))
                if action == "gate":
                    pipeline.executor.resolve_gate(
                        run_id, body.get("names") or [], body.get("folder"), body.get("time_start"), body.get("time_end"),
                        body["shift_minutes"] if "shift_minutes" in body else ..., bool(body.get("skip_logs")),
                        ticket_utc_offset=body["ticket_utc_offset"] if "ticket_utc_offset" in body else ...)
                    return self._send(202, {"ok": True})
                if action == "timezone":
                    pipeline.executor.resolve_timezone(run_id, body.get("mode"), body.get("shift_minutes"),
                                                       body.get("time_start"), body.get("time_end"),
                                                       body.get("ticket_utc_offset"))
                    return self._send(202, {"ok": True})
                if action == "log-scan":
                    pipeline.executor.start(run_id, "log", only=True, catalog_only=True)
                    return self._send(202, {"ok": True})
                if action == "attachments":
                    pipeline.executor.fetch_attachments(run_id, body.get("names") or [], body.get("folder"))
                    return self._send(202, {"ok": True})
                if action == "graph-query":
                    return self._send(200, {"hits": codegraph.query(pipeline.read_file(run_id, "04-graph.json"), body.get("q", ""))})
                if action == "open-folder":
                    target = pipeline.run_dir(run_id)
                    if body.get("what") == "attachments":
                        target = pipeline.attachment_folder(pipeline.get(run_id))
                        target.mkdir(parents=True, exist_ok=True)
                    _open_folder(target)
                    return self._send(200, {"ok": True})
            self._send(404, {"error": "not found"})
        except Exception as exc:
            if not p.startswith("/api/assistant"):  # the chat shows its own errors
                assistant.record_error("POST " + p, exc)
            self._send(400, {"error": str(exc)})

    def _events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")  # streamed until the tab goes away; no Content-Length
        self.close_connection = True
        self.end_headers()
        q = events.subscribe()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    message = q.get(timeout=15)
                    self.wfile.write(f"data: {message}\n\n".encode("utf-8"))
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            events.unsubscribe(q)


def state():
    return {
        "settings": settings.load(),
        "steps": pipeline.STEPS,
        "runs": pipeline.list_runs(),
        "busy": pipeline.executor.busy,
        "busy_run": pipeline.executor.run_id if pipeline.executor.busy else None,
        "mcp": mcp.status(),
        "models": {"m365": settings.M365_MODELS, "copilot": settings.COPILOT_MODELS},
        "gates": list(settings.GATES),
        "logsleuth": {"defaults": logsleuth.DEFAULT_OPTIONS, "parsers": logsleuth.PARSERS,
                  "max_tokens": logsleuth.MAX_BRIEF_TOKENS, "min_tokens": logsleuth.MIN_BRIEF_TOKENS},
        "default_download_dir": str(attachments.default_folder("<ISSUE>")),
        "runs_dir": str(paths.RUNS),  # a run without a ticket data folder keeps its files in runs_dir/<run id>/
    }


def main():
    paths.ensure_dirs()
    port = int(os.getenv("TA_PORT") or settings.load()["port"])
    server = Server(("127.0.0.1", port), Handler)
    url = f"http://localhost:{port}"
    print(t("srv.running", url=url), flush=True)
    # Probe M365 login once on startup even when Copilot CLI is the selected backend.
    if not os.getenv("TA_NO_STARTUP_PROBE"):
        _background(lambda: doctor.checks(probe_m365=True))
    if not os.getenv("TA_NO_BROWSER"):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        pipeline.executor.stop()
        m365_worker.shutdown()
        server.server_close()
