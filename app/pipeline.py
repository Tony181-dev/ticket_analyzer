"""The 8-step workflow: Jira -> AI understand -> Log brief -> Code graph ->
AI analysis -> AI format -> optional AI infographic -> Collab.

Each run lives in data/runs/<ISSUE>-<stamp>/ with numbered artifacts and a
run.json holding step states. Only one run executes at a time (M365 drives a
single browser).
"""
import html
import json
import re
import shutil
import threading
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path

from . import ai, attachments, events, logprep, mcp, paths, settings
from .evidence import graph as codegraph
from .i18n import t
from .evidence import logsleuth
from .evidence.timestamps import normalize_focus, parse_focus
from .report import collab, infographic, preview, storage
from .report.refine_pipeline import refine_report

STEPS = [
    {"id": "jira", "title": "Jira ticket", "sub": "MCP jira_get_issue"},
    {"id": "understand", "title": "AI hiểu ticket", "sub": "Lấy timestamp từ ticket"},
    {"id": "log", "title": "Log brief", "sub": "LogSleuth + source catalog"},
    {"id": "graph", "title": "Code graph", "sub": "CodeGraph"},
    {"id": "analyze", "title": "AI phân tích", "sub": "Phân tích tổng hợp"},
    {"id": "format", "title": "AI format", "sub": "Confluence XHTML"},
    {"id": "infographic", "title": "AI infographic", "sub": "M365 / Copilot CLI"},
    {"id": "collab", "title": "Đăng Collab", "sub": "create / update"},
]
ORDER = [s["id"] for s in STEPS]
CONTINUABLE_ERROR_STEPS = {"understand", "log", "graph"}

# (file, label, kind, editable)
ARTIFACTS = {
    "jira": [("01-jira.md", "Ticket", "md", False), ("01-jira.json", "JSON thô", "json", False),
             ("01-attachments.json", "Attachments", "json", False)],
    "understand": [("02-understanding.md", "Thời điểm trong ticket", "md", False),
                   ("02-focus.json", "Khung giờ & từ khoá", "json", False)],
        "log": [("03-log-brief.md", "Log brief", "md", False), ("03-catalog.md", "Catalog log", "md", False),
            ("03-log-stats.json", "LogSleuth stats", "json", False)],
    "graph": [("04-brain.html", "Sơ đồ tương tác", "html", False), ("04-GRAPH_REPORT.md", "Report", "md", False),
              ("04-graph.simple.json", "graph.simple.json (AI)", "json", False), ("04-graph.json", "graph.json", "json", False)],
    "analyze": [("05-analysis.md", "Phân tích", "md", True),
                ("05-source-evidence.md", "Code evidence", "md", False)],
    "format": [("06-report.preview.html", "Xem trước", "html", False), ("06-report.xhtml", "XHTML", "xhtml", True),
               ("06-report.raw.txt", "Phản hồi gốc của AI", "text", False), ("06-refine.log", "Log refine", "text", False)],
    "infographic": [("06-infographic.png", "Infographic", "image", False), ("06-infographic.jpg", "Infographic", "image", False),
                    ("06-infographic.webp", "Infographic", "image", False), ("06-infographic.svg", "Infographic", "image", False)],
    "collab": [("07-collab.json", "Kết quả Collab", "json", False)],
}
EDITABLE = {f for items in ARTIFACTS.values() for f, _, _, e in items if e} | {"02-focus.json"}
DOWNSTREAM_OF_FILE = {"05-analysis.md": "format", "06-report.xhtml": "collab", "02-focus.json": "log"}

_lock = threading.RLock()
_runs = {}


def _empty_copilot_usage():
    return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0, "reasoning_tokens": 0,
            "model_calls": 0, "updated": None}


def _record_copilot_usage(run, delta):
    if run.get("ai_mode") != "copilot" or not isinstance(delta, dict):
        return
    try:
        calls = max(0, int(delta.get("model_calls", 0)))
    except (TypeError, ValueError, OverflowError):
        calls = 0
    if not calls:
        return
    usage = run.setdefault("copilot_usage", _empty_copilot_usage())
    for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens"):
        try:
            value = max(0, int(delta.get(key, 0)))
        except (TypeError, ValueError, OverflowError):
            value = 0
        usage[key] = max(0, int(usage.get(key, 0))) + value
    usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    usage["model_calls"] = max(0, int(usage.get("model_calls", 0))) + calls
    usage["updated"] = time.time()
    _save(run)


def _ask_ai(run, cfg, prompt, files, work_dir, **kwargs):
    kwargs["on_usage"] = lambda delta: _record_copilot_usage(run, delta)
    return ai.ask(cfg, prompt, files, work_dir, **kwargs)


def _finish_step(state, status, **changes):
    ended = time.time()
    try:
        elapsed = max(0.0, ended - float(state["started"])) if state.get("started") is not None else 0.0
    except (TypeError, ValueError, OverflowError):
        elapsed = 0.0
    try:
        previous = max(0.0, float(state.get("elapsed_seconds", 0) or 0))
    except (TypeError, ValueError, OverflowError):
        previous = 0.0
    state.update(status=status, ended=ended, elapsed_seconds=round(previous + elapsed, 3), **changes)


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------------------
# run store
# ---------------------------------------------------------------------------
def issue_key(text):
    m = re.search(r"([A-Za-z][A-Za-z0-9_]+-\d+)", str(text or ""))
    return m.group(1).upper() if m else ""


def run_dir(run_id):
    return paths.RUNS / run_id


def _artifacts(run_id, step):
    d = run_dir(run_id)
    return [{"name": f, "label": label, "kind": kind, "editable": editable}
            for f, label, kind, editable in ARTIFACTS[step] if (d / f).exists()]


def _save(run):
    run["updated"] = time.time()
    for step in ORDER:
        run["steps"][step]["artifacts"] = _artifacts(run["id"], step)
    tmp = run_dir(run["id"]) / "run.json.tmp"
    tmp.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(run_dir(run["id"]) / "run.json")
    events.publish("run", run=run)


def get(run_id):
    with _lock:
        if run_id not in _runs:
            file = run_dir(run_id) / "run.json"
            if not file.exists():
                return None
            run = json.loads(file.read_text(encoding="utf-8"))
            run.pop("att_progress", None)
            run.setdefault("inputs", {}).setdefault(
                "use_infographic", any(run_dir(run_id).glob("06-infographic.*")))
            run.setdefault("inputs", {}).setdefault("continue_on_optional_error", False)
            changed = False
            if "auto_publish_to_collab" not in run["inputs"]:
                run["inputs"]["auto_publish_to_collab"] = not settings.load().get("review_before_publish", True)
                changed = True
            if not isinstance(run.get("copilot_usage"), dict):
                run["copilot_usage"] = _empty_copilot_usage()
                changed = True
            else:
                for key, value in _empty_copilot_usage().items():
                    if key not in run["copilot_usage"]:
                        run["copilot_usage"][key] = value
                        changed = True
            for step in ORDER:  # a crash mid-run leaves "running" behind
                state = run["steps"].setdefault(step, {"status": "idle", "note": "", "error": "", "started": None,
                                                       "ended": None, "elapsed_seconds": 0.0, "artifacts": []})
                if "elapsed_seconds" not in state:
                    try:
                        if state.get("started") is not None and state.get("ended") is not None:
                            elapsed = max(0.0, float(state["ended"]) - float(state["started"]))
                        else:
                            elapsed = 0.0
                        state["elapsed_seconds"] = round(elapsed, 3)
                    except (TypeError, ValueError, OverflowError):
                        state["elapsed_seconds"] = 0.0
                    changed = True
                if state["status"] == "running":
                    ended = time.time()
                    try:
                        elapsed = max(0.0, ended - float(state["started"])) if state.get("started") is not None else 0.0
                    except (TypeError, ValueError, OverflowError):
                        elapsed = 0.0
                    state.update(status="error", error=t("err.interrupted"), ended=ended,
                                 elapsed_seconds=round(state["elapsed_seconds"] + elapsed, 3))
                    changed = True
            _runs[run_id] = run
            if changed:
                _save(run)
        return _runs[run_id]


def list_runs(limit=50):
    items = []
    for file in sorted(paths.RUNS.glob("*/run.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            run = json.loads(file.read_text(encoding="utf-8"))
        except ValueError:
            continue
        done = sum(1 for s in run["steps"].values() if s["status"] == "done")
        items.append({"id": run["id"], "issue": run["issue"], "summary": run.get("summary", ""),
                      "created": run["created"], "done": done, "collab": run.get("collab") or {},
                      "failed": any(s["status"] == "error" for s in run["steps"].values())})
    return items


def create(issue, log_files=(), source_dir="", auto_attachments=False, download_dir="", use_codegraph=True,
           logsleuth_options=None, use_infographic=True, continue_on_optional_error=False,
           auto_publish_to_collab=None):
    key = issue_key(issue)
    if not key:
        raise ValueError(t("err.issue"))
    files = [str(Path(f)) for f in log_files or [] if str(f).strip()]
    for f in files:
        if not Path(f).is_file():
            raise ValueError(t("err.log_missing", path=f))
    if source_dir and not Path(source_dir).is_dir():
        raise ValueError(t("err.src_missing", path=source_dir))
    cfg = settings.load()
    if auto_publish_to_collab is None:
        auto_publish_to_collab = not cfg.get("review_before_publish", True)
    log_options = logsleuth.normalize_options(logsleuth_options, cfg["log_budget_tokens"])
    selected_catalog = None
    if log_options["catalog_path"]:
        selected_catalog = {"path": log_options["catalog_path"], "source_path": log_options["catalog_path"],
                            **logsleuth.catalog_info(log_options["catalog_path"])}
    run_id = f"{key}-{datetime.now():%Y%m%d-%H%M%S}"
    run_dir(run_id).mkdir(parents=True, exist_ok=True)
    run = {
        "id": run_id, "issue": key, "summary": "", "created": time.time(), "updated": time.time(),
        "inputs": {"log_files": files, "source_dir": str(source_dir or ""), "auto_attachments": bool(auto_attachments),
                   "download_dir": str(download_dir or ""), "use_codegraph": bool(use_codegraph),
                   "use_infographic": bool(use_infographic),
                   "continue_on_optional_error": bool(continue_on_optional_error),
                   "auto_publish_to_collab": bool(auto_publish_to_collab), "logsleuth": log_options},
        "attachments": [],
        "session_id": str(uuid.uuid4()), "focus": {}, "collab": {}, "ai_mode": cfg["ai_mode"],
        "copilot_usage": _empty_copilot_usage(),
        "steps": {s: {"status": "idle", "note": "", "error": "", "started": None, "ended": None,
                  "elapsed_seconds": 0.0, "artifacts": []} for s in ORDER},
    }
    with _lock:
        if selected_catalog:
            run["log_catalog"] = selected_catalog
        _runs[run_id] = run
        _save(run)
    return run


def clear_runs(keep=None):
    """Delete every run folder except `keep` (the one executing). Downloads elsewhere are untouched."""
    removed = 0
    with _lock:
        for d in list(paths.RUNS.iterdir()) if paths.RUNS.exists() else []:
            if d.is_dir() and d.name != keep:
                shutil.rmtree(d, ignore_errors=True)
                _runs.pop(d.name, None)
                removed += 1
    return removed


def update_inputs(run_id, log_files=None, source_dir=None, auto_attachments=None, download_dir=None, use_codegraph=None,
                  logsleuth_options=None, use_infographic=None, continue_on_optional_error=None,
                  auto_publish_to_collab=None):
    run = get(run_id)
    if not run:
        raise ValueError("Run not found")
    if executor.busy and executor.run_id == run_id:
        raise RuntimeError(t("err.busy"))
    if logsleuth_options is not None:
        options = logsleuth.normalize_options({**run["inputs"].get("logsleuth", {}), **logsleuth_options},
                                             settings.load()["log_budget_tokens"])
        previous_catalog = run["inputs"].get("logsleuth", {}).get("catalog_path")
        if options["catalog_path"] and options["catalog_path"] != previous_catalog:
            info = logsleuth.catalog_info(options["catalog_path"])
            run["log_catalog"] = {"path": options["catalog_path"], "source_path": options["catalog_path"], **info}
        run["inputs"]["logsleuth"] = options
        _mark_stale(run, "log")
    if auto_attachments is not None:
        run["inputs"]["auto_attachments"] = bool(auto_attachments)
    if download_dir is not None:
        run["inputs"]["download_dir"] = str(download_dir)
    if use_codegraph is not None and bool(use_codegraph) != run["inputs"].get("use_codegraph", True):
        run["inputs"]["use_codegraph"] = bool(use_codegraph)
        _mark_stale(run, "graph")
    if use_infographic is not None and bool(use_infographic) != run["inputs"].get("use_infographic", True):
        enabled = bool(use_infographic)
        run["inputs"]["use_infographic"] = enabled
        _mark_stale(run, "infographic")
        state = run["steps"]["infographic"]
        if enabled:
            state.update(status="stale", note="", error="")
        else:
            state.update(status="skipped", note=t("note.skip_infographic_disabled"), error="")
    if continue_on_optional_error is not None:
        run["inputs"]["continue_on_optional_error"] = bool(continue_on_optional_error)
    if auto_publish_to_collab is not None:
        run["inputs"]["auto_publish_to_collab"] = bool(auto_publish_to_collab)
    if log_files is not None:
        run["inputs"]["log_files"] = [f for f in log_files if f]
        if run["inputs"]["log_files"] and run["steps"]["understand"]["status"] == "skipped":
            run["steps"]["understand"].update(status="idle", note="", error="")
        _mark_stale(run, "log")
    if source_dir is not None:
        run["inputs"]["source_dir"] = source_dir
        run.pop("log_catalog", None)
        if "logsleuth" in run["inputs"]:
            run["inputs"]["logsleuth"]["catalog_path"] = ""
        _mark_stale(run, "log")
    _save(run)
    return run


def _mark_stale(run, from_step):
    for step in ORDER[ORDER.index(from_step):]:
        state = run["steps"][step]
        if state["status"] in ("done", "waiting"):
            state["status"] = "stale"
        elif state["status"] == "skipped":
            if APPLICABLE[step](run):
                state.update(status="idle", note="", error="")
            else:
                state["note"] = t(_skip_reason(run, step))


def read_file(run_id, name):
    path = (run_dir(run_id) / name).resolve()
    if run_dir(run_id).resolve() not in path.parents:
        raise ValueError("invalid path")
    return path


def write_file(run_id, name, text):
    if name not in EDITABLE:
        raise ValueError(t("err.not_editable"))
    run = get(run_id)
    if name == "06-report.xhtml":
        # Same repair + validation as the AI output; raises with the parser's message if unrecoverable.
        text = storage.normalize_storage_report(text)
        (run_dir(run_id) / "06-report.preview.html").write_text(_preview_page(text, run["issue"]), encoding="utf-8")
    if name == "02-focus.json":
        run["focus"] = _clean_focus(json.loads(text))
        text = json.dumps(run["focus"], ensure_ascii=False, indent=2)
    (run_dir(run_id) / name).write_text(text, encoding="utf-8")
    _mark_stale(run, DOWNSTREAM_OF_FILE.get(name, "format"))
    _log(run, t("log.saved", name=name))
    _save(run)
    return run


# ---------------------------------------------------------------------------
# executor
# ---------------------------------------------------------------------------
class Executor:
    def __init__(self):
        self.thread = None
        self.run_id = None
        self.cancel = threading.Event()

    @property
    def busy(self):
        return bool(self.thread and self.thread.is_alive())

    def start(self, run_id, start=None, only=False, publish=False, catalog_only=False):
        if self.busy:
            raise RuntimeError(t("err.busy"))
        if not get(run_id):
            raise ValueError("Run not found")
        if start and start not in ORDER:
            raise ValueError(f"Unknown pipeline step: {start}")
        self.cancel.clear()
        ai.reset()
        self.run_id = run_id
        self.thread = threading.Thread(target=self._run, args=(run_id, start, only, publish, catalog_only), daemon=True)
        self.thread.start()

    def stop(self):
        self.cancel.set()
        ai.cancel()

    def _run(self, run_id, start, only, publish, catalog_only=False):
        run = get(run_id)
        cfg = settings.load()
        mode_changed = (run.get("ai_mode") != cfg["ai_mode"]
                or run.get("infographic_mode") not in (None, cfg["ai_mode"]))
        if (mode_changed and run.get("inputs", {}).get("use_infographic", True)
            and run["steps"]["infographic"]["status"] in ("done", "stale", "error")):
            run["steps"]["infographic"].update(status="stale", note="", error="")
            _mark_stale(run, "infographic")
        run["ai_mode"] = cfg["ai_mode"]
        requested_first = ORDER.index(start) if start else 0
        requested_step = ORDER[requested_first]
        first = requested_first
        for index, prerequisite in enumerate(ORDER[:requested_first] if not catalog_only else []):
            if (run["steps"][prerequisite]["status"] == "stale"
                    and APPLICABLE[prerequisite](run)):
                first = index
                break
        if run.pop("pending_attachments", None) and run["steps"]["jira"]["status"] == "waiting":
            run["steps"]["jira"].update(status="done", note=_jira_note(run))
        if first < requested_first:
            _log(run, t("log.rewind_stale", step=STEP_TITLE[ORDER[first]], requested=STEP_TITLE[requested_step]))
        _mark_stale(run, ORDER[first])
        events.publish("busy", run_id=run_id, busy=True)
        try:
            for step in ORDER[first:]:
                if self.cancel.is_set():
                    break
                state = run["steps"][step]
                if not APPLICABLE[step](run) and not (catalog_only and step == "log"):
                    state.update(status="skipped", note=t(_skip_reason(run, step)), error="")
                    _save(run)
                    if only and step == requested_step:
                        break
                    continue
                if (step == "log" and run["inputs"].get("logsleuth", {}).get("mode") == "manual"
                        and start != "log"):
                    state.update(status="waiting", note=t("note.log_manual"), error="")
                    _save(run)
                    break
                if (step == "collab" and not run["inputs"].get("auto_publish_to_collab", False)
                    and not publish and not (only and start == "collab")):
                    state.update(status="waiting", note=t("note.review"), error="")
                    _save(run)
                    break
                state.update(status="running", note="", error="", started=time.time(), ended=None)
                _save(run)
                _log(run, "▶ " + t("step." + step))
                try:
                    if catalog_only and step == "log":
                        _build_log_catalog(run, self, force=True)
                        _finish_step(state, "waiting", note=t("note.log_catalog_ready"))
                        _save(run)
                        break
                    note = RUNNERS[step](run, cfg, self)
                except Cancelled:
                    _finish_step(state, "idle", note=t("note.stopped"))
                    _save(run)
                    break
                except Exception as exc:  # surface every failure on the step card
                    if self.cancel.is_set():
                        _finish_step(state, "idle", note=t("note.stopped"))
                    else:
                        _finish_step(state, "error", error=str(exc))
                        _log(run, "✗ " + t("step." + step) + f": {exc}")
                        _log(run, traceback.format_exc(), quiet=True)
                    _save(run)
                    if (not self.cancel.is_set() and not only and step in CONTINUABLE_ERROR_STEPS
                            and run["inputs"].get("continue_on_optional_error")):
                        _log(run, t("log.continue_optional_error", step=t("step." + step)))
                        continue
                    break
                _finish_step(state, "done", note=note or "")
                _log(run, "✓ " + t("step." + step) + (f" — {note}" if note else ""))
                if step == "jira" and run["inputs"].get("auto_attachments") and run.get("attachments"):
                    # Let the user pick which attachments to download; the run resumes at step 2 afterwards.
                    run["pending_attachments"] = {"resume": not only, "publish": publish}
                    state.update(status="waiting", note=note + " · " + t("note.pick_att"))
                    _save(run)
                    break
                _save(run)
                if only and step == requested_step:
                    break
        finally:
            events.publish("busy", run_id=run_id, busy=False)

    def check(self):
        if self.cancel.is_set():
            raise Cancelled()

    def fetch_attachments(self, run_id, names, folder=None):
        """Download the chosen attachments (log/archive ones are extracted and analyzed).

        While the run waits for this choice, continue the pipeline at step 2 afterwards (empty `names` = skip).
        """
        if self.busy:
            raise RuntimeError(t("err.busy"))
        run = get(run_id)
        if not run:
            raise ValueError("Run not found")
        pending = run.get("pending_attachments")
        if not names and not pending:
            raise ValueError(t("err.att_none"))
        if folder is not None:
            run["inputs"]["download_dir"] = str(folder).strip()
        self.cancel.clear()
        self.run_id = run_id
        names = set(names)
        selected_names = names
        names = attachments.expand_7z_volumes(names, (a["filename"] for a in run.get("attachments") or []))
        added_volumes = sum(1 for name in names - selected_names if attachments.is_7z_volume(name))
        analyze = {a["filename"] for a in run.get("attachments") or [] if a["filename"] in names and a["kind"] != "other"}
        if names:  # visible at once, before the worker thread has fetched anything
            run["att_progress"] = {"phase": "download", "done": 0, "total": len(names), "name": ""}
            _save(run)

        def job():
            events.publish("busy", run_id=run_id, busy=True)
            jira = run["steps"]["jira"]
            before = {k: jira[k] for k in ("status", "note")}
            resume = False
            try:
                if added_volumes:
                    _log(run, t("log.att_split_volumes", selected=len(selected_names), added=added_volumes, total=len(names)))
                added = _fetch_attachments(run, names, analyze, self.check)
                if pending:
                    run.pop("pending_attachments", None)
                    jira.update(status="done", note=_jira_note(run, added if names else None))
                    resume = pending.get("resume", True)
                else:
                    jira.update(before, note=_jira_note(run, added) if names else before["note"])
                    if added:
                        if run["steps"]["understand"]["status"] == "skipped":
                            run["steps"]["understand"].update(status="idle", note="", error="")
                        _mark_stale(run, "log")
                _save(run)
                if resume:
                    self._run(run_id, "understand", False, pending.get("publish", False))
            except Cancelled:
                jira.update(before)
                _log(run, t("note.stopped"))
            except Exception as exc:
                jira.update(before)
                _log(run, f"✗ {exc}")
            finally:
                run.pop("att_progress", None)
                _save(run)
                events.publish("busy", run_id=run_id, busy=False)
        self.thread = threading.Thread(target=job, daemon=True)
        self.thread.start()


executor = Executor()
STEP_TITLE = {s["id"]: s["title"] for s in STEPS}


def _log(run, line, quiet=False):
    stamp = datetime.now().strftime("%H:%M:%S")
    with open(run_dir(run["id"]) / "run.log", "a", encoding="utf-8") as f:
        f.write(f"[{stamp}] {line}\n")
    if not quiet:
        events.publish("log", run_id=run["id"], line=f"[{stamp}] {line}")


def _write(run, name, text):
    (run_dir(run["id"]) / name).write_text(text, encoding="utf-8")
    return run_dir(run["id"]) / name


def _path(run, name):
    return run_dir(run["id"]) / name


def _done(run, step):
    return run["steps"][step]["status"] == "done"


def _instruction(name):
    return paths.INSTRUCTIONS / name


def _log_options(run, budget_tokens=None):
    """Manual honors per-run tuning; Auto always starts from LogSleuth defaults."""
    saved = run["inputs"].get("logsleuth") or {}
    budget_tokens = logsleuth.MAX_BRIEF_TOKENS if budget_tokens is None else budget_tokens
    if saved.get("mode") == "manual":
        return logsleuth.normalize_options(saved, budget_tokens)
    return logsleuth.normalize_options({"mode": "auto"}, budget_tokens)


# ---------------------------------------------------------------------------
# steps
# ---------------------------------------------------------------------------
def step_jira(run, cfg, ex):
    result = mcp.call("jira", "jira_get_issue", {
        "issue_key": run["issue"], "fields": "*all", "expand": "renderedFields,changelog", "comment_limit": 50})
    text = mcp.result_text(result)
    if result.get("isError") or not text.strip():
        raise RuntimeError(t("err.jira", detail=text[:800] or t("err.no_content")))
    try:
        data = json.loads(text)
        pretty = json.dumps(data, ensure_ascii=False, indent=2)
        run["summary"] = _find_summary(data)
    except ValueError:
        pretty = text
        m = re.search(r'"summary"\s*:\s*"([^"]+)"', text)
        run["summary"] = m.group(1) if m else ""
    _write(run, "01-jira.json", pretty)
    _write(run, "01-jira.md", f"# Jira Ticket Evidence — {run['issue']}\n\n"
           f"Link: {cfg['jira_browse_url'].rstrip('/')}/{run['issue']}\n\n```json\n{pretty}\n```\n")
    found = attachments.from_issue_json(text)
    if not found and run["inputs"].get("auto_attachments"):
        try:
            found = attachments.list_rest(run["issue"])
        except Exception as exc:  # listing attachments must never fail the Jira step
            _log(run, f"attachments: {exc}", quiet=True)
    known = {a["filename"]: a for a in run.get("attachments") or []}
    run["attachments"] = [{**a, **{k: known[a["filename"]][k] for k in ("path", "analyzed") if k in known.get(a["filename"], {})}}
                          for a in found]
    _write(run, "01-attachments.json", json.dumps(run["attachments"], ensure_ascii=False, indent=2))
    return _jira_note(run)


def _jira_note(run, added=None):
    note = run["summary"][:120]
    if run.get("attachments"):
        note += " · " + t("note.attachments", n=len(run["attachments"]))
    if added is not None:
        note += " · " + t("note.att_logs", n=added)
    return note


def attachment_folder(run):
    custom = (run["inputs"].get("download_dir") or settings.load().get("download_dir") or "").strip()
    return Path(custom) / run["issue"] if custom else attachments.default_folder(run["issue"])


def _fetch_attachments(run, names, analyze, cancel):
    """Download the named attachments; logs from `analyze` ones join the log inputs. Returns #log files added."""
    items = [a for a in run.get("attachments") or [] if a["filename"] in names]
    if not items:
        return 0
    folder = attachment_folder(run)
    _log(run, t("log.att_download", n=len(items), folder=folder))
    jira = run["steps"]["jira"]
    jira.update(status="running", error="")

    def progress(phase, done, total, name, pct=None):
        run["att_progress"] = {"phase": phase, "done": done, "total": total, "name": name, "pct": pct}
        jira["note"] = t("note.att_" + phase, done=done, total=total, name=name)
        _save(run)

    try:
        logs, files = attachments.collect(run["issue"], items, analyze, folder, log=lambda m: _log(run, m), cancel=cancel,
                                          progress=progress)
    finally:
        run.pop("att_progress", None)
    for a in run["attachments"]:
        if a["filename"] in files:
            a["path"] = files[a["filename"]]
            a["analyzed"] = a["filename"] in analyze or a.get("analyzed", False)
    current = run["inputs"]["log_files"]
    added = [f for f in logs if f not in current]
    run["inputs"]["log_files"] = current + added
    _write(run, "01-attachments.json", json.dumps(run["attachments"], ensure_ascii=False, indent=2))
    _log(run, t("log.att_added", n=len(added)))
    _save(run)
    return len(added)


def _find_summary(data):
    if isinstance(data, dict):
        if isinstance(data.get("summary"), str):
            return data["summary"]
        for value in data.values():
            found = _find_summary(value)
            if found:
                return found
    return ""


def _ready_logs(run, ex):
    """The selected logs converted to formats LogSleuth reads (archives expanded, encodings fixed)."""
    files = logprep.prepare(run["inputs"]["log_files"], run_dir(run["id"]) / "logs-prepared",
                            log=lambda m: _log(run, m), cancel=ex.check)
    if not files:
        raise RuntimeError(t("err.no_readable_logs"))
    return files


def _unreadable(run, sources):
    """Log files LogSleuth could not read; raises when none is usable."""
    bad = [s for s in sources if s["format"] == "binary"]
    for s in bad:
        _log(run, t("log.prep_skip", name=s["name"], why=t("log.why_binary")))
    if sources and len(bad) == len(sources):
        raise RuntimeError(t("err.no_readable_logs"))


def step_understand(run, cfg, ex):
    ex.check()
    answer = _ask_ai(run, cfg, (f"Jira issue {run['issue']}. Read only the attached Jira ticket and follow "
                          "understand.md to extract an explicitly stated incident time. Do not inspect logs or source code."),
                    [_path(run, "01-jira.md"), _instruction("understand.md")],
                    run_dir(run["id"]), session_id=run["session_id"])
    ex.check()
    answer = storage.strip_chat_wrappers(answer)
    _write(run, "02-understanding.md", answer)
    focus = _parse_focus(answer)
    run["focus"] = focus
    _write(run, "02-focus.json", json.dumps(focus, ensure_ascii=False, indent=2))
    if focus.get("time_start") or focus.get("time_end"):
        return t("note.window", start=focus.get("time_start") or "…", end=focus.get("time_end") or "…",
                 confidence=focus.get("confidence") or "?")
    return t("note.nowindow")


def _parse_focus(answer):
    return parse_focus(answer)


def _clean_focus(data):
    return normalize_focus(data)


def _build_log_catalog(run, ex, force=False):
    source = run["inputs"]["source_dir"]
    if not source or not Path(source).is_dir():
        raise ValueError(t("err.need_catalog_source"))
    _log(run, t("log.scan"))
    options = _log_options(run)
    catalog = logsleuth.catalog_for(source, force=force, options=options, cancel=ex.check)
    run["inputs"].setdefault("logsleuth", options)["catalog_path"] = str(catalog)
    return _use_log_catalog(run, catalog, ex)


def _use_log_catalog(run, catalog, ex):
    info = logsleuth.catalog_info(catalog, cancel=ex.check)
    target = _path(run, "03-catalog.md")
    if Path(catalog).resolve() != target.resolve():
        shutil.copyfile(catalog, target)
    run["log_catalog"] = {"path": str(target), "source_path": str(catalog), **info}
    return target


def step_log(run, cfg, ex):
    files = _ready_logs(run, ex)
    source = run["inputs"]["source_dir"]
    options = _log_options(run, cfg["log_budget_tokens"])
    manual = options["mode"] == "manual"
    catalog = None
    if manual:
        selected = options["catalog_path"] or (run.get("log_catalog") or {}).get("path")
        if not selected or not Path(selected).is_file():
            raise ValueError(t("err.log_manual_catalog"))
        catalog = _use_log_catalog(run, selected, ex)
    elif source:
        catalog = _build_log_catalog(run, ex)
    else:
        _path(run, "03-catalog.md").unlink(missing_ok=True)
        run.pop("log_catalog", None)
        # LogSleuth has a no-source mode that seeds from ticket keywords and
        # generic error signals. Keep log evidence available without a source tree.
        _log(run, t("log.no_catalog"))
    ex.check()
    try:
        if not _done(run, "understand"):
            raise FileNotFoundError("Step 2 did not complete")
        focus = _clean_focus(json.loads(_path(run, "02-focus.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        focus = _clean_focus(None)
    if manual:
        focus = _clean_focus({"time_start": options["time_start"] if options["time_enabled"] else None,
                              "time_end": options["time_end"] if options["time_enabled"] else None})
    elif focus["filter_mode"] == "full":
        # No timestamp contract: match ALL catalog services over the full logs.
        focus["keywords"] = []
    out = _path(run, "03-log-brief.md")
    stats = logsleuth.slice_logs(files, out, catalog, focus, cfg["log_budget_tokens"], options, cancel=ex.check)
    _unreadable(run, stats.get("sources") or [])
    window = stats.get("timeWindow")
    fallback = False
    if not manual and stats.get("afterTime", 0) == 0 and window:
        _log(run, t("log.window_empty"))
        stats = logsleuth.slice_logs(files, out, catalog, {**focus, "time_start": "", "time_end": ""},
                                    cfg["log_budget_tokens"], options, cancel=ex.check)
        window = None
        fallback = True
    stats.update(mode=options["mode"], requestedFocus=focus, fullTimeFallback=fallback)
    run["log_stats"] = stats
    _write(run, "03-log-stats.json", json.dumps(stats, ensure_ascii=False, indent=2))
    note = t("note.log", total=stats["totalLines"], kept=stats["kept"], tokens=stats["tokens"])
    if not catalog:
        note += " · " + t("note.log_no_catalog")
    if window:
        note += f", {window['start']}–{window['end']}"
    return note


def step_graph(run, cfg, ex):
    out, meta = codegraph.build(run["inputs"]["source_dir"], cfg["graph_token_budget"])
    codegraph.copy_into(out, run_dir(run["id"]))
    return t("note.graph", files=meta.get("files", 0), nodes=meta.get("nodes", 0), edges=meta.get("edges", 0))


def _source_code_evidence(run, max_refs=12, context_lines=8):
    """Return bounded source excerpts for the log statements observed by LogSleuth."""
    source_value = run["inputs"].get("source_dir")
    brief_path = _path(run, "03-log-brief.md")
    if not source_value or not brief_path.is_file():
        return ""
    catalog_root = (run.get("log_catalog", {}).get("stats") or {}).get("root")
    if catalog_root and Path(catalog_root).resolve() != Path(source_value).resolve():
        return ""  # an imported catalog may describe a different checkout
    try:
        brief = brief_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    section = re.search(r"(?ms)^## Source statements observed\s*\n(.*?)(?=^## |\Z)", brief)
    if not section:
        return ""

    references = []
    seen = set()
    for row in section.group(1).splitlines():
        match = re.match(r"^\|\s*(\d+)\s*\|.*?`([^`]+):(\d+)`\s*\|", row)
        if not match:
            continue
        hits, relative, line = match.groups()
        relative = relative.replace("\\", "/")
        key = (relative, int(line))
        if key not in seen:
            seen.add(key)
            references.append((int(hits), relative, int(line)))
        if len(references) >= max_refs:
            break
    if not references:
        return ""

    root = Path(source_value).resolve()
    out = ["# Source excerpts for observed log statements", "",
           "Read-only excerpts from the selected source folder. References are listed by LogSleuth "
           "as matching the runtime log; they do not by themselves prove causality.", ""]
    for hits, relative, line in references:
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError:  # refuse references that escape the selected source tree
            continue
        try:
            if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        if not lines:
            continue
        start = max(1, line - context_lines)
        end = min(len(lines), line + context_lines)
        out.extend([f"## `{relative}:{line}` — {hits} observed log hit(s)", "", "```text"])
        out.extend(f"{number:>5} | {lines[number - 1]}" for number in range(start, end + 1))
        out.extend(["```", ""])
    return "\n".join(out).rstrip() + "\n" if len(out) > 4 else ""


def step_analyze(run, cfg, ex):
    if not _path(run, "01-jira.md").exists():
        raise RuntimeError(t("err.need_jira"))
    files = [_path(run, "01-jira.md")]
    if _done(run, "understand") and _path(run, "02-understanding.md").exists():
        files.append(_path(run, "02-understanding.md"))
    if _done(run, "log") and _path(run, "03-log-brief.md").exists():
        options = _log_options(run, cfg["log_budget_tokens"])
        cap = min(options["budget_tokens"], cfg["log_budget_tokens"], logsleuth.MAX_BRIEF_TOKENS)
        if _path(run, "03-log-brief.md").stat().st_size > cap:
            raise RuntimeError(t("err.log_reslice"))
        files.append(_path(run, "03-log-brief.md"))
    if (run["inputs"].get("use_codegraph", True) and _done(run, "graph")
            and _path(run, "04-graph.simple.json").exists()):
        files.append(_path(run, "04-graph.simple.json"))
    source_evidence_path = _path(run, "05-source-evidence.md")
    source_evidence_path.unlink(missing_ok=True)
    source_evidence = _source_code_evidence(run) if _done(run, "log") else ""
    if source_evidence:
        _write(run, source_evidence_path.name, source_evidence)
        files.append(source_evidence_path)
    files.append(_instruction("ticket-analysis.md"))
    names = ", ".join(f.name for f in files[:-1])
    answer = _ask_ai(run, cfg, (f"Jira issue {run['issue']}. Apply the attached instruction ticket-analysis.md to the attached "
                          f"evidence ({names}) and return the final analysis in Markdown only."),
                    files, run_dir(run["id"]), session_id=run["session_id"], effort="high", context="long_context")
    ex.check()
    _write(run, "05-analysis.md", storage.strip_chat_wrappers(answer))
    return t("note.analyze", chars=len(answer), sources=len(files) - 1)


def step_format(run, cfg, ex):
    if not _path(run, "05-analysis.md").exists():
        raise RuntimeError(t("err.need_analyze"))
    link = f"{cfg['jira_browse_url'].rstrip('/')}/{run['issue']}"
    raw = _ask_ai(run, cfg, (f"Jira issue {run['issue']} ({link}). Apply the attached report instruction report.md to the attached "
                       "analysis 05-analysis.md. Return only the single fenced json block described in report.md."),
                 [_path(run, "05-analysis.md"), _instruction("report.md")], run_dir(run["id"]), session_id=run["session_id"])
    ex.check()
    _write(run, "06-report.raw.txt", raw)
    refine_log = []

    def log(line):
        refine_log.append(line)
        _log(run, line)

    candidate = storage.last_fence(raw)
    try:
        base = storage.parse_step5_storage_response(candidate)
    except Exception as exc:
        log(t("log.decode", error=exc))
        base = raw
    try:
        report = storage.normalize_storage_report(base)
        log(t("log.local_ok"))
        mode = t("note.format_local")
    except Exception as exc:
        log(t("log.local_fail", error=exc))
        frag_dir = _path(run, "06-refine")
        frag_dir.mkdir(exist_ok=True)
        instruction = _instruction("refine.md").read_text(encoding="utf-8")

        def llm_call(fragment, index, total):
            ex.check()
            frag = frag_dir / f"fragment-{index + 1:02d}.txt"
            frag.write_text(fragment, encoding="utf-8")
            out = _ask_ai(run, cfg, f"{instruction}\nThis is fragment {index + 1} of {total}, attached as {frag.name}.",
                         [frag], run_dir(run["id"]), session_id=run["session_id"], small=True)
            return storage.parse_step5_storage_response(storage.last_fence(out))

        refined = refine_report(base, llm_call, max_input_tokens=6000, max_output_tokens=4000, reserve_tokens=1500,
                                log=log, cancel=ex.check)
        report = storage.normalize_storage_report(refined)
        mode = t("note.format_ai")
    _write(run, "06-refine.log", "\n".join(refine_log) + "\n")
    _write(run, "06-report.xhtml", report)
    _write(run, "06-report.preview.html", _preview_page(report, run["issue"]))
    return t("note.format", mode=mode, chars=len(report))


def _preview_page(storage_xhtml, key):
    return ("<!doctype html><html><head><meta charset='utf-8'><title>" + html.escape(key) + "</title>"
            "<link rel='stylesheet' href='/static/preview.css'></head><body><main class='cf-page'>"
            + preview.to_html(storage_xhtml) + "</main></body></html>")


def step_infographic(run, cfg, ex):
    analysis = _path(run, "05-analysis.md")
    if not analysis.exists():
        raise RuntimeError(t("err.need_analyze"))
    for old in run_dir(run["id"]).glob("06-infographic.*"):
        old.unlink()
    staging = _path(run, "06-infographic")
    shutil.rmtree(staging, ignore_errors=True)
    try:
        if cfg.get("ai_mode") == "copilot":
            raw = _ask_ai(run, cfg, (
                f"Jira issue {run['issue']}. Read 05-analysis.md and return exactly one JSON object, no Markdown or code fence, "
                "with these keys: title (string), summary (string), root_cause (string), timeline (array of objects "
                "with time and event strings), evidence (array of strings), actions (array of strings). Use the same "
                "language as the analysis. Use only supported facts; do not invent missing times or causes. Keep each "
                "list to at most four concise items; use empty arrays when the analysis has no such details."),
                [analysis], run_dir(run["id"]), session_id=run["session_id"], context="long_context")
            ex.check()
            summary = infographic.parse_ai_summary(raw)
            target = _path(run, "06-infographic.svg")
            target.write_text(infographic.render_svg(summary, run["issue"]), encoding="utf-8")
        else:
            images = ai.ask_image(cfg, (
                f"Jira issue {run['issue']}. Using the analysis in this conversation (also attached as 05-analysis.md), "
                "create ONE landscape infographic image that summarises the ticket: problem, key timeline, root cause, "
                "supporting evidence and recommended actions. Write the text in the same language as the analysis. "
                "Generate the image itself; do not reply with text only."),
                [analysis], staging, session_id=run["session_id"])
            ex.check()
            if not images:
                raise RuntimeError(t("err.no_image"))
            source = Path(images[0])
            if not source.is_file():
                raise RuntimeError(t("err.no_image"))
            with source.open("rb") as image_file:
                header = image_file.read(12)
            if header.startswith(b"\x89PNG\r\n\x1a\n"):
                suffix = ".png"
            elif header.startswith(b"\xff\xd8"):
                suffix = ".jpg"
            elif header[:4] == b"RIFF" and header[8:12] == b"WEBP":
                suffix = ".webp"
            else:
                raise RuntimeError(t("err.no_image"))
            target = _path(run, "06-infographic" + suffix)
            shutil.copyfile(source, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    run["infographic_mode"] = cfg.get("ai_mode")
    return t("note.infographic", name=target.name, kb=target.stat().st_size // 1024)


def _infographic(run):
    found = sorted(run_dir(run["id"]).glob("06-infographic.*"))
    return found[0] if found else None


def step_collab(run, cfg, ex):
    if not _path(run, "06-report.xhtml").exists():
        raise RuntimeError(t("err.need_format"))
    report = storage.normalize_storage_report(_path(run, "06-report.xhtml").read_text(encoding="utf-8"))
    space = (cfg.get("collab_space_key") or "").strip()
    if not space:
        raise RuntimeError(t("err.no_space"))
    title = f"{run['issue']} - {run.get('summary') or 'Ticket analysis'}"[:240]
    parent = (cfg.get("collab_parent_id") or "").strip()
    log = lambda m: _log(run, m)
    use_infographic = bool(run.get("inputs", {}).get("use_infographic", False))
    image = _infographic(run) if use_infographic else None
    image_note = ""
    if use_infographic:
        if (not image or not image.is_file() or run["steps"]["infographic"]["status"] != "done"):
            raise RuntimeError(t("err.infographic_required"))
        body = report + collab.image_block(image.name)
        existing_id = collab.find_existing(space, title, parent)
        if existing_id:
            if not collab.attach_image(existing_id, image, log=log):
                raise RuntimeError(t("err.infographic_attach"))
            result = collab.publish(space, title, body, parent, log=log)
            result["page_id"] = result.get("page_id") or existing_id
        else:
            initial = collab.publish(space, title, report, parent, log=log)
            page_id = initial.get("page_id") or collab.find_existing(space, title, parent)
            if not page_id:
                raise RuntimeError(t("err.infographic_attach"))
            try:
                if not collab.attach_image(page_id, image, log=log):
                    raise RuntimeError(t("err.infographic_attach"))
                result = collab.publish(space, title, body, parent, log=log)
            except Exception:
                if initial.get("action") == "created":
                    collab.delete_page(page_id, log=log)
                raise
            result["action"] = initial.get("action", result.get("action"))
            result["page_id"] = result.get("page_id") or page_id
            result["url"] = result.get("url") or initial.get("url", "")
        image_note = " + " + t("note.with_image")
    else:
        result = collab.publish(space, title, report, parent, log=log)
    url = result.get("url") or ""
    if not url and result.get("page_id"):
        base = mcp.status()["collab"]["url"].rstrip("/")
        url = f"{base}/pages/viewpage.action?pageId={result['page_id']}"
    run["collab"] = {"action": result["action"], "page_id": result.get("page_id"), "url": url, "title": title}
    _write(run, "07-collab.json", json.dumps({**run["collab"], "result": result.get("result")}, ensure_ascii=False, indent=2, default=str))
    return (t("note.created") if result["action"] == "created" else t("note.updated")) + (f" {result.get('page_id')}" if result.get("page_id") else "") + image_note


RUNNERS = {"jira": step_jira, "understand": step_understand, "log": step_log, "graph": step_graph,
           "analyze": step_analyze, "format": step_format, "infographic": step_infographic, "collab": step_collab}
APPLICABLE = {
    "jira": lambda r: True,
    "understand": lambda r: bool(r["inputs"]["log_files"]),
    "log": lambda r: bool(r["inputs"]["log_files"]),
    "graph": lambda r: bool(r["inputs"].get("source_dir")) and bool(r["inputs"].get("use_codegraph", True)),
    "analyze": lambda r: True, "format": lambda r: True, "collab": lambda r: True,
    "infographic": lambda r: bool(r["inputs"].get("use_infographic", True)),
}
SKIP_REASON = {"understand": "note.skip_nolog", "log": "note.skip_nolog", "graph": "note.skip_nosrc",
               "infographic": "note.skip_infographic_disabled"}


def _skip_reason(run, step):
    if step == "log" and run["inputs"].get("log_files") and not run["inputs"].get("source_dir"):
        return "note.skip_nocatalog"
    if step == "graph":
        if not run["inputs"].get("use_codegraph", True):
            return "note.skip_graph_disabled"
        if not run["inputs"].get("source_dir"):
            return "note.skip_nosrc"
    return SKIP_REASON[step]
