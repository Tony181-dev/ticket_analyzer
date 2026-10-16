"""The 7-step workflow: Jira -> AI understand -> Log brief -> Code graph ->
AI analysis -> AI format -> Collab.

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

from . import ai, attachments, events, mcp, paths, settings
from .evidence import graph as codegraph
from .i18n import t
from .evidence import logsleuth
from .report import collab, preview, storage
from .report.refine_pipeline import refine_report

STEPS = [
    {"id": "jira", "title": "Jira ticket", "sub": "MCP jira_get_issue"},
    {"id": "understand", "title": "AI hiểu ticket", "sub": "Chọn khung giờ log"},
    {"id": "log", "title": "Log brief", "sub": "LogSleuth"},
    {"id": "graph", "title": "Code graph", "sub": "CodeGraph"},
    {"id": "analyze", "title": "AI phân tích", "sub": "Phân tích tổng hợp"},
    {"id": "format", "title": "AI format", "sub": "Confluence XHTML"},
    {"id": "collab", "title": "Đăng Collab", "sub": "create / update"},
]
ORDER = [s["id"] for s in STEPS]

# (file, label, kind, editable)
ARTIFACTS = {
    "jira": [("01-jira.md", "Ticket", "md", False), ("01-jira.json", "JSON thô", "json", False),
             ("01-attachments.json", "Attachments", "json", False)],
    "understand": [("02-understanding.md", "Hiểu ticket", "md", False), ("02-focus.json", "Khung giờ & từ khoá", "json", False),
                   ("02-log-span.md", "Dải giờ trong log", "md", False)],
    "log": [("03-log-brief.md", "Log brief", "md", False), ("03-catalog.md", "Catalog log", "md", False)],
    "graph": [("04-brain.html", "Sơ đồ tương tác", "html", False), ("04-GRAPH_REPORT.md", "Report", "md", False),
              ("04-graph.simple.json", "graph.simple.json (AI)", "json", False), ("04-graph.json", "graph.json", "json", False)],
    "analyze": [("05-analysis.md", "Phân tích", "md", True)],
    "format": [("06-report.preview.html", "Xem trước", "html", False), ("06-report.xhtml", "XHTML", "xhtml", True),
               ("06-report.raw.txt", "Phản hồi gốc của AI", "text", False), ("06-refine.log", "Log refine", "text", False)],
    "collab": [("07-collab.json", "Kết quả Collab", "json", False)],
}
EDITABLE = {f for items in ARTIFACTS.values() for f, _, _, e in items if e} | {"02-focus.json"}
DOWNSTREAM_OF_FILE = {"05-analysis.md": "format", "06-report.xhtml": "collab", "02-focus.json": "log"}

_lock = threading.RLock()
_runs = {}


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
            for step in ORDER:  # a crash mid-run leaves "running" behind
                if run["steps"][step]["status"] == "running":
                    run["steps"][step].update(status="error", error=t("err.interrupted"))
            _runs[run_id] = run
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


def create(issue, log_files=(), source_dir="", auto_attachments=False, download_dir=""):
    key = issue_key(issue)
    if not key:
        raise ValueError(t("err.issue"))
    files = [str(Path(f)) for f in log_files or [] if str(f).strip()]
    for f in files:
        if not Path(f).is_file():
            raise ValueError(t("err.log_missing", path=f))
    if source_dir and not Path(source_dir).is_dir():
        raise ValueError(t("err.src_missing", path=source_dir))
    run_id = f"{key}-{datetime.now():%Y%m%d-%H%M%S}"
    run_dir(run_id).mkdir(parents=True, exist_ok=True)
    run = {
        "id": run_id, "issue": key, "summary": "", "created": time.time(), "updated": time.time(),
        "inputs": {"log_files": files, "source_dir": str(source_dir or ""), "auto_attachments": bool(auto_attachments),
                   "download_dir": str(download_dir or "")},
        "attachments": [],
        "session_id": str(uuid.uuid4()), "focus": {}, "collab": {}, "ai_mode": settings.load()["ai_mode"],
        "steps": {s: {"status": "idle", "note": "", "error": "", "started": None, "ended": None, "artifacts": []} for s in ORDER},
    }
    with _lock:
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


def update_inputs(run_id, log_files=None, source_dir=None, auto_attachments=None, download_dir=None):
    run = get(run_id)
    if auto_attachments is not None:
        run["inputs"]["auto_attachments"] = bool(auto_attachments)
    if download_dir is not None:
        run["inputs"]["download_dir"] = str(download_dir)
    if log_files is not None:
        run["inputs"]["log_files"] = [f for f in log_files if f]
        _mark_stale(run, "understand")
    if source_dir is not None:
        run["inputs"]["source_dir"] = source_dir
        _mark_stale(run, "log")
    _save(run)
    return run


def _mark_stale(run, from_step):
    for step in ORDER[ORDER.index(from_step):]:
        if run["steps"][step]["status"] in ("done", "waiting"):
            run["steps"][step]["status"] = "stale"


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

    def start(self, run_id, start=None, only=False, publish=False):
        if self.busy:
            raise RuntimeError(t("err.busy"))
        if not get(run_id):
            raise ValueError("Run not found")
        self.cancel.clear()
        self.run_id = run_id
        self.thread = threading.Thread(target=self._run, args=(run_id, start, only, publish), daemon=True)
        self.thread.start()

    def stop(self):
        self.cancel.set()
        ai.cancel()

    def _run(self, run_id, start, only, publish):
        run = get(run_id)
        cfg = settings.load()
        run["ai_mode"] = cfg["ai_mode"]
        first = ORDER.index(start) if start else 0
        _mark_stale(run, ORDER[first])
        events.publish("busy", run_id=run_id, busy=True)
        try:
            for step in ORDER[first:]:
                if self.cancel.is_set():
                    break
                state = run["steps"][step]
                if not APPLICABLE[step](run):
                    state.update(status="skipped", note=t(SKIP_REASON[step]), error="")
                    _save(run)
                    continue
                if step == "collab" and cfg.get("review_before_publish") and not publish and not (only and start == "collab"):
                    state.update(status="waiting", note=t("note.review"), error="")
                    _save(run)
                    break
                state.update(status="running", note="", error="", started=time.time(), ended=None)
                _save(run)
                _log(run, "▶ " + t("step." + step))
                try:
                    note = RUNNERS[step](run, cfg, self)
                except Cancelled:
                    state.update(status="idle", note=t("note.stopped"), ended=time.time())
                    _save(run)
                    break
                except Exception as exc:  # surface every failure on the step card
                    if self.cancel.is_set():
                        state.update(status="idle", note=t("note.stopped"), ended=time.time())
                    else:
                        state.update(status="error", error=str(exc), ended=time.time())
                        _log(run, "✗ " + t("step." + step) + f": {exc}")
                        _log(run, traceback.format_exc(), quiet=True)
                    _save(run)
                    break
                state.update(status="done", note=note or "", ended=time.time())
                _log(run, "✓ " + t("step." + step) + (f" — {note}" if note else ""))
                _save(run)
                if only:
                    break
        finally:
            events.publish("busy", run_id=run_id, busy=False)

    def check(self):
        if self.cancel.is_set():
            raise Cancelled()

    def fetch_attachments(self, run_id, names, analyze, folder=None):
        """Manual download (Jira step panel). Uses the same busy slot as a run."""
        if self.busy:
            raise RuntimeError(t("err.busy"))
        run = get(run_id)
        if not run:
            raise ValueError("Run not found")
        if folder is not None:
            run["inputs"]["download_dir"] = str(folder).strip()
        self.cancel.clear()
        self.run_id = run_id

        def job():
            events.publish("busy", run_id=run_id, busy=True)
            try:
                added = _fetch_attachments(run, set(names), set(analyze), self.check)
                if added:
                    _mark_stale(run, "understand")
                _save(run)
            except Cancelled:
                _log(run, t("note.stopped"))
            except Exception as exc:
                _log(run, f"✗ {exc}")
            finally:
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
    return run["steps"][step]["status"] in ("done", "stale")


def _instruction(name):
    return paths.INSTRUCTIONS / name


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
    if not found:
        try:
            found = attachments.list_rest(run["issue"])
        except Exception as exc:  # listing attachments must never fail the Jira step
            _log(run, f"attachments: {exc}", quiet=True)
    known = {a["filename"]: a for a in run.get("attachments") or []}
    run["attachments"] = [{**a, **{k: known[a["filename"]][k] for k in ("path", "analyzed") if k in known.get(a["filename"], {})}}
                          for a in found]
    _write(run, "01-attachments.json", json.dumps(run["attachments"], ensure_ascii=False, indent=2))
    note = run["summary"][:120]
    if found:
        note += " · " + t("note.attachments", n=len(found))
    if run["inputs"].get("auto_attachments"):
        wanted = {a["filename"] for a in found if a["kind"] in ("log", "archive")}
        if wanted:
            added = _fetch_attachments(run, wanted, wanted, ex.check)
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
    logs, files = attachments.collect(run["issue"], items, analyze, folder, log=lambda m: _log(run, m), cancel=cancel)
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


def step_understand(run, cfg, ex):
    files = run["inputs"]["log_files"]
    spans = logsleuth.span(files)
    rows = "\n".join(f"| {s['name']} | {s['format']} | {s['first'] or '-'} | {s['last'] or '-'} | {s['lines']} | {s['firstTs'] or '-'} |"
                     for s in spans)
    _write(run, "02-log-span.md", "# Log clock ranges\n\n| File | Format | First clock | Last clock | Lines | First timestamp |\n"
           "|---|---|---|---|---|---|\n" + rows + "\n")
    ex.check()
    answer = ai.ask(cfg, (f"Jira issue {run['issue']}. Follow the attached instruction understand.md using the attached "
                          "Jira ticket (01-jira.md) and the log clock ranges (02-log-span.md)."),
                    [_path(run, "01-jira.md"), _path(run, "02-log-span.md"), _instruction("understand.md")],
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
    block = storage.last_fence(answer, ("json",))
    m = re.search(r"(?s)```json\s*(.*?)```", block or "")
    try:
        return _clean_focus(json.loads(m.group(1)) if m else {})
    except ValueError:
        return {}


def _clean_focus(data):
    def clock(v):
        v = str(v or "").strip()
        return v if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", v) else ""
    return {
        "time_start": clock(data.get("time_start")), "time_end": clock(data.get("time_end")),
        "confidence": str(data.get("confidence") or ""), "reason": str(data.get("reason") or ""),
        "keywords": [str(k) for k in data.get("keywords") or [] if str(k).strip()][:15],
        "services": [str(s) for s in data.get("services") or [] if str(s).strip()][:10],
    }


def step_log(run, cfg, ex):
    files = run["inputs"]["log_files"]
    source = run["inputs"]["source_dir"]
    catalog = None
    if source:
        _log(run, t("log.scan"))
        catalog = logsleuth.catalog_for(source)
        shutil.copyfile(catalog, _path(run, "03-catalog.md"))
    ex.check()
    focus = run.get("focus") or {}
    out = _path(run, "03-log-brief.md")
    stats = logsleuth.slice_logs(files, out, catalog, focus, cfg["log_budget_tokens"])
    window = stats.get("timeWindow")
    if stats.get("kept", 0) == 0 and window:
        _log(run, t("log.window_empty"))
        stats = logsleuth.slice_logs(files, out, catalog, {**focus, "time_start": "", "time_end": ""}, cfg["log_budget_tokens"])
        window = None
    note = t("note.log", total=stats["totalLines"], kept=stats["kept"], tokens=stats["tokens"])
    if window:
        note += f", {window['start']}–{window['end']}"
    return note


def step_graph(run, cfg, ex):
    out, meta = codegraph.build(run["inputs"]["source_dir"], cfg["graph_token_budget"])
    codegraph.copy_into(out, run_dir(run["id"]))
    return t("note.graph", files=meta.get("files", 0), nodes=meta.get("nodes", 0), edges=meta.get("edges", 0))


def step_analyze(run, cfg, ex):
    if not _path(run, "01-jira.md").exists():
        raise RuntimeError(t("err.need_jira"))
    files = [_path(run, "01-jira.md")]
    if _done(run, "understand") and _path(run, "02-understanding.md").exists():
        files.append(_path(run, "02-understanding.md"))
    if _done(run, "log") and _path(run, "03-log-brief.md").exists():
        files.append(_path(run, "03-log-brief.md"))
    if _done(run, "graph") and _path(run, "04-graph.simple.json").exists():
        files.append(_path(run, "04-graph.simple.json"))
    files.append(_instruction("ticket-analysis.md"))
    names = ", ".join(f.name for f in files[:-1])
    answer = ai.ask(cfg, (f"Jira issue {run['issue']}. Apply the attached instruction ticket-analysis.md to the attached "
                          f"evidence ({names}) and return the final analysis in Markdown only."),
                    files, run_dir(run["id"]), session_id=run["session_id"])
    ex.check()
    _write(run, "05-analysis.md", storage.strip_chat_wrappers(answer))
    return t("note.analyze", chars=len(answer), sources=len(files) - 1)


def step_format(run, cfg, ex):
    if not _path(run, "05-analysis.md").exists():
        raise RuntimeError(t("err.need_analyze"))
    link = f"{cfg['jira_browse_url'].rstrip('/')}/{run['issue']}"
    raw = ai.ask(cfg, (f"Jira issue {run['issue']} ({link}). Apply the attached report instruction report.md to the attached "
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
            out = ai.ask(cfg, f"{instruction}\nThis is fragment {index + 1} of {total}, attached as {frag.name}.",
                         [frag], run_dir(run["id"]), session_id=None, small=True)
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


def step_collab(run, cfg, ex):
    if not _path(run, "06-report.xhtml").exists():
        raise RuntimeError(t("err.need_format"))
    report = storage.normalize_storage_report(_path(run, "06-report.xhtml").read_text(encoding="utf-8"))
    space = (cfg.get("collab_space_key") or "").strip()
    if not space:
        raise RuntimeError(t("err.no_space"))
    title = f"{run['issue']} - {run.get('summary') or 'Ticket analysis'}"[:240]
    result = collab.publish(space, title, report, (cfg.get("collab_parent_id") or "").strip(), log=lambda m: _log(run, m))
    url = result.get("url") or ""
    if not url and result.get("page_id"):
        base = mcp.status()["collab"]["url"].rstrip("/")
        url = f"{base}/pages/viewpage.action?pageId={result['page_id']}"
    run["collab"] = {"action": result["action"], "page_id": result.get("page_id"), "url": url, "title": title}
    _write(run, "07-collab.json", json.dumps({**run["collab"], "result": result.get("result")}, ensure_ascii=False, indent=2, default=str))
    return (t("note.created") if result["action"] == "created" else t("note.updated")) + (f" {result.get('page_id')}" if result.get("page_id") else "")


RUNNERS = {"jira": step_jira, "understand": step_understand, "log": step_log, "graph": step_graph,
           "analyze": step_analyze, "format": step_format, "collab": step_collab}
APPLICABLE = {
    "jira": lambda r: True,
    "understand": lambda r: bool(r["inputs"]["log_files"]),
    "log": lambda r: bool(r["inputs"]["log_files"]),
    "graph": lambda r: bool(r["inputs"]["source_dir"]),
    "analyze": lambda r: True, "format": lambda r: True, "collab": lambda r: True,
}
SKIP_REASON = {"understand": "note.skip_nolog", "log": "note.skip_nolog", "graph": "note.skip_nosrc"}
