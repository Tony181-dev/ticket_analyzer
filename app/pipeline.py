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
from .i18n import language_name, relocalize, t
from .evidence import logsleuth, timealign
from .evidence.timestamps import (format_offset, normalize_clock, normalize_focus, normalize_shift, parse_focus,
                                  parse_offset)
from .report import collab, infographic, preview, storage
from .report.refine_pipeline import DIAGRAM_HEADINGS, refine_report

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
# Steps that only add evidence/decoration: in Skip mode a failure here is recorded and the run goes on.
CONTINUABLE_ERROR_STEPS = {"understand", "log", "graph", "infographic"}
GATES = settings.GATES

# (file, label, kind, editable)
ARTIFACTS = {
    "jira": [("01-jira.md", "Ticket", "md", False), ("01-jira.json", "JSON thô", "json", False),
             ("01-attachments.md", "Tệp đính kèm", "md", False), ("01-attachments.json", "Attachments", "json", False)],
    "understand": [("02-understanding.md", "Thời điểm trong ticket", "md", False),
                   ("02-focus.json", "Khung giờ & từ khoá", "json", False)],
    "log": [("03-log-brief.md", "Log brief", "md", False), ("03-log-span.md", "Khoảng giờ & múi giờ log", "md", False),
            ("03-catalog.md", "Catalog log", "md", False), ("03-log-stats.json", "LogSleuth stats", "json", False)],
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


USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")


def _empty_copilot_usage():
    return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0, "reasoning_tokens": 0,
            "model_calls": 0, "updated": None}


def _count(value):
    try:
        return max(0, int(value)) if not isinstance(value, bool) else 0
    except (TypeError, ValueError, OverflowError):
        return 0


def _add_usage(target, delta, calls):
    for key in USAGE_KEYS:
        target[key] = _count(target.get(key)) + _count(delta.get(key))
    target["total_tokens"] = target["input_tokens"] + target["output_tokens"]
    target["model_calls"] = _count(target.get("model_calls")) + calls


def _open_pass(run, start, continued=False):
    """One "lượt chạy": every press of Run adds a record, so re-running a ticket keeps each pass's tokens."""
    passes = run.setdefault("usage_passes", [])
    if passes and passes[-1].get("ended") is None:
        if continued:
            return passes[-1]
        if passes[-1].get("model_calls"):  # left open at a gate that was never answered
            passes[-1]["ended"] = time.time()
        else:
            passes.pop()
    record = {"n": (passes[-1]["n"] + 1 if passes else 1), "started": time.time(), "ended": None, "seconds": 0.0,
              "from": start, "ai_mode": run.get("ai_mode"), "estimated": False, "by_step": {},
              **{k: v for k, v in _empty_copilot_usage().items() if k != "updated"}}
    passes.append(record)
    del passes[:-50]
    return record


def _close_pass(run, paused=False):
    passes = run.get("usage_passes") or []
    if not passes:
        return
    record = passes[-1]
    seconds = 0.0
    for step in ORDER:
        state = run["steps"][step]
        if state.get("started") and state.get("ended") and state["started"] >= record["started"] - 1:
            seconds += max(0.0, _count_float(state["ended"]) - _count_float(state["started"]))
    record["seconds"] = round(seconds, 3)
    if paused:  # waiting at the evidence gate: the same pass continues after the user answers
        return
    record["ended"] = time.time()
    if not record["model_calls"]:  # e.g. only "Publish" was pressed: nothing to account for
        passes.pop()


def _count_float(value):
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0.0


def usage_summary(run):
    """Totals over every pass of this run (measured for Copilot CLI, estimated for M365)."""
    passes = run.get("usage_passes") or []
    total = {k: v for k, v in _empty_copilot_usage().items() if k != "updated"}
    for record in passes:
        _add_usage(total, record, _count(record.get("model_calls")))
    total["passes"] = len(passes)
    total["estimated"] = any(record.get("estimated") for record in passes)
    return total


def _record_usage(run, delta, estimated=False):
    """Add one AI call's tokens to the current pass (and to the step that made the call)."""
    if not isinstance(delta, dict):
        return
    calls = _count(delta.get("model_calls"))
    if not calls:
        return
    passes = run.get("usage_passes") or []
    if passes:
        record = passes[-1]
        _add_usage(record, delta, calls)
        record["estimated"] = bool(record.get("estimated") or estimated)
        step = run.get("current_step")
        if step:
            record["by_step"][step] = _count(record["by_step"].get(step)) + _count(delta.get("input_tokens")) + _count(delta.get("output_tokens"))


def _record_copilot_usage(run, delta):
    """Measured Copilot CLI usage: cumulative for the run, plus the current pass."""
    if run.get("ai_mode") != "copilot" or not isinstance(delta, dict):
        return
    calls = _count(delta.get("model_calls"))
    if not calls:
        return
    usage = run.setdefault("copilot_usage", _empty_copilot_usage())
    _add_usage(usage, delta, calls)
    usage["updated"] = time.time()
    _record_usage(run, delta)
    _save(run)


def _estimate_tokens(prompt, files, answer):
    """M365 reports no usage: a UTF-8 bytes/4 estimate of what was sent and received (the chat's own
    history and system prompt are not visible to the app, so the real input is somewhat larger)."""
    sent = len(str(prompt or "").encode("utf-8"))
    for file in files or []:
        try:
            sent += Path(file).stat().st_size
        except OSError:
            pass
    return {"input_tokens": sent // 4, "output_tokens": len(str(answer or "").encode("utf-8")) // 4, "model_calls": 1}


_NO_RETRY = ("not signed in", "sign-in", "sign in", "login", "not installed", "cancel", "unauthor", "authenticat")


def _retryable(exc):
    """Timeouts, a crashed browser worker or an empty answer are worth one more try; a missing login is not."""
    text = str(exc).lower()
    return not any(marker in text for marker in _NO_RETRY)


def _ask_ai(run, cfg, prompt, files, work_dir, retries=None, **kwargs):
    measured = []
    kwargs["on_usage"] = lambda delta: (measured.append(1), _record_copilot_usage(run, delta))
    if retries is None:
        try:
            retries = max(0, min(3, int(cfg.get("ai_retries", 1))))
        except (TypeError, ValueError):
            retries = 1
    for attempt in range(retries + 1):
        try:
            answer = ai.ask(cfg, prompt, files, work_dir, **kwargs)
            if not measured:  # M365 (and a Copilot CLI without usage events) reports nothing: estimate
                _record_usage(run, _estimate_tokens(prompt, files, answer), estimated=True)
            return answer
        except Exception as exc:
            if executor.cancel.is_set() or attempt >= retries or not _retryable(exc):
                raise
            _log(run, t("log.ai_retry", n=attempt + 1, total=retries, error=str(exc)[:300]))
            if executor.cancel.wait(3):
                raise


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


class Skip(Exception):
    """Raised by a step that turns out to have nothing to do: recorded as skipped, the run goes on."""


class NeedGate(Exception):
    """Raised by the log step in Review/Manual mode when the user has to supply or confirm evidence."""


class NeedTimezone(Exception):
    """Raised by the log step in Review/Manual mode when the ticket does not state the time zone of its
    incident time: the user picks the log clock alignment (suggested / own / no time filter)."""


TZ_MODES = ("auto", "suggested", "manual", "bypass")


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
    run["usage_total"] = usage_summary(run)
    for step in ORDER:
        run["steps"][step]["artifacts"] = _artifacts(run["id"], step)
    tmp = run_dir(run["id"]) / "run.json.tmp"
    tmp.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(run_dir(run["id"]) / "run.json")
    events.publish("run", run=view(run))


def view(run):
    """The run as the browser shows it: stored step notes / errors (written in whatever language was
    active then) rendered again in the current UI language. run.json itself is left as written."""
    if not run:
        return run
    out = dict(run)
    out["steps"] = {step: {**state, "note": relocalize(state.get("note")), "error": relocalize(state.get("error"))}
                    for step, state in (run.get("steps") or {}).items()}
    if isinstance(run.get("pending_gate"), dict):
        out["pending_gate"] = {**run["pending_gate"], "problem": relocalize(run["pending_gate"].get("problem"))}
    return out


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
            run["inputs"].setdefault("gate", "review" if run["inputs"].get("auto_attachments") else "skip")
            run["inputs"].setdefault("log_shift_minutes", None)
            run.setdefault("gate_state", {})
            run.pop("pending_attachments", None)  # pre-gate pause: the evidence gate replaces it
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


def create(issue, log_files=(), source_dir="", auto_attachments=None, download_dir="", use_codegraph=None,
           logsleuth_options=None, use_infographic=None, continue_on_optional_error=None,
           auto_publish_to_collab=None, gate=None, log_shift_minutes=None):
    """New run. Options left as None take the "defaults for a new ticket" from Settings."""
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
    gate = cfg["default_gate"] if gate is None else gate
    if gate not in GATES:
        raise ValueError(t("err.gate"))
    if auto_attachments is None:
        auto_attachments = cfg["default_use_attachments"]
    if use_codegraph is None:
        use_codegraph = cfg["default_use_codegraph"]
    if use_infographic is None:
        use_infographic = cfg["default_use_infographic"]
    if continue_on_optional_error is None:  # Skip mode = keep going without the step that failed
        continue_on_optional_error = gate == "skip"
    if auto_publish_to_collab is None:
        auto_publish_to_collab = not cfg.get("review_before_publish", True)
    log_options = logsleuth.normalize_options(logsleuth_options, cfg["log_budget_tokens"])
    selected_catalog = None
    if log_options["catalog_path"]:
        selected_catalog = {"path": log_options["catalog_path"], "source_path": log_options["catalog_path"],
                            **logsleuth.catalog_info(log_options["catalog_path"])}
    run_id = f"{key}-{datetime.now():%Y%m%d-%H%M%S}"
    while run_dir(run_id).exists():  # two runs of one ticket created within the same second
        run_id += "x"
    run_dir(run_id).mkdir(parents=True, exist_ok=True)
    run = {
        "id": run_id, "issue": key, "summary": "", "created": time.time(), "updated": time.time(),
        "inputs": {"log_files": files, "source_dir": str(source_dir or ""), "auto_attachments": bool(auto_attachments),
                   "gate": gate, "log_shift_minutes": normalize_shift(log_shift_minutes),
                   "download_dir": str(download_dir or ""), "use_codegraph": bool(use_codegraph),
                   "use_infographic": bool(use_infographic),
                   "continue_on_optional_error": bool(continue_on_optional_error),
                   "auto_publish_to_collab": bool(auto_publish_to_collab), "logsleuth": log_options},
        "attachments": [], "gate_state": {},
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
                  auto_publish_to_collab=None, gate=None, log_shift_minutes=...):
    run = get(run_id)
    if not run:
        raise ValueError("Run not found")
    if executor.busy and executor.run_id == run_id:
        raise RuntimeError(t("err.busy"))
    # Validate everything first so a bad value never leaves the run half-updated.
    if gate is not None and gate not in GATES:
        raise ValueError(t("err.gate"))
    if log_files is not None:
        log_files = [str(f) for f in log_files if f and str(f).strip()]
        for f in log_files:
            if not Path(f).is_file():
                raise ValueError(t("err.log_missing", path=f))
    if source_dir is not None:
        source_dir = str(source_dir).strip()
        if source_dir and not Path(source_dir).is_dir():
            raise ValueError(t("err.src_missing", path=source_dir))
    if log_shift_minutes is not ...:
        log_shift_minutes = normalize_shift(log_shift_minutes)
    inputs = run["inputs"]
    if logsleuth_options is not None:
        options = logsleuth.normalize_options({**inputs.get("logsleuth", {}), **logsleuth_options},
                                             settings.load()["log_budget_tokens"])
        previous_catalog = inputs.get("logsleuth", {}).get("catalog_path")
        if options["catalog_path"] and options["catalog_path"] != previous_catalog:
            info = logsleuth.catalog_info(options["catalog_path"])
            run["log_catalog"] = {"path": options["catalog_path"], "source_path": options["catalog_path"], **info}
        inputs["logsleuth"] = options
        _mark_stale(run, "log")
    if gate is not None and gate != inputs.get("gate"):
        inputs["gate"] = gate
        if continue_on_optional_error is None:
            continue_on_optional_error = gate == "skip"
        run.setdefault("gate_state", {}).pop("resolved", None)
        run.pop("pending_gate", None)
        if run["steps"]["log"]["status"] == "waiting":
            run["steps"]["log"].update(status="stale", note="")
        _refresh_skipped(run)
    if auto_attachments is not None and bool(auto_attachments) != bool(inputs.get("auto_attachments")):
        inputs["auto_attachments"] = bool(auto_attachments)
        run.setdefault("gate_state", {}).pop("skip_logs", None)
        _refresh_skipped(run)
        _mark_stale(run, "log")
    if download_dir is not None:
        inputs["download_dir"] = str(download_dir)
    if use_codegraph is not None and bool(use_codegraph) != inputs.get("use_codegraph", True):
        inputs["use_codegraph"] = bool(use_codegraph)
        _mark_stale(run, "graph")
    if use_infographic is not None and bool(use_infographic) != inputs.get("use_infographic", True):
        enabled = bool(use_infographic)
        inputs["use_infographic"] = enabled
        _mark_stale(run, "infographic")
        state = run["steps"]["infographic"]
        if enabled:
            state.update(status="stale", note="", error="")
        else:
            state.update(status="skipped", note=t("note.skip_infographic_disabled"), error="")
    if continue_on_optional_error is not None:
        inputs["continue_on_optional_error"] = bool(continue_on_optional_error)
    if auto_publish_to_collab is not None:
        inputs["auto_publish_to_collab"] = bool(auto_publish_to_collab)
    if log_shift_minutes is not ... and log_shift_minutes != inputs.get("log_shift_minutes"):
        inputs["log_shift_minutes"] = log_shift_minutes
        _mark_stale(run, "log")
    if log_files is not None:
        inputs["log_files"] = log_files
        run.setdefault("gate_state", {}).pop("skip_logs", None)
        _refresh_skipped(run)
        _mark_stale(run, "log")
    if source_dir is not None:
        inputs["source_dir"] = source_dir
        run.pop("log_catalog", None)
        if "logsleuth" in inputs:
            inputs["logsleuth"]["catalog_path"] = ""
        _mark_stale(run, "log")
    _save(run)
    return run


def _stop_step(run, step, state, prior, fresh):
    """Stop pressed while `step` ran. A step that had finished before keeps that result when its files are
    all still there (runners write their outputs at the end): "done" when nothing earlier in this pass
    produced a new result - and then the steps after it get their status back too - else "stale". Without
    a previous result the step goes back to idle ("Stopped")."""
    before = prior.get(step) or {}
    names = [a.get("name") for a in before.get("artifacts") or [] if isinstance(a, dict) and a.get("name")]
    kept = (before.get("status") in ("done", "stale") and bool(names)
            and all((run_dir(run["id"]) / name).exists() for name in names))
    if not kept:
        _finish_step(state, "idle", note=t("note.stopped"))
        return False
    status = "stale" if fresh or before.get("status") == "stale" else "done"
    state.update(status=status, note=" · ".join(x for x in (before.get("note"), t("note.stopped_kept")) if x),
                 error="", started=before.get("started"), ended=before.get("ended"),
                 elapsed_seconds=before.get("elapsed_seconds", 0.0))
    if status == "done":
        for later in ORDER[ORDER.index(step) + 1:]:
            was = (prior.get(later) or {}).get("status")
            if run["steps"][later]["status"] == "stale" and was in ("done", "waiting"):
                run["steps"][later]["status"] = was
    _log(run, t("log.stopped_kept", step=t("step." + step)))
    return True


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


def _refresh_skipped(run):
    """A skipped step that an input change made runnable becomes idle again."""
    for step in ORDER:
        state = run["steps"][step]
        if state["status"] == "skipped" and APPLICABLE[step](run):
            state.update(status="idle", note="", error="")


def resume_step(run):
    """Where "Continue" picks up: the first step still to do. An optional step that failed but was
    passed over (later steps finished) is not retried here - that is what its own Retry button is for."""
    statuses = [run["steps"][step]["status"] for step in ORDER]
    progressed = max((i for i, status in enumerate(statuses) if status in ("done", "waiting")), default=-1)
    for i, status in enumerate(statuses):
        if status in ("idle", "stale", "waiting") or (status == "error" and i > progressed):
            return ORDER[i]
    return None


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
        run.setdefault("gate_state", {})["focus_confirmed"] = True  # a window typed by the user is used as is
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

    def start(self, run_id, start=None, only=False, publish=False, catalog_only=False, until=None, resume=False):
        """Run the pipeline: from `start` (default: step 1), a single step (`only`), up to `until`,
        or from wherever the run stopped (`resume`)."""
        if self.busy:
            raise RuntimeError(t("err.busy"))
        run = get(run_id)
        if not run:
            raise ValueError("Run not found")
        for step in (start, until):
            if step and step not in ORDER:
                raise ValueError(f"Unknown pipeline step: {step}")
        if resume:
            start = resume_step(run)
            if until and (start is None or ORDER.index(start) > ORDER.index(until)):
                start = until
            if start is None:
                raise ValueError(t("err.nothing_to_resume"))
        if start and until and ORDER.index(until) < ORDER.index(start):
            raise ValueError(t("err.until_before_start"))
        self.cancel.clear()
        ai.reset()
        self.run_id = run_id
        self.thread = threading.Thread(target=self._run, args=(run_id, start, only, publish, catalog_only, until), daemon=True)
        self.thread.start()

    def stop(self):
        self.cancel.set()
        ai.cancel()

    def _run(self, run_id, start, only, publish, catalog_only=False, until=None, after_gate=False):
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
        if first == 0 and not after_gate:
            run["gate_state"] = {}  # a pass from step 1 decides the evidence again
            # ... and talks to M365 Copilot in a brand-new conversation: a full run never inherits the
            # previous run's chat context. Re-running from a later step keeps the conversation.
            run["session_id"] = str(uuid.uuid4())
        run.pop("pending_gate", None)
        run.pop("pending_tz", None)
        if first < requested_first:
            _log(run, t("log.rewind_stale", step=STEP_TITLE[ORDER[first]], requested=STEP_TITLE[requested_step]))
        prior = {step: dict(run["steps"][step]) for step in ORDER}  # to keep a result when Stop interrupts a re-run
        fresh = False  # a step finished in this pass: results after it are outdated
        _mark_stale(run, ORDER[first])
        _open_pass(run, ORDER[first], continued=after_gate)
        events.publish("busy", run_id=run_id, busy=True)
        paused = False

        def last(step):  # this step ends the requested range
            return (only and step == requested_step) or step == until

        try:
            for step in ORDER[first:]:
                if self.cancel.is_set():
                    break
                state = run["steps"][step]
                run["current_step"] = step
                if not APPLICABLE[step](run) and not (catalog_only and step == "log"):
                    state.update(status="skipped", note=t(_skip_reason(run, step)), error="")
                    _save(run)
                    if last(step):
                        break
                    continue
                if step == "log" and not catalog_only:
                    if _gate_pending(run):
                        _open_gate(run)
                        paused = True
                        break
                    if (run["inputs"].get("logsleuth", {}).get("mode") == "manual"
                            and (start != "log" or after_gate)):
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
                    _stop_step(run, step, state, prior, fresh)
                    _save(run)
                    break
                except Skip as skipped:
                    _finish_step(state, "skipped", note=str(skipped))
                    _log(run, "– " + t("step." + step) + f": {skipped}")
                    _save(run)
                    if last(step):
                        break
                    continue
                except NeedGate as need:
                    _finish_step(state, "waiting")
                    _log(run, "⏸ " + t("step." + step) + f": {need}")
                    _open_gate(run, str(need))
                    paused = True
                    break
                except NeedTimezone as need:
                    _finish_step(state, "waiting", note=t("note.tz_wait"))
                    _log(run, "⏸ " + t("step." + step) + f": {need}")
                    _save(run)
                    paused = True
                    break
                except Exception as exc:  # surface every failure on the step card
                    if self.cancel.is_set():
                        _stop_step(run, step, state, prior, fresh)
                    else:
                        _finish_step(state, "error", error=str(exc))
                        _log(run, "✗ " + t("step." + step) + f": {exc}")
                        _log(run, traceback.format_exc(), quiet=True)
                    _save(run)
                    if (not self.cancel.is_set() and not last(step) and step in CONTINUABLE_ERROR_STEPS
                            and run["inputs"].get("continue_on_optional_error")):
                        _log(run, t("log.continue_optional_error", step=t("step." + step)))
                        continue
                    break
                _finish_step(state, "done", note=note or "")
                fresh = True
                _log(run, "✓ " + t("step." + step) + (f" — {note}" if note else ""))
                _save(run)
                if last(step):
                    break
        finally:
            run.pop("current_step", None)
            _close_pass(run, paused)
            _save(run)
            events.publish("busy", run_id=run_id, busy=False)

    def check(self):
        if self.cancel.is_set():
            raise Cancelled()

    def fetch_attachments(self, run_id, names, folder=None):
        """Download the chosen attachments now (Jira card menu); log/archive ones are extracted and their
        logs join the log inputs. The pipeline is not started."""
        run = self._idle_run(run_id)
        known = {a["filename"] for a in run.get("attachments") or []}
        names = {n for n in names or [] if n in known}
        if not names:
            raise ValueError(t("err.att_none"))
        if folder is not None:
            run["inputs"]["download_dir"] = str(folder).strip()
        self._download(run, names, "jira")

    def resolve_gate(self, run_id, names=(), folder=None, time_start=None, time_end=None, shift_minutes=...,
                     skip_logs=False):
        """The user's answer at the evidence gate (Review / Manual): which attachments to download, the
        incident window and the log clock shift - or "go on without logs". The pipeline then continues."""
        run = self._idle_run(run_id)
        clocks = []
        for value in (time_start, time_end):
            clock = normalize_clock(value) if isinstance(value, str) and value.strip() else None
            if isinstance(value, str) and value.strip() and clock is None:
                raise ValueError(t("err.time_format"))
            clocks.append(clock)
        shift = ... if shift_minutes is ... else normalize_shift(shift_minutes)
        known = {a["filename"] for a in run.get("attachments") or []}
        names = set() if skip_logs else {n for n in names or [] if n in known}
        if folder is not None:
            run["inputs"]["download_dir"] = str(folder).strip()
        run["focus"] = _clean_focus({**(run.get("focus") or {}), "time_start": clocks[0], "time_end": clocks[1]})
        _write(run, "02-focus.json", json.dumps(run["focus"], ensure_ascii=False, indent=2))
        if shift is not ...:
            run["inputs"]["log_shift_minutes"] = shift
        run.setdefault("gate_state", {}).update(resolved=True, focus_confirmed=True, skip_logs=bool(skip_logs))
        run.pop("pending_gate", None)
        run["steps"]["log"].update(status="idle", note="", error="")
        _log(run, t("log.gate_skip") if skip_logs else t("log.gate_ok", n=len(names)))
        _save(run)
        self._download(run, names, "log", resume=True)

    def resolve_timezone(self, run_id, mode, shift_minutes=None, time_start=None, time_end=None,
                         ticket_utc_offset=None):
        """The user's answer when the ticket does not state its time zone: `suggested` / `manual` apply a log
        clock shift (minutes, log - ticket), `auto` lets the app align by itself, `bypass` drops the time
        filter (full log range). The log step then runs again with that decision."""
        run = self._idle_run(run_id)
        if mode not in TZ_MODES:
            raise ValueError(t("err.tz_mode"))
        decision = {"mode": mode}
        if mode in ("suggested", "manual"):
            shift = normalize_shift(shift_minutes)
            if shift is None:
                raise ValueError(t("err.tz_shift"))
            decision["shift_minutes"] = shift
        focus = dict(run.get("focus") or {})
        if ticket_utc_offset not in (None, ""):
            offset = parse_offset(ticket_utc_offset)
            if offset is None:
                raise ValueError(t("err.tz_offset"))
            focus["ticket_utc_offset"] = decision["ticket_utc_offset"] = format_offset(offset)
        changed_window = False
        for key, value in (("time_start", time_start), ("time_end", time_end)):
            if isinstance(value, str) and value.strip():
                clock = normalize_clock(value)
                if clock is None:
                    raise ValueError(t("err.time_format"))
                changed_window = changed_window or clock != focus.get(key)
                focus[key] = clock
        if changed_window or "ticket_utc_offset" in decision:
            run["focus"] = _clean_focus(focus)
            _write(run, "02-focus.json", json.dumps(run["focus"], ensure_ascii=False, indent=2))
            run.setdefault("gate_state", {})["focus_confirmed"] = True
        run.setdefault("gate_state", {})["tz"] = decision
        run.pop("pending_tz", None)
        run["steps"]["log"].update(status="idle", note="", error="")
        how = t("tz." + mode) + (f" ({_shift_text(decision['shift_minutes'])})" if "shift_minutes" in decision else "")
        _log(run, t("log.tz_decided", how=how))
        _save(run)
        self.start(run_id, "log")

    def _idle_run(self, run_id):
        if self.busy:
            raise RuntimeError(t("err.busy"))
        run = get(run_id)
        if not run:
            raise ValueError("Run not found")
        return run

    def _download(self, run, names, step, resume=False):
        """Background job: fetch `names` with progress on `step`; with `resume`, continue the pipeline at
        the log step afterwards, or reopen the gate when the download fails or is stopped."""
        run_id = run["id"]
        self.cancel.clear()
        ai.reset()
        self.run_id = run_id
        available = [a["filename"] for a in run.get("attachments") or []]
        selected = set(names)
        names = attachments.expand_7z_volumes(selected, available)
        added_volumes = sum(1 for name in names - selected if attachments.is_7z_volume(name))
        analyze = {a["filename"] for a in run.get("attachments") or [] if a["filename"] in names and _analyzable(a)}
        if names:  # visible at once, before the worker thread has fetched anything
            queued = [a for a in run.get("attachments") or [] if a["filename"] in names]
            run["att_progress"] = {"phase": "download", "done": 0, "total": len(names), "name": "",
                                   "files": list(_progress_files(queued).values())}
            _save(run)

        def job():
            events.publish("busy", run_id=run_id, busy=True)
            state = run["steps"][step]
            before = {k: state[k] for k in ("status", "note", "error")}
            ok = False
            try:
                if added_volumes:
                    _log(run, t("log.att_split_volumes", selected=len(selected), added=added_volumes, total=len(names)))
                added = _fetch_attachments(run, names, analyze, self.check, step) if names else 0
                state.update(before)
                if step == "jira":
                    state["note"] = _jira_note(run, added)
                    if added:
                        run.setdefault("gate_state", {}).pop("skip_logs", None)
                        _refresh_skipped(run)
                        _mark_stale(run, "log")
                ok = True
            except Cancelled:
                state.update(before)
                _log(run, t("note.stopped"))
                if resume:
                    _open_gate(run)
            except Exception as exc:
                state.update(before)
                _log(run, f"✗ {exc}")
                if resume:
                    _open_gate(run, t("err.att_download", error=exc))
            finally:
                run.pop("att_progress", None)
                _save(run)
                if ok and resume:
                    self._run(run_id, "log", False, False, after_gate=True)
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


def _lang_code():
    code = settings.load().get("lang")
    return code if code in DIAGRAM_HEADINGS else "vi"


def _write_in():
    """The report, analysis and infographic follow the UI language (Vietnamese / English / Korean)."""
    return (f"Write all prose in {language_name()}; keep code, log lines, identifiers, file paths and error "
            "messages verbatim.")


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
    listing = _attachment_listing(run, cfg)
    if listing:
        _write(run, "01-attachments.md", listing)
    else:
        _path(run, "01-attachments.md").unlink(missing_ok=True)
    return _jira_note(run)


def _size(n):
    n = int(n or 0)
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{max(1, n // 1024)} KB" if n else "?"


def _analyzable(attachment):
    """Could hold log text: anything except obvious media / documents / binaries."""
    return (attachment.get("kind") != "other"
            or Path(str(attachment.get("filename", "")).lower()).suffix not in attachments.SKIP_EXT)


def _log_candidates(run):
    return [a for a in run.get("attachments") or [] if _analyzable(a)]


def _attachment_listing(run, cfg):
    """Markdown table of the ticket's attachments: what the AI chooses from (and what the user sees)."""
    items = run.get("attachments") or []
    if not items:
        return ""
    cap = int(cfg.get("auto_download_max_mb") or 0)
    lines = [f"# Attachments of {run['issue']}", "",
             f"Download limit for automatic selection: {f'{cap} MB in total' if cap else 'none'}.", "",
             "| filename | size | uploaded | type |", "|---|---:|---|---|"]
    for a in items:
        kind = a["kind"] if _analyzable(a) else "media/document (not a log)"
        lines.append(f"| {a['filename']} | {_size(a.get('size'))} | {a.get('created') or '?'} | {kind} |")
    return "\n".join(lines) + "\n"


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


def _progress_files(items):
    """{filename: progress row} for the per-file progress bars, in download order."""
    return {a["filename"]: {"name": a["filename"], "size": attachments.parse_size(a.get("size")), "state": "queued",
                            "pct": 0} for a in items}


def _fetch_attachments(run, names, analyze, cancel, step="jira"):
    """Download the named attachments; logs from `analyze` ones join the log inputs. Returns #log files added.

    Progress is shown on `step`: the Jira card for a manual download, the Log card when the files are
    fetched as evidence (evidence gate / Skip mode)."""
    items = [a for a in run.get("attachments") or [] if a["filename"] in names]
    if not items:
        return 0
    folder = attachment_folder(run)
    _log(run, t("log.att_download", n=len(items), folder=folder))
    state = run["steps"][step]
    state.update(status="running", error="")
    files = _progress_files(items)
    finished = {"downloaded", "present", "failed", "extracted"}

    def progress(phase, done, total, name, pct=None):
        entry = files.get(name)
        if entry:  # one row per file: queued -> download (pct) -> done / present / failed -> extract -> ready
            entry.update({"download": {"state": "download", "pct": pct if pct is not None else entry["pct"] or 0},
                          "downloaded": {"state": "done", "pct": 100}, "present": {"state": "present", "pct": 100},
                          "failed": {"state": "failed"}, "extract": {"state": "extract"},
                          "extracted": {"state": "ready"}}.get(phase, {}))
        overall = "extract" if phase.startswith("extract") else "download"
        count = done + 1 if phase in finished else done
        run["att_progress"] = {"phase": overall, "done": count, "total": total, "name": name,
                               "pct": pct if phase == "download" else None, "files": list(files.values())}
        state["note"] = t("note.att_" + overall, done=count, total=total, name=name)
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


# ---------------------------------------------------------------------------
# evidence gate: which attachments, which time window
# ---------------------------------------------------------------------------
def _attachment_picks(run):
    """(picks, source): the attachments to fetch as log evidence - the AI's choice when step 2 made one,
    otherwise every log/archive attachment, newest first."""
    candidates = {a["filename"].casefold(): a for a in _log_candidates(run)}
    chosen = (run.get("focus") or {}).get("attachments") if _done(run, "understand") else None
    if chosen is not None:
        picks = [{"filename": candidates[c["filename"].casefold()]["filename"], "reason": c.get("reason", "")}
                 for c in chosen if c["filename"].casefold() in candidates]
        return picks, "ai"
    ordered = sorted((a for a in candidates.values() if a["kind"] in ("log", "archive")
                      and not attachments.is_7z_volume_continuation(a["filename"])),
                     key=lambda a: str(a.get("created") or ""), reverse=True)
    return [{"filename": a["filename"], "reason": ""} for a in ordered], "heuristic"


def _within_budget(run, picks, cap_mb):
    """Skip mode: the picks that fit the download limit, in order (a split archive counts with all its parts)."""
    available = [a["filename"] for a in run.get("attachments") or []]
    size = {a["filename"]: attachments.parse_size(a.get("size")) for a in run.get("attachments") or []}
    left = int(cap_mb) * 1024 ** 2 if cap_mb else None
    chosen, dropped = [], []
    for pick in picks:
        need = sum(size.get(n, 0) for n in attachments.expand_7z_volumes({pick["filename"]}, available))
        if left is not None and need > left:
            dropped.append(pick["filename"])
            continue
        if left is not None:
            left -= need
        chosen.append(pick["filename"])
    return chosen, dropped


def _gate_pending(run):
    return (run["inputs"].get("gate") in ("review", "manual")
            and not (run.get("gate_state") or {}).get("resolved"))


def _open_gate(run, problem=""):
    """Pause at the log step until the user confirms the evidence (POST /api/runs/<id>/gate)."""
    picks, source = _attachment_picks(run) if run["inputs"].get("auto_attachments") else ([], "none")
    mode = run["inputs"].get("gate") if run["inputs"].get("gate") in ("review", "manual") else "review"
    if mode == "manual" and source == "heuristic":
        picks = []  # Manual: nothing is pre-selected by the app
    run["pending_gate"] = {"mode": mode, "problem": problem, "source": source,
                           "suggested": [pick["filename"] for pick in picks],
                           "reasons": {pick["filename"]: pick["reason"] for pick in picks if pick["reason"]}}
    run.setdefault("gate_state", {}).pop("resolved", None)
    run["steps"]["log"].update(status="waiting", note=problem or t("note.gate_" + mode), error="")
    _save(run)


def _acquire_logs(run, cfg, ex):
    """Skip mode: download the chosen attachments by itself. A failed download is logged, never fatal -
    the run simply continues with whatever evidence it has."""
    if not run["inputs"].get("auto_attachments") or not _log_candidates(run):
        return
    picks, source = _attachment_picks(run)
    if not picks:
        _log(run, t("log.att_none_picked"))
        return
    cap = cfg.get("auto_download_max_mb") or 0
    names, dropped = _within_budget(run, picks, cap)
    for name in dropped:
        _log(run, t("log.att_over_cap", name=name, mb=cap))
    done = {a["filename"] for a in run.get("attachments") or [] if a.get("analyzed")}
    names = [name for name in names if name not in done]
    if not names:
        return
    _log(run, t("log.att_auto_" + source, names=", ".join(names)))
    wanted = attachments.expand_7z_volumes(set(names), [a["filename"] for a in run["attachments"]])
    try:
        _fetch_attachments(run, wanted, wanted, ex.check, "log")
    except Cancelled:
        raise
    except Exception as exc:
        ex.check()
        _log(run, "⚠ " + t("log.att_failed", error=exc))
    finally:
        run["steps"]["log"]["note"] = ""


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
    if not _path(run, "01-jira.md").exists():
        raise RuntimeError(t("err.need_jira"))
    listing = _path(run, "01-attachments.md")
    choose = bool(run["inputs"].get("auto_attachments") and _log_candidates(run) and listing.exists())
    files = [_path(run, "01-jira.md")] + ([listing] if choose else []) + [_instruction("understand.md")]
    task = ("extract the explicitly stated incident time and its time zone"
            + (", and choose which attachments from 01-attachments.md are worth downloading as log evidence" if choose else ""))
    answer = _ask_ai(run, cfg, (f"Jira issue {run['issue']}. Read only the attached Jira ticket"
                                + (" and the attachment list" if choose else "") + f" and follow understand.md to {task}. "
                                "Do not inspect logs or source code."),
                     files, run_dir(run["id"]), session_id=run["session_id"])
    ex.check()
    answer = storage.strip_chat_wrappers(answer)
    _write(run, "02-understanding.md", answer)
    focus = _parse_focus(answer)
    if not choose:
        focus["attachments"] = None
    run["focus"] = focus
    _write(run, "02-focus.json", json.dumps(focus, ensure_ascii=False, indent=2))
    gate_state = run.setdefault("gate_state", {})
    gate_state.pop("focus_confirmed", None)  # a fresh AI reading replaces a window typed earlier
    gate_state.pop("resolved", None)
    gate_state.pop("tz", None)               # ... and the time-zone decision that went with it
    run.pop("pending_tz", None)
    notes = []
    if focus.get("time_start") or focus.get("time_end"):
        notes.append(t("note.window", start=focus.get("time_start") or "…", end=focus.get("time_end") or "…",
                       confidence=focus.get("confidence") or "?")
                     + (f" {focus['ticket_utc_offset']}" if focus.get("ticket_utc_offset") else ""))
        if not focus.get("ticket_utc_offset"):
            notes.append(t("note.tz_unknown"))
    else:
        notes.append(t("note.nowindow"))
    if choose and focus.get("attachments") is not None:
        offered = {a["filename"].casefold() for a in _log_candidates(run)}
        notes.append(t("note.picks", n=sum(1 for pick in focus["attachments"] if pick["filename"].casefold() in offered)))
    return " · ".join(notes)


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


def _align_window(run, cfg, ex, files, focus, manual_shift=None, ask_user=False):
    """Turn the ticket's incident window into the clock the logs print (see app/evidence/timealign.py).

    With `ask_user` the alignment is only computed as a suggestion: the evidence (log time ranges and the
    candidate alignments) is stored as run["pending_tz"] and NeedTimezone pauses the step for the user."""
    try:
        spans = logsleuth.span(files, cancel=ex.check)
    except Cancelled:
        raise
    except Exception as exc:  # the alignment is a refinement: without spans the window is used as written
        ex.check()
        _log(run, f"log span: {exc}", quiet=True)
        spans = []
    try:
        jira_offset = timealign.jira_utc_offset(_path(run, "01-jira.json").read_text(encoding="utf-8"))
    except OSError:
        jira_offset = None
    report = _path(run, "03-log-span.md")

    def ask(options):
        report.write_text(timealign.report(focus, spans, run.get("attachments"), jira_offset, options), encoding="utf-8")
        answer = _ask_ai(run, cfg, (f"Jira issue {run['issue']}. Follow log-time.md with the attached 03-log-span.md "
                                    "to decide which clock the log files print relative to the ticket's incident time. "
                                    "Return only the JSON block described in log-time.md."),
                         [report, _path(run, "01-jira.md"), _instruction("log-time.md")], run_dir(run["id"]),
                         session_id=run["session_id"], retries=0)
        ex.check()
        blocks = re.findall(r"```json\s*\n?([\s\S]*?)```", answer, flags=re.I)
        data = json.loads(blocks[-1] if blocks else answer)
        return data if isinstance(data, dict) else {}

    use_ai = run["inputs"].get("gate") != "manual"
    result = timealign.align(focus, spans, jira_offset, manual_shift, ask if use_ai else None)
    chosen = (f"\n## {'Suggested (waiting for your choice)' if ask_user else 'Applied'}\n\n"
              f"- Shift: **{result['shift_minutes']} min** ({result['source']})\n"
              f"- Window in log clock: `{result['time_start'] or '…'}` → `{result['time_end'] or '…'}`\n"
              f"- Why: {result['reason']}\n")
    report.write_text(timealign.report(focus, spans, run.get("attachments"), jira_offset, result["candidates"]) + chosen,
                      encoding="utf-8")
    if ask_user:
        keys = ("shift_minutes", "source", "time_start", "time_end", "covered")
        run["pending_tz"] = {
            "ticket_start": focus.get("time_start"), "ticket_end": focus.get("time_end"),
            "incident_date": focus.get("incident_date"), "jira_offset": format_offset(jira_offset) or None,
            "spans": [{"name": s.get("name"), "format": s.get("format"), "first": s.get("first"), "last": s.get("last"),
                       "first_ts": s.get("firstTs") or s.get("first"), "last_ts": s.get("lastTs") or s.get("last"),
                       "wraps": bool(s.get("wraps"))} for s in spans[:30]],
            "candidates": [{k: option.get(k) for k in keys} for option in result["candidates"]],
            "suggested": {**{k: result.get(k) for k in keys}, "reason": result["reason"]},
        }
        raise NeedTimezone(t("log.tz_wait"))
    _log(run, t("log.align", shift=_shift_text(result["shift_minutes"]), start=result["time_start"] or "…",
               end=result["time_end"] or "…", why=t("align." + result["source"])))
    return result


def _shift_text(minutes):
    hours = minutes / 60
    return ("+" if minutes > 0 else "") + (f"{hours:.0f}" if minutes % 60 == 0 else f"{hours:.2f}".rstrip("0")) + "h"


def step_log(run, cfg, ex):
    interactive = run["inputs"].get("gate") in ("review", "manual")
    if not interactive:
        _acquire_logs(run, cfg, ex)
    if not run["inputs"]["log_files"]:
        if interactive:
            raise NeedGate(t("err.no_logs_available"))
        raise Skip(t("note.skip_no_logs_found"))
    try:
        files = _ready_logs(run, ex)
    except RuntimeError as exc:
        if interactive:
            raise NeedGate(str(exc))
        raise
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
        if not (_done(run, "understand") or (run.get("gate_state") or {}).get("focus_confirmed")):
            raise FileNotFoundError("Step 2 did not complete")
        focus = _clean_focus(json.loads(_path(run, "02-focus.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        focus = _clean_focus(None)
    alignment = None
    tz = (run.get("gate_state") or {}).get("tz") or {}
    if not manual and focus["filter_mode"] == "window" and tz.get("mode") == "bypass":
        _log(run, t("log.tz_bypass"))
        focus = _clean_focus({**focus, "time_start": None, "time_end": None})
    if manual:
        _path(run, "03-log-span.md").unlink(missing_ok=True)
        focus = _clean_focus({"time_start": options["time_start"] if options["time_enabled"] else None,
                              "time_end": options["time_end"] if options["time_enabled"] else None})
    elif focus["filter_mode"] == "full":
        _path(run, "03-log-span.md").unlink(missing_ok=True)
        if catalog:
            # No timestamp contract: match ALL catalog services over the full logs. Without a
            # catalog the ticket keywords are the only ticket-specific seed, so they are kept.
            focus["keywords"] = []
    else:
        # The ticket time is only as good as its time zone. A shift typed on the run, or the user's
        # time-zone decision, wins; when the ticket states no zone, Review/Manual stop and ask.
        shift = run["inputs"].get("log_shift_minutes")
        if shift is None and tz.get("mode") in ("suggested", "manual"):
            shift = tz.get("shift_minutes")
        unknown_zone = not focus.get("ticket_utc_offset") and shift is None and not tz
        ask_user = unknown_zone and run["inputs"].get("gate") in ("review", "manual")
        alignment = _align_window(run, cfg, ex, files, focus, shift, ask_user)
        if unknown_zone:
            _log(run, "⚠ " + t("log.tz_auto_unknown"))
        focus = {**focus, "time_start": alignment["time_start"], "time_end": alignment["time_end"]}
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
    stats.update(mode=options["mode"], requestedFocus=focus, fullTimeFallback=fallback, timeAlignment=alignment)
    run["log_stats"] = stats
    _write(run, "03-log-stats.json", json.dumps(stats, ensure_ascii=False, indent=2))
    note = t("note.log", total=stats["totalLines"], kept=stats["kept"], tokens=stats["tokens"])
    if not catalog:
        note += " · " + t("note.log_no_catalog")
    if window:
        note += f", {window['start']}–{window['end']}"
        if alignment and alignment["shift_minutes"]:
            note += " (" + t("note.log_shift", shift=_shift_text(alignment["shift_minutes"])) + ")"
    elif fallback:
        note += " · " + t("note.log_full_fallback")
    return note


_STOPWORDS = {"with", "when", "from", "that", "this", "after", "before", "does", "cannot", "into", "have", "been",
              "issue", "error", "fail", "fails", "failed", "failure", "problem", "ticket", "không", "khong", "được",
              "while", "then", "there", "their", "should", "would", "could", "about", "still", "sometimes"}


def _graph_focus(run):
    """What this ticket is about, for ordering the AI's view of the code graph: ticket keywords and
    the source files LogSleuth matched in the log."""
    focus = run.get("focus") or {}
    terms = [*(focus.get("keywords") or []), *(focus.get("services") or [])]
    terms += [w for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", run.get("summary") or "") if w.lower() not in _STOPWORDS]
    files = []
    if _done(run, "log"):
        try:
            brief = _path(run, "03-log-brief.md").read_text(encoding="utf-8")
            files = sorted({m.replace("\\", "/") for m in re.findall(r"`([^`\s]+\.\w+):\d+`", brief)})
        except OSError:
            pass
    seen, unique = set(), []
    for term in terms:
        key = term.strip().lower()
        if len(key) >= 3 and key not in seen:
            seen.add(key)
            unique.append(term.strip())
    return {"terms": unique[:40], "files": files[:60]}


def step_graph(run, cfg, ex):
    out, meta = codegraph.build(run["inputs"]["source_dir"], cfg["graph_token_budget"], cancel=ex.check)
    codegraph.copy_into(out, run_dir(run["id"]))
    if not meta.get("nodes"):
        raise Skip(t("note.graph_empty"))
    note = t("note.graph", files=meta.get("files", 0), nodes=meta.get("nodes", 0), edges=meta.get("edges", 0))
    focus = _graph_focus(run)
    if focus["terms"] or focus["files"]:
        matched = codegraph.write_focused(out, _path(run, "04-graph.simple.json"), cfg["graph_token_budget"], focus)
        if matched:
            note += " · " + t("note.graph_focus", n=matched)
    return note


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
                          f"evidence ({names}) and return the final analysis in Markdown only. {_write_in()}"),
                    files, run_dir(run["id"]), session_id=run["session_id"], effort="high", context="long_context")
    ex.check()
    _write(run, "05-analysis.md", storage.strip_chat_wrappers(answer))
    return t("note.analyze", chars=len(answer), sources=len(files) - 1)


def step_format(run, cfg, ex):
    if not _path(run, "05-analysis.md").exists():
        raise RuntimeError(t("err.need_analyze"))
    link = f"{cfg['jira_browse_url'].rstrip('/')}/{run['issue']}"
    raw = _ask_ai(run, cfg, (f"Jira issue {run['issue']} ({link}). Apply the attached report instruction report.md to the attached "
                       "analysis 05-analysis.md. Return only the single fenced json block described in report.md. "
                       f"Report language: {language_name()} - {_write_in()} Title the diagram section "
                       f"'{DIAGRAM_HEADINGS[_lang_code()]}'."),
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
    run["report_lang"] = _lang_code()
    return t("note.format", mode=mode, chars=len(report))


def _preview_page(storage_xhtml, key):
    return ("<!doctype html><html><head><meta charset='utf-8'><title>" + html.escape(key) + "</title>"
            "<link rel='stylesheet' href='/static/preview.css'></head><body><main class='cf-page'>"
            + preview.to_html(storage_xhtml) + "</main></body></html>")


_INFOGRAPHIC_JSON_PROMPT = (
    "Apply the attached instruction infographic.md (section 'JSON contract') to 05-analysis.md and return "
    "exactly one JSON object, no Markdown or code fence. Keys: title, subtitle, severity, confidence, summary, "
    "root_cause, key_facts, timeline, evidence, code_locations, actions. Use only supported facts; use empty "
    "strings or arrays when the analysis has no such details.")


def _vector_infographic(run, cfg, ex, analysis):
    """Text-only route: the AI returns the facts as JSON and the app draws the SVG itself."""
    raw = _ask_ai(run, cfg, f"Jira issue {run['issue']}. " + _INFOGRAPHIC_JSON_PROMPT + " " + _write_in(),
                  [analysis, _instruction("infographic.md")], run_dir(run["id"]), session_id=run["session_id"],
                  context="long_context")
    ex.check()
    summary = infographic.parse_ai_summary(raw)
    target = _path(run, "06-infographic.svg")
    target.write_text(infographic.render_svg(summary, run["issue"]), encoding="utf-8")
    return target


def _generated_image(run, cfg, ex, analysis, staging):
    """M365 image generation; raises when the model returns no usable image (quota, refusal, timeout)."""
    images = ai.ask_image(cfg, (
        f"Jira issue {run['issue']}. Using the analysis in this conversation (also attached as 05-analysis.md), "
        "create ONE 16:9 landscape infographic image that summarises the ticket: problem, key facts, timeline, "
        "root cause, supporting evidence and recommended actions. Follow the attached infographic.md "
        "(section 'Image route') for layout, style and text rules. " + _write_in() + " "
        "Generate the image itself; do not reply with text only."),
        [analysis, _instruction("infographic.md")], staging, session_id=run["session_id"])
    ex.check()
    source = Path(images[0]) if images else None
    if not source or not source.is_file():
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
    return target


def step_infographic(run, cfg, ex):
    analysis = _path(run, "05-analysis.md")
    if not analysis.exists():
        raise RuntimeError(t("err.need_analyze"))
    for old in run_dir(run["id"]).glob("06-infographic.*"):
        old.unlink()
    staging = _path(run, "06-infographic")
    shutil.rmtree(staging, ignore_errors=True)
    fallback = ""
    try:
        if cfg.get("ai_mode") == "copilot":
            target = _vector_infographic(run, cfg, ex, analysis)
        else:
            try:
                target = _generated_image(run, cfg, ex, analysis, staging)
            except Cancelled:
                raise
            except Exception as exc:
                # M365 image generation fails now and then (daily image quota, refusals, timeouts).
                # The same facts drawn locally are better than no infographic at all.
                ex.check()
                _log(run, "⚠ " + t("log.infographic_fallback", error=str(exc)[:300]))
                target = _vector_infographic(run, cfg, ex, analysis)
                fallback = " · " + t("note.infographic_fallback")
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    run["infographic_mode"] = cfg.get("ai_mode")
    return t("note.infographic", name=target.name, kb=max(1, target.stat().st_size // 1024)) + fallback


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
    if run.get("report_lang") and run["report_lang"] != _lang_code():
        _log(run, "⚠ " + t("log.report_lang", was=language_name(run["report_lang"]), now=language_name()))
    title = f"{run['issue']} - {run.get('summary') or 'Ticket analysis'}"[:240]
    parent = (cfg.get("collab_parent_id") or "").strip()
    log = lambda m: _log(run, m)
    wanted = bool(run.get("inputs", {}).get("use_infographic", False))
    # The report is the deliverable; the infographic decorates it. A missing or unattachable image
    # never blocks publishing - the note says so and the step can be re-run once the image exists.
    image = _infographic(run) if wanted and run["steps"]["infographic"]["status"] == "done" else None
    if image is not None and not image.is_file():
        image = None
    attached = False
    if image:
        body = report + collab.image_block(image.name)
        existing_id = collab.find_existing(space, title, parent)
        if existing_id:
            attached = collab.attach_image(existing_id, image, log=log)
            result = collab.publish(space, title, body if attached else report, parent, log=log)
            result["page_id"] = result.get("page_id") or existing_id
        else:
            result = collab.publish(space, title, report, parent, log=log)
            page_id = result.get("page_id") or collab.find_existing(space, title, parent)
            attached = bool(page_id) and collab.attach_image(page_id, image, log=log)
            if attached:
                second = collab.publish(space, title, body, parent, log=log)
                result = {**second, "action": result.get("action", second.get("action")),
                          "page_id": second.get("page_id") or page_id, "url": second.get("url") or result.get("url", "")}
    else:
        result = collab.publish(space, title, report, parent, log=log)
    if attached:
        image_note = " + " + t("note.with_image")
    elif image:
        image_note = " · " + t("note.image_failed")
    elif wanted:
        image_note = " · " + t("note.no_image")
        log("⚠ " + t("note.no_image"))
    else:
        image_note = ""
    url = result.get("url") or ""
    if not url and result.get("page_id"):
        base = mcp.status()["collab"]["url"].rstrip("/")
        url = f"{base}/pages/viewpage.action?pageId={result['page_id']}"
    run["collab"] = {"action": result["action"], "page_id": result.get("page_id"), "url": url, "title": title,
                     "infographic": attached}
    _write(run, "07-collab.json", json.dumps({**run["collab"], "result": result.get("result")}, ensure_ascii=False, indent=2, default=str))
    return (t("note.created") if result["action"] == "created" else t("note.updated")) + (f" {result.get('page_id')}" if result.get("page_id") else "") + image_note


RUNNERS = {"jira": step_jira, "understand": step_understand, "log": step_log, "graph": step_graph,
           "analyze": step_analyze, "format": step_format, "infographic": step_infographic, "collab": step_collab}
def _wants_logs(run):
    """Log evidence is expected: files were chosen, or the ticket has attachments that may hold logs."""
    inputs = run["inputs"]
    return bool(inputs["log_files"]) or (bool(inputs.get("auto_attachments")) and bool(_log_candidates(run)))


APPLICABLE = {
    "jira": lambda r: True,
    # Manual gate: the user types the time window, so the AI triage is not asked.
    "understand": lambda r: r["inputs"].get("gate") != "manual" and _wants_logs(r),
    "log": lambda r: _wants_logs(r) and not (r.get("gate_state") or {}).get("skip_logs"),
    "graph": lambda r: bool(r["inputs"].get("source_dir")) and bool(r["inputs"].get("use_codegraph", True)),
    "analyze": lambda r: True, "format": lambda r: True, "collab": lambda r: True,
    "infographic": lambda r: bool(r["inputs"].get("use_infographic", True)),
}
SKIP_REASON = {"understand": "note.skip_nolog", "log": "note.skip_nolog", "graph": "note.skip_nosrc",
               "infographic": "note.skip_infographic_disabled"}


def _skip_reason(run, step):
    inputs = run["inputs"]
    if step in ("understand", "log"):
        if step == "log" and (run.get("gate_state") or {}).get("skip_logs"):
            return "note.skip_logs_user"
        if step == "understand" and inputs.get("gate") == "manual" and _wants_logs(run):
            return "note.skip_manual_gate"
        if inputs.get("auto_attachments") and not inputs["log_files"] and run["steps"]["jira"]["status"] == "done":
            return "note.skip_no_attached_logs"
    if step == "graph":
        if not inputs.get("use_codegraph", True):
            return "note.skip_graph_disabled"
        if not inputs.get("source_dir"):
            return "note.skip_nosrc"
    return SKIP_REASON[step]
