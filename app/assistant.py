"""AI assistant behind the chat bubble.

Every turn the server writes a bounded snapshot of the app (settings, environment, the open run with its
steps, evidence gate, attachments and logs, recent errors) plus the recent conversation into
data/assistant/work/, and Copilot CLI answers from those files in read-only mode, in its own process slot
so it runs beside the pipeline. The reply may end with a fenced ```actions JSON block; each action is
validated against a whitelist and only executed when the user clicks it in the UI.

Bug tickets for defects of Ticket Analyzer itself are written as Markdown to data/bugs/.
"""
import json
import platform
import re
import shutil
import threading
import time
import unicodedata
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path

from . import __version__, attachments, doctor, events, logprep, paths, pipeline, settings
from .ai import copilot
from .evidence.timestamps import format_offset, normalize_clock, parse_offset
from .i18n import language_name, relocalize, t

DIR = paths.DATA / "assistant"
WORK = DIR / "work"
CHAT_FILE = DIR / "chat.json"
BUGS = paths.DATA / "bugs"

MAX_MESSAGES = 80          # kept in chat.json
HISTORY_MESSAGES = 12      # sent to the model
MAX_TEXT = 4000            # one user message
MAX_ACTIONS = 5
TIMEOUT = 600
SNAPSHOT_LIMIT = 60000     # characters of context.md
DOCTOR_TTL = 90            # seconds a doctor result is reused

STEP_IDS = list(pipeline.ORDER)
SEVERITIES = ("critical", "high", "medium", "low")
RUN_ACTIONS = {"download_attachments", "resolve_gate", "resolve_timezone", "run_steps", "log_scan", "update_inputs"}
RUN_INPUT_TYPES = {"source_dir": str, "download_dir": str, "data_dir": str, "gate": str, "auto_attachments": bool,
                   "use_codegraph": bool, "use_infographic": bool, "continue_on_optional_error": bool,
                   "auto_publish_to_collab": bool, "log_shift_minutes": (int, type(None))}
SETTING_BLOCKLIST = {"port"}
BUG_TEXT_FIELDS = {"title": 120, "component": 80, "summary": 1500, "expected": 800, "actual": 1500,
                   "evidence": 4000, "suspected_cause": 1500, "suggested_fix": 1500}

PROMPT = ("You are the Ticket Analyzer assistant. Read these files in the current directory completely, in this "
          "order: assistant.md (your instruction), context.md (live system snapshot), conversation.md (recent chat). "
          "Answer the LAST user message in conversation.md, following assistant.md, in {language}. You may open "
          "files in the readable folders listed in context.md. Do not modify any file.")

SCAN_REQUESTS = {
    "": ("Scan the whole system state now. Report, most important first: problems and risks, the next best step "
         "for the open run, attachments worth downloading or skipping (and why), inputs or settings to adjust, "
         "and any Ticket Analyzer defect worth a bug ticket. Propose actions."),
    "gate": ("The evidence gate of the open run is waiting for a decision. Recommend exactly which attachments to "
             "download (exact file names, whole multipart sets, total sizes and the download cap; prioritize images) "
             "and the normalized incident time window / UTC time zone, or continuing without logs when nothing "
             "is useful. An inner log filename may refer to a ZIP with a different name. Propose one resolve_gate "
             "action."),
    "tz": ("The ticket does not state the time zone of its incident time, so the Log step waits for the user's choice "
           "before slicing the logs. Using 'Time zone decision' in the snapshot (each log file's first/last timestamp, "
           "the candidate alignments and whether each window falls inside the logs, the app's suggestion), the ticket "
           "text (01-jira.md) and the attachment upload times, recommend ONE of: a candidate alignment, a manual "
           "shift (minutes = log clock - ticket clock), or dropping the time filter. Explain the evidence briefly and "
           "propose one resolve_timezone action."),
    "error": ("Step '{step}' of the open run just failed with: {error}\nExplain the most likely cause (user input, "
              "configuration, environment, network/credentials, or a Ticket Analyzer defect) and how to fix it. "
              "Propose fix actions; if it is a defect of Ticket Analyzer itself, also propose create_bug."),
}

_lock = threading.RLock()
_job = {"running": False, "cancelled": False, "started": None}
_errors = deque(maxlen=30)
_doctor_cache = {"at": 0.0, "value": None}
_chat_cache = {"data": None}


# ---------------------------------------------------------------- small helpers
def _id():
    return uuid.uuid4().hex[:12]


def _clip(value, limit):
    text = " ".join(str(value or "").split()) if limit <= 300 else str(value or "").strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _cell(value, limit=160):
    return _clip(value, limit).replace("|", "\\|").replace("\n", " ")


def _table(headers, rows):
    if not rows:
        return []
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return out


def _human(size):
    size = attachments.parse_size(size)
    if not size:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return "?"


def _when(stamp):
    try:
        return datetime.fromtimestamp(float(stamp)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def _tail(path, lines=80, limit=9000):
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])[-limit:]


def _language():
    return language_name()


def record_error(source, error):
    """Remember an app-level error (API failure, background job) so the assistant can see it."""
    _errors.append({"time": time.time(), "source": str(source)[:120], "message": _clip(error, 600)})


# ---------------------------------------------------------------- chat storage
def _chat():
    if _chat_cache["data"] is None:
        try:
            data = json.loads(CHAT_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data["messages"] = [m for m in data.get("messages") or [] if isinstance(m, dict)]
        data["triggers"] = [str(x) for x in data.get("triggers") or []]
        _chat_cache["data"] = data
    return _chat_cache["data"]


def _store():
    data = _chat()
    data["messages"] = data["messages"][-MAX_MESSAGES:]
    data["triggers"] = data["triggers"][-300:]
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = CHAT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(CHAT_FILE)


def busy():
    return _job["running"]


def _model(cfg):
    return cfg.get("copilot_model") or "auto"


def _view(message):
    """A chat message as the panel shows it: app-written texts (scan requests, errors) in the current language."""
    out = dict(message)
    if out.get("role") == "user" and out.get("kind") == "scan":
        out["text"] = relocalize(out.get("text"))
    if out.get("error"):
        out["error"] = relocalize(out["error"])
    return out


def state():
    with _lock:
        messages = [_view(m) for m in _chat()["messages"]]
    return {"messages": messages, "busy": busy(), "started": _job["started"], "bugs": list_bugs()[:30],
            "bugs_dir": str(BUGS), "model": _model(settings.load())}


def clear():
    """The trash button: a clean slate - conversation, local bug tickets (the counter in the panel), the
    remembered app errors and the per-turn work files. Proactive-scan keys stay, so old events are not
    scanned again. Returns what is left (bug files that could not be deleted)."""
    with _lock:
        if busy():
            raise RuntimeError(t("asst.busy"))
        _chat()["messages"] = []
        _store()
        _errors.clear()
        for path in BUGS.glob("BUG-*.md") if BUGS.is_dir() else []:
            try:
                path.unlink()
            except OSError:
                pass
        shutil.rmtree(WORK, ignore_errors=True)
        left = list_bugs()[:30]
    events.publish("assistant_cleared", bugs=left)
    return {"bugs": left}


# ---------------------------------------------------------------- asking
def _scan_label(reason, run):
    kind, _, step = (reason or "").partition(":")
    if kind == "gate":
        return t("asst.auto_gate", issue=run["issue"] if run else "")
    if kind == "tz":
        return t("asst.auto_tz", issue=run["issue"] if run else "")
    if kind == "error":
        return t("asst.auto_error", step=t("step." + step) if step in STEP_IDS else step)
    return t("asst.scan_msg")


def _scan_request(reason, run):
    kind, _, step = (reason or "").partition(":")
    template = SCAN_REQUESTS.get(kind, SCAN_REQUESTS[""])
    error = ((run or {}).get("steps", {}).get(step) or {}).get("error", "") if step else ""
    return template.format(step=step, error=_clip(error, 600) or "(no error text)")


def ask(text="", run_id=None, kind="chat", reason="", trigger=None):
    """Queue one question (`chat`) or a system scan (`scan`, optional `reason` gate / error:<step>).
    A `trigger` key makes a proactive scan happen once: repeats and scans while busy are skipped."""
    kind = kind if kind in ("chat", "scan") else "chat"
    text = str(text or "").strip()[:MAX_TEXT]
    if kind == "chat" and not text:
        raise ValueError(t("asst.empty"))
    run = pipeline.get(run_id) if run_id else None
    with _lock:
        data = _chat()
        trigger = str(trigger or "")[:200]
        if trigger and trigger in data["triggers"]:
            return {"skipped": "duplicate"}
        if busy():
            if trigger:
                return {"skipped": "busy"}
            raise RuntimeError(t("asst.busy"))
        if trigger:
            data["triggers"].append(trigger)
        message = {"id": _id(), "role": "user", "kind": kind, "reason": reason or "", "time": time.time(),
                   "text": text or _scan_label(reason, run), "run_id": run["id"] if run else None,
                   "issue": run["issue"] if run else ""}
        if kind == "scan":
            message["request"] = (text + "\n\n" if text else "") + _scan_request(reason, run)
        data["messages"].append(message)
        _store()
        _job.update(running=True, cancelled=False, started=time.time())
        threading.Thread(target=_answer, args=(message,), daemon=True).start()
    events.publish("assistant", message=message)
    events.publish("assistant_busy", busy=True, started=_job["started"])
    return {"message": message}


def stop():
    with _lock:
        if busy():
            _job["cancelled"] = True
            copilot.cancel("assistant")


def _readable_dirs(run):
    dirs = []
    if run:
        dirs.append(pipeline.run_dir(run["id"]))
        source = (run["inputs"].get("source_dir") or "").strip()
        if source and Path(source).is_dir():
            dirs.append(Path(source))
        folder = pipeline.attachment_folder(run)
        if folder.is_dir():
            dirs.append(folder)
        dirs += [Path(f).parent for f in run["inputs"].get("log_files") or [] if Path(f).parent.is_dir()][:3]
    if BUGS.is_dir():
        dirs.append(BUGS)
    if not paths.FROZEN and paths.APP.is_dir():
        dirs.append(paths.APP)  # the tool's own source, to locate Ticket Analyzer defects
    unique = []
    for d in dirs:
        resolved = str(Path(d).resolve())
        if resolved not in unique:
            unique.append(resolved)
    return unique


def _conversation(upto):
    data = _chat()["messages"]
    index = next((i for i, m in enumerate(data) if m["id"] == upto["id"]), len(data) - 1)
    out = ["# Conversation (oldest first; answer the last user message)", ""]
    for m in data[max(0, index + 1 - HISTORY_MESSAGES):index + 1]:
        who = "User" if m["role"] == "user" else "Assistant"
        where = f" · run {m['run_id']} ({m.get('issue')})" if m.get("run_id") else ""
        out.append(f"### {who} · {_when(m.get('time'))}{where}")
        if m["role"] == "user":
            out.append(m.get("request") or m.get("text") or "")
        else:
            out.append(m.get("text") or f"(failed: {m.get('error', '')})")
            for a in m.get("actions") or []:
                out.append(f"- proposed action `{a['type']}` \"{a.get('label')}\" → {a.get('status')}"
                           + (f" ({a['result']})" if a.get("result") else ""))
        out.append("")
    return "\n".join(out)


def _prepare_work(run, cfg, message):
    WORK.mkdir(parents=True, exist_ok=True)
    for old in WORK.iterdir():
        if old.is_file():
            old.unlink()
    shutil.copyfile(paths.INSTRUCTIONS / "assistant.md", WORK / "assistant.md")
    (WORK / "context.md").write_text(snapshot(run, cfg), encoding="utf-8")
    (WORK / "conversation.md").write_text(_conversation(message), encoding="utf-8")


def _answer(message):
    cfg = settings.load()
    reply = {"id": _id(), "role": "assistant", "reply_to": message["id"], "run_id": message["run_id"],
             "issue": message.get("issue", ""), "model": _model(cfg), "actions": []}
    usage = {}
    started = time.time()
    try:
        run = pipeline.get(message["run_id"]) if message["run_id"] else None
        _prepare_work(run, cfg, message)
        raw = copilot.ask(PROMPT.format(language=_language()), WORK, cfg.get("copilot_model") or "",
                          timeout=TIMEOUT, extra_dirs=_readable_dirs(run), slot="assistant",
                          on_usage=usage.update)
        if _job["cancelled"]:
            raise copilot.CopilotError(t("asst.stopped"))
        text, proposed = parse_reply(raw)
        reply["text"] = text or t("asst.no_text")
        reply["actions"] = [a for a in (validate_action(p, run) for p in proposed) if a]
    except Exception as exc:
        reply["text"] = ""
        reply["error"] = t("asst.stopped") if _job["cancelled"] else _clip(exc, 1500)
    reply["time"] = time.time()
    reply["seconds"] = round(reply["time"] - started, 1)
    if usage:
        reply["usage"] = {k: int(usage.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_read_tokens")}
    with _lock:
        try:
            _chat()["messages"].append(reply)
            _store()
        finally:
            _job.update(running=False, started=None)
    events.publish("assistant", message=reply)
    events.publish("assistant_busy", busy=False)


# ---------------------------------------------------------------- reply parsing / actions
def parse_reply(raw):
    """(answer text, proposed actions) - the last fenced ```actions block (or a JSON array of typed
    objects) is removed from the text and parsed."""
    text = str(raw or "").strip()
    for m in reversed(list(re.finditer(r"```[ \t]*([A-Za-z]*)[ \t]*\r?\n(.*?)```", text, re.S))):
        tag = m.group(1).lower()
        if tag not in ("actions", "json", ""):
            continue
        try:
            data = json.loads(m.group(2))
        except ValueError:
            if tag == "actions":  # a broken block is still not something to show
                return (text[:m.start()] + text[m.end():]).strip(), []
            continue
        if isinstance(data, dict) and isinstance(data.get("actions"), list):
            data = data["actions"]
        typed = isinstance(data, list) and all(isinstance(x, dict) and x.get("type") for x in data)
        if tag == "actions" or (typed and data):
            items = [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []
            return (text[:m.start()] + text[m.end():]).strip(), items[:MAX_ACTIONS]
    return text, []


def _names(value, run, required=False):
    known = {a["filename"].casefold(): a["filename"] for a in run.get("attachments") or []}
    names = []
    for name in value if isinstance(value, list) else []:
        real = known.get(str(name).strip().casefold())
        if real and real not in names:
            names.append(real)
    if required and not names:
        raise ValueError("no known attachment")
    return names


def _bool(value):
    if isinstance(value, bool):
        return value
    raise ValueError("not a boolean")


def validate_action(raw, run):
    """A cleaned copy of a proposed action, or None when it is unknown, malformed or not applicable."""
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("type") or "").strip()
    action = {"type": kind, "label": _clip(raw.get("label") or t("asst.act." + kind), 80),
              "why": _clip(raw.get("why") or "", 300), "status": "pending"}
    try:
        if kind in RUN_ACTIONS and not run:
            return None
        if kind == "download_attachments":
            action["names"] = _names(raw.get("names"), run, required=True)
        elif kind == "resolve_gate":
            if not run.get("pending_gate"):
                return None
            action["skip_logs"] = bool(raw.get("skip_logs"))
            action["names"] = [] if action["skip_logs"] else _names(raw.get("names"), run)
            for key in ("time_start", "time_end"):
                value = raw.get(key)
                if isinstance(value, str) and value.strip() and normalize_clock(value.strip()):
                    action[key] = value.strip()
            shift = raw.get("shift_minutes")
            if isinstance(shift, (int, float)) and not isinstance(shift, bool) and -840 <= shift <= 840:
                action["shift_minutes"] = int(shift)
            if "ticket_utc_offset" in raw:
                offset = raw["ticket_utc_offset"]
                if offset not in (None, "") and parse_offset(offset) is None:
                    return None
                action["ticket_utc_offset"] = format_offset(parse_offset(offset)) or None
            if not action["names"] and not action["skip_logs"] and not any(
                    k in action for k in ("time_start", "time_end", "shift_minutes", "ticket_utc_offset")):
                return None
        elif kind == "resolve_timezone":
            action["mode"] = raw.get("mode") if raw.get("mode") in pipeline.TZ_MODES else None
            if not action["mode"]:
                return None
            if action["mode"] in ("suggested", "manual"):
                shift = raw.get("shift_minutes")
                if not isinstance(shift, (int, float)) or isinstance(shift, bool) or abs(shift) > 840:
                    return None
                action["shift_minutes"] = int(shift)
            offset = raw.get("ticket_utc_offset")
            if isinstance(offset, str) and parse_offset(offset) is not None:
                action["ticket_utc_offset"] = format_offset(parse_offset(offset))
        elif kind == "run_steps":
            for key in ("from", "until"):
                value = raw.get(key)
                if value not in (None, ""):
                    if value not in STEP_IDS:
                        return None
                    action[key] = value
            action["only"] = bool(raw.get("only"))
            action["resume"] = bool(raw.get("resume"))
        elif kind == "update_inputs":
            changes = {}
            for key, value in (raw.get("changes") or {}).items():
                if key in RUN_INPUT_TYPES and isinstance(value, RUN_INPUT_TYPES[key]) and not (
                        RUN_INPUT_TYPES[key] is not bool and isinstance(value, bool)):
                    changes[key] = value.strip() if isinstance(value, str) else value
            if changes.get("gate") not in (None, *settings.GATES):
                return None
            if not changes:
                return None
            action["changes"] = changes
        elif kind == "update_settings":
            changes = {}
            for key, value in (raw.get("changes") or {}).items():
                if key in settings.DEFAULTS and key not in SETTING_BLOCKLIST:
                    try:
                        changes[key] = settings._valid(key, value)
                    except ValueError:
                        continue
            if not changes:
                return None
            action["changes"] = changes
        elif kind == "create_bug":
            for key, limit in BUG_TEXT_FIELDS.items():
                action[key] = _clip(raw.get(key) or "", limit)
            if not action["title"]:
                return None
            action["severity"] = str(raw.get("severity") or "medium").lower()
            if action["severity"] not in SEVERITIES:
                action["severity"] = "medium"
            steps = raw.get("steps") or []
            action["steps"] = [_clip(s, 300) for s in (steps if isinstance(steps, list) else [steps]) if s][:12]
        elif kind == "open_folder":
            action["what"] = raw.get("what") if raw.get("what") in ("bugs", "attachments", "run") else "bugs"
            if action["what"] != "bugs" and not run:
                return None
        elif kind not in ("log_scan", "stop_pipeline"):
            return None
    except (ValueError, TypeError, AttributeError):
        return None
    return action


def run_action(message_id, index, opener=None):
    """Execute one proposed action (the user clicked it); the result is stored on the action."""
    with _lock:
        message = next((m for m in _chat()["messages"] if m.get("id") == message_id and m.get("role") == "assistant"),
                       None)
        actions = (message or {}).get("actions") or []
        if not isinstance(index, int) or not 0 <= index < len(actions):
            raise ValueError(t("asst.no_action"))
        action = actions[index]
        if action.get("status") == "done" and action["type"] != "open_folder":
            raise ValueError(t("asst.done_already"))
    run = pipeline.get(message["run_id"]) if message.get("run_id") else None
    try:
        result, status = _execute(action, run, opener), "done"
    except Exception as exc:
        result, status = str(exc), "error"
    with _lock:
        action.update(status=status, result=_clip(result, 500), done_at=time.time())
        _store()
    events.publish("assistant_action", message_id=message_id, index=index, action=action)
    if status == "error":
        raise RuntimeError(result)
    return {"action": action}


def _execute(action, run, opener):
    kind = action["type"]
    if kind in RUN_ACTIONS and not run:
        raise ValueError(t("asst.need_run"))
    ex = pipeline.executor
    if kind == "download_attachments":
        ex.fetch_attachments(run["id"], action["names"])
        return t("asst.res.download", n=len(action["names"]))
    if kind == "resolve_gate":
        if not run.get("pending_gate"):
            raise ValueError(t("asst.gate_gone"))
        focus = run.get("focus") or {}
        ex.resolve_gate(run["id"], action.get("names") or [], None,
                        action.get("time_start", focus.get("time_start")), action.get("time_end", focus.get("time_end")),
                        action["shift_minutes"] if "shift_minutes" in action else ..., action.get("skip_logs", False),
                        **({"ticket_utc_offset": action["ticket_utc_offset"]} if "ticket_utc_offset" in action else {}))
        return t("asst.res.gate")
    if kind == "resolve_timezone":
        ex.resolve_timezone(run["id"], action["mode"], action.get("shift_minutes"), None, None,
                            action.get("ticket_utc_offset"))
        return t("asst.res.tz")
    if kind == "run_steps":
        ex.start(run["id"], action.get("from"), action.get("only", False), False, until=action.get("until"),
                 resume=action.get("resume", False))
        return t("asst.res.started")
    if kind == "log_scan":
        ex.start(run["id"], "log", only=True, catalog_only=True)
        return t("asst.res.started")
    if kind == "update_inputs":
        pipeline.update_inputs(run["id"], **action["changes"])
        return t("asst.res.saved")
    if kind == "update_settings":
        if ex.busy and "ai_mode" in action["changes"]:
            raise RuntimeError(t("err.mode_busy"))
        events.publish("settings", settings=settings.save(action["changes"]))
        return t("asst.res.saved")
    if kind == "create_bug":
        path, duplicate = write_bug(action, run)
        events.publish("assistant_bugs", bugs=list_bugs()[:30])
        return (t("asst.res.bug_dup", file=path.name) if duplicate else t("asst.res.bug", file=path.name))
    if kind == "open_folder":
        target = BUGS if action["what"] == "bugs" else (
            pipeline.attachment_folder(run) if action["what"] == "attachments" else pipeline.run_dir(run["id"]))
        target.mkdir(parents=True, exist_ok=True)
        if opener:
            opener(target)
        return str(target)
    if kind == "stop_pipeline":
        ex.stop()
        return t("asst.res.stopped")
    raise ValueError(t("asst.no_action"))


# ---------------------------------------------------------------- bug tickets
def _slug(text):
    text = unicodedata.normalize("NFKD", str(text or "").replace("đ", "d").replace("Đ", "D"))
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:60] or "bug"


def _front_matter(path):
    meta = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return meta
    match = re.match(r"(?s)^---\r?\n(.*?)\r?\n---", text)
    for line in (match.group(1).splitlines() if match else []):
        key, sep, value = line.partition(":")
        if sep:
            value = value.strip()
            try:
                value = json.loads(value) if value.startswith('"') else value
            except ValueError:
                pass
            meta[key.strip()] = value
    return meta


def list_bugs():
    out = []
    for path in sorted(BUGS.glob("BUG-*.md"), reverse=True) if BUGS.is_dir() else []:
        meta = _front_matter(path)
        out.append({"id": meta.get("id") or path.stem, "title": meta.get("title") or path.stem,
                    "severity": meta.get("severity", ""), "status": meta.get("status", "open"),
                    "created": meta.get("created", ""), "file": path.name, "path": str(path)})
    return out


def _error_lines(run, limit=60):
    if not run:
        return ""
    text = _tail(pipeline.run_dir(run["id"]) / "run.log", lines=1500, limit=200000)
    keep = [line for line in text.splitlines()
            if re.search(r"✗|⚠|Traceback|Error|Exception|File \"|^\s+\w+Error", line)]
    return "\n".join(keep[-limit:])[-8000:]


def write_bug(fields, run=None):
    """Write data/bugs/BUG-<stamp>-<slug>.md; an open ticket with the same title is reused.
    Returns (path, duplicate)."""
    BUGS.mkdir(parents=True, exist_ok=True)
    title = _clip(fields.get("title") or "Untitled bug", 120)
    key = _slug(title)
    for existing in BUGS.glob("BUG-*.md"):
        meta = _front_matter(existing)
        if _slug(meta.get("title", "")) == key and meta.get("status", "open") == "open":
            return existing, True
    now = datetime.now()
    bug_id = f"BUG-{now:%Y%m%d-%H%M%S}"
    path = BUGS / f"{bug_id}-{key[:48]}.md"
    cfg = settings.load()
    severity = fields.get("severity") if fields.get("severity") in SEVERITIES else "medium"
    q = lambda v: json.dumps(str(v or ""), ensure_ascii=False)  # noqa: E731
    lines = ["---", f"id: {bug_id}", f"title: {q(title)}", f"severity: {severity}", "status: open",
             f"component: {q(fields.get('component'))}", f"created: {q(now.strftime('%Y-%m-%d %H:%M:%S'))}",
             f"run: {q(run['id'] if run else '')}", f"issue: {q(run['issue'] if run else '')}",
             f"app_version: {q(__version__)}", "reported_by: \"Ticket Analyzer assistant\"", "---", "",
             f"# {title}", "",
             f"**Severity:** {severity} · **Component:** {fields.get('component') or '—'} · "
             f"**Run:** `{run['id'] if run else '—'}`" + (f" ({run['issue']})" if run else ""), ""]

    def section(heading, body):
        if body:
            lines.extend([f"## {heading}", "", body.strip(), ""])

    section("Summary", fields.get("summary"))
    steps = fields.get("steps") or []
    section("Steps to reproduce", "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)))
    section("Expected", fields.get("expected"))
    section("Actual", fields.get("actual"))
    if fields.get("evidence"):
        section("Evidence", "```text\n" + fields["evidence"] + "\n```")
    section("Suspected cause / code location", fields.get("suspected_cause"))
    section("Suggested fix", fields.get("suggested_fix"))
    env = [f"- Ticket Analyzer {__version__} · Python {platform.python_version()} · {platform.platform()}",
           f"- AI mode: {cfg['ai_mode']} · Copilot model: {cfg['copilot_model'] or 'CLI default'} · "
           f"M365 model: {cfg['m365_model']}"]
    if run:
        env += ["", *_table(["step", "status", "note", "error"],
                            [[s, run["steps"][s]["status"], run["steps"][s].get("note", ""),
                              run["steps"][s].get("error", "")] for s in STEP_IDS])]
    section("Environment (collected by the app)", "\n".join(env))
    excerpt = _error_lines(run)
    if excerpt:
        section("Run log excerpt (collected by the app)", "```text\n" + excerpt + "\n```")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path, False


# ---------------------------------------------------------------- snapshot
def _doctor():
    if time.time() - _doctor_cache["at"] > DOCTOR_TTL or _doctor_cache["value"] is None:
        try:
            _doctor_cache["value"] = doctor.checks()
        except Exception as exc:
            _doctor_cache["value"] = {"error": str(exc)}
        _doctor_cache["at"] = time.time()
    return _doctor_cache["value"]


def _sniff(path):
    try:
        kind, enc = logprep.sniff(Path(path))
        return f"{kind}" + (f" ({enc})" if enc else "")
    except Exception:
        return "?"


def _files(folder, limit=40):
    try:
        items = [p for p in Path(folder).rglob("*") if p.is_file()]
    except OSError:
        return [], 0
    items.sort(key=lambda p: str(p).lower())
    rows = []
    for p in items[:limit]:
        try:
            rows.append([str(p.relative_to(folder)), _human(p.stat().st_size)])
        except (OSError, ValueError):
            continue
    return rows, len(items)


def _run_section(run, cfg):
    rid, d = run["id"], pipeline.run_dir(run["id"])
    inputs = run.get("inputs") or {}
    ex = pipeline.executor
    out = [f"## Open run `{rid}` — {run['issue']}", "",
           f"- Summary: {run.get('summary') or '(not loaded yet)'}",
           f"- Created: {_when(run.get('created'))} · last update: {_when(run.get('updated'))}",
           f"- AI mode used: {run.get('ai_mode')} · current step: {run.get('current_step') or '-'} · "
           f"pipeline running on this run: {'yes' if ex.busy and ex.run_id == rid else 'no'}",
           f"- Run folder: {d}"]
    usage = run.get("usage_total") or {}
    if usage:
        out.append(f"- Token usage so far: {json.dumps(usage, ensure_ascii=False)[:400]}")
    source = inputs.get("source_dir") or ""
    folder = pipeline.attachment_folder(run)
    log_opts = inputs.get("logsleuth") or {}
    out += ["", "### Inputs (this run)",
            f"- Handling mode (gate): {inputs.get('gate')} · use attachments: {inputs.get('auto_attachments')} · "
            f"code graph: {inputs.get('use_codegraph')} · infographic: {inputs.get('use_infographic')} · "
            f"continue on optional error: {inputs.get('continue_on_optional_error')} · "
            f"auto publish: {inputs.get('auto_publish_to_collab')}",
            f"- Source folder: {source or '(none)'}" + (" (missing!)" if source and not Path(source).is_dir() else ""),
            f"- Download folder: {folder} ({'exists' if folder.is_dir() else 'not created yet'}) · "
            f"auto-download cap: {cfg.get('auto_download_max_mb')} MB",
            f"- Log clock shift (minutes): {inputs.get('log_shift_minutes')}",
            f"- LogSleuth: mode {log_opts.get('mode', 'auto')} · catalog {log_opts.get('catalog_path') or '(built from source)'}",
            "", "### Steps"]
    out += _table(["#", "step", "status", "note", "error", "seconds"],
                  [[i + 1, s, run["steps"][s]["status"], run["steps"][s].get("note", ""),
                    _clip(run["steps"][s].get("error", ""), 300), run["steps"][s].get("elapsed_seconds", 0)]
                   for i, s in enumerate(STEP_IDS)])
    gate = run.get("pending_gate")
    out += ["", "### Evidence gate",
            ("WAITING for the user: " + json.dumps(gate, ensure_ascii=False)[:2500]) if gate else "not waiting",
            f"- gate state: {json.dumps(run.get('gate_state') or {}, ensure_ascii=False)[:400]}"]
    tz_wait = run.get("pending_tz")
    out += ["", "### Time zone decision",
            ("WAITING for the user (the ticket states no time zone): " + json.dumps(tz_wait, ensure_ascii=False)[:4000])
            if tz_wait else f"- decision: {json.dumps((run.get('gate_state') or {}).get('tz') or 'none yet', ensure_ascii=False)}"]
    focus = run.get("focus") or {}
    out += ["", "### Focus from step 02 (incident window, keywords, attachment picks)",
            json.dumps(focus, ensure_ascii=False)[:3000] if focus else "(none yet)"]
    items = run.get("attachments") or []
    picks = {str(p.get("filename", "")).casefold(): p.get("reason", "") for p in focus.get("attachments") or []
             if isinstance(p, dict)}
    out += ["", f"### Jira attachments ({len(items)})"]
    rows = []
    for a in items[:60]:
        path = a.get("path")
        note = []
        if attachments.is_7z_volume(a["filename"]):
            note.append("split 7z volume")
        if a["filename"].casefold() in picks:
            note.append("AI pick: " + (picks[a["filename"].casefold()] or "-"))
        rows.append([a["filename"], _human(a.get("size")), a.get("kind", ""), a.get("created", ""),
                     "yes" if path and Path(path).exists() else "no", "yes" if a.get("analyzed") else "no",
                     "; ".join(note)])
    out += _table(["file", "size", "kind", "created", "downloaded", "analysed", "note"], rows) or ["(none listed)"]
    if run.get("att_progress"):
        out.append(f"- Download in progress: {json.dumps(run['att_progress'], ensure_ascii=False)}")
    if folder.is_dir():
        files, total = _files(folder)
        out += ["", f"### Files in the download folder ({total})", *_table(["file", "size"], files)]
    logs = inputs.get("log_files") or []
    out += ["", f"### Log files selected for analysis ({len(logs)})"]
    out += _table(["file", "size", "type"],
                  [[f, _human(Path(f).stat().st_size) if Path(f).is_file() else "missing", _sniff(f)]
                   for f in logs[:40]]) or ["(none)"]
    prepared = d / "logs-prepared"
    if prepared.is_dir():
        files, total = _files(prepared, 30)
        out += ["", f"### Prepared logs for LogSleuth ({total})", *_table(["file", "size"], files)]
    span = d / "03-log-span.md"
    if span.is_file():
        out += ["", "### Log time span (03-log-span.md, excerpt)", _tail(span, 40, 2500)]
    artifacts = sorted(p for p in d.glob("0*") if p.is_file())
    out += ["", "### Artifacts in the run folder (open them when needed)",
            *_table(["file", "size"], [[p.name, _human(p.stat().st_size)] for p in artifacts])]
    out += ["", "### Run log (last lines)", "```text", _tail(d / "run.log", 80, 9000), "```"]
    return out


def snapshot(run, cfg):
    """Markdown picture of the app right now, bounded to SNAPSHOT_LIMIT characters."""
    ex = pipeline.executor
    out = [f"# Ticket Analyzer snapshot — {datetime.now():%Y-%m-%d %H:%M:%S}", "", "## App",
           f"- Ticket Analyzer {__version__} · Python {platform.python_version()} · {platform.platform()}",
           f"- Pipeline: {'RUNNING run `' + str(ex.run_id) + '`' if ex.busy else 'idle'}",
           f"- Data folder: {paths.DATA} · local bug tickets: {BUGS}", "",
           "## Settings (non-secret; keys usable in update_settings)"]
    out += [f"- {k}: {json.dumps(v, ensure_ascii=False)}" for k, v in cfg.items()]
    out += ["", "## Environment checks"]
    checks = _doctor()
    if checks.get("error"):
        out.append(f"(could not run checks: {checks['error']})")
    else:
        out.append(f"- Ready for mode {checks.get('mode')}: {checks.get('ready')}")
        out += _table(["check", "ok", "needed", "detail"],
                      [[c["label"], c["ok"], c["needed"], c["detail"]] for c in checks.get("checks") or []])
    out += ["", "## Recent app errors"]
    out += [f"- {_when(e['time'])} [{e['source']}] {e['message']}" for e in list(_errors)[-15:]] or ["(none)"]
    out += [""]
    out += _run_section(run, cfg) if run else ["## Open run", "No ticket is open in the UI (new-ticket draft)."]
    out += ["", "## Recent runs"]
    out += _table(["run", "issue", "created", "steps done", "failed"],
                  [[r["id"], r["issue"], _when(r["created"]), r["done"], r["failed"]]
                   for r in pipeline.list_runs(8)]) or ["(none)"]
    out += ["", "## Local bug tickets (do not duplicate)"]
    out += [f"- {b['file']} · {b['severity']} · {b['status']} · {b['title']}" for b in list_bugs()[:20]] or ["(none)"]
    out += ["", "## Readable folders", *[f"- {d}" for d in _readable_dirs(run)]]
    text = "\n".join(str(line) for line in out)
    return text if len(text) <= SNAPSHOT_LIMIT else text[:SNAPSHOT_LIMIT] + "\n\n…(snapshot truncated)"
