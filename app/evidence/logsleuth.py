"""Python wrapper around logsleuth/cli.js (Node)."""
import hashlib
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .. import paths
from .source_cache import source_fingerprint
from .timestamps import normalize_clock

MAX_BRIEF_TOKENS = 50000
MIN_BRIEF_TOKENS = 2000
PARSERS = ("auto", "dlt-binary", "dlt-text", "logcat-threadtime", "logcat-time", "logcat-brief",
           "slog2", "syslog", "syslog5424", "journal-iso", "log4j", "iso", "ts-level", "json", "generic")
DEFAULT_OPTIONS = {
    "mode": "auto", "catalog_path": "", "services": [], "time_enabled": False,
    "time_start": "", "time_end": "", "budget_tokens": MAX_BRIEF_TOKENS,
    "before": 5, "after": 12, "window_ms": 300, "max_repeat": 3,
    "min_level": "", "min_score": 0, "min_anchor_length": 5,
    "extra_grep": [], "exclude": [], "parser": "auto", "exclude_dirs": [], "max_file_size_mb": 2,
}


def normalize_options(options=None, budget_tokens=MAX_BRIEF_TOKENS):
    """Validate per-run settings before starting a background job."""
    if options is not None and not isinstance(options, dict):
        raise ValueError("LogSleuth options must be an object")
    out = {**DEFAULT_OPTIONS, "budget_tokens": budget_tokens, **(options or {})}
    out = {key: out[key] for key in DEFAULT_OPTIONS}
    if out["mode"] not in ("auto", "manual"):
        raise ValueError("LogSleuth mode must be auto or manual")
    if out["parser"] not in PARSERS or out["min_level"] not in ("", "V", "D", "I", "W", "E", "F"):
        raise ValueError("Invalid LogSleuth parser or severity")
    bounds = {"budget_tokens": (MIN_BRIEF_TOKENS, MAX_BRIEF_TOKENS), "before": (0, 100), "after": (0, 100),
              "window_ms": (0, 60000), "max_repeat": (1, 100), "min_score": (0, 100),
              "min_anchor_length": (1, 200), "max_file_size_mb": (1, 100)}
    for key, (lo, hi) in bounds.items():
        value = out[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi or int(value) != value:
            raise ValueError(f"{key} must be an integer between {lo} and {hi}")
        out[key] = int(value)
    for key in ("services", "extra_grep", "exclude", "exclude_dirs"):
        if not isinstance(out[key], list) or len(out[key]) > 200 or any(not isinstance(x, str) or len(x) > 1000 for x in out[key]):
            raise ValueError(f"{key} must be a list of at most 200 short strings")
        out[key] = [x.strip() for x in out[key] if x.strip()]
    if not isinstance(out["time_enabled"], bool):
        raise ValueError("time_enabled must be a boolean")
    for key in ("time_start", "time_end"):
        if out[key] not in (None, "") and normalize_clock(out[key]) is None:
            raise ValueError("Time window must use HH:MM:SS[.mmm]")
        out[key] = normalize_clock(out[key]) or ""
    if not isinstance(out["catalog_path"], str):
        raise ValueError("catalog_path must be a path")
    return out


class LogSleuthError(RuntimeError):
    pass


def _run(command, request, timeout=900, cancel=lambda: None):
    node = shutil.which("node")
    if not node:
        raise LogSleuthError("Node.js is not installed. Open Setup.")
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(request, f)
        req = f.name
    try:
        cancel()
        with subprocess.Popen([node, str(paths.LOGSLEUTH_CLI), command, req], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as proc:
            deadline = time.monotonic() + timeout
            try:
                while True:
                    cancel()
                    if time.monotonic() >= deadline:
                        raise LogSleuthError(f"LogSleuth {command} timed out after {timeout}s")
                    try:
                        stdout, stderr = proc.communicate(timeout=0.25)
                        break
                    except subprocess.TimeoutExpired:
                        continue
            except BaseException:
                proc.kill()
                proc.communicate()
                raise
    finally:
        Path(req).unlink(missing_ok=True)
    text = stdout.decode("utf-8", "replace").strip().splitlines()
    try:
        result = json.loads(text[-1]) if text else {}
    except ValueError:
        result = {}
    if proc.returncode != 0 or not result or "error" in result:
        raise LogSleuthError(result.get("error") or stderr.decode("utf-8", "replace")[-1500:] or "LogSleuth failed")
    return result


def catalog_for(source_dir, force=False, options=None, cancel=lambda: None):
    """Cache a catalog per folder and refresh it when source files change."""
    opt = normalize_options(options)
    scan = {"excludeDirs": opt["exclude_dirs"], "maxFileSizeMB": opt["max_file_size_mb"],
            "minAnchorLength": opt["min_anchor_length"]}
    key = hashlib.sha1((str(Path(source_dir).resolve()).lower() + json.dumps(scan, sort_keys=True)).encode()).hexdigest()[:12]
    out = paths.CATALOGS / f"catalog-{Path(source_dir).name}-{key}.md"
    fingerprint = source_fingerprint(source_dir)
    manifest = out.with_suffix(out.suffix + ".fingerprint")
    try:
        cached_fingerprint = manifest.read_text(encoding="utf-8").strip()
    except OSError:
        cached_fingerprint = ""
    if force or not out.exists() or cached_fingerprint != fingerprint:
        paths.CATALOGS.mkdir(parents=True, exist_ok=True)
        _run("scan", {"root": str(source_dir), "out": str(out), **scan}, cancel=cancel)
        tmp = manifest.with_suffix(manifest.suffix + ".tmp")
        tmp.write_text(fingerprint, encoding="utf-8")
        tmp.replace(manifest)
    return out


def span(files, cancel=lambda: None):
    """First/last timestamp of every log file (and whether it crosses midnight): the facts the
    incident window is aligned against."""
    return _run("span", {"files": [str(f) for f in files]}, timeout=600, cancel=cancel)["files"]


def catalog_info(catalog, cancel=lambda: None):
    return _run("catalog-info", {"catalog": str(catalog)}, cancel=cancel)


def list_catalogs():
    return [{"path": str(p), "name": p.name} for p in sorted(paths.CATALOGS.glob("catalog-*.md"),
            key=lambda p: p.stat().st_mtime, reverse=True)[:100]]


def slice_logs(files, out, catalog=None, focus=None, budget_tokens=50000, options=None, cancel=lambda: None):
    focus = focus or {}
    opt = normalize_options(options, budget_tokens=min(MAX_BRIEF_TOKENS, int(budget_tokens)))
    # Never exceed the configured application-wide ceiling, even in Manual mode.
    budget = min(opt["budget_tokens"], int(budget_tokens), MAX_BRIEF_TOKENS)
    request = {
        "catalog": str(catalog) if catalog else None,
        "files": [str(f) for f in files],
        "out": str(out),
        "budgetTokens": budget,
        "timeStart": focus.get("time_start") or "",
        "timeEnd": focus.get("time_end") or "",
        # More ranges in the same (log) clock; a line inside ANY window is kept.
        "timeWindows": [{"start": w.get("time_start") or "", "end": w.get("time_end") or ""}
                        for w in focus.get("extra_windows") or [] if w.get("time_start") or w.get("time_end")],
        "extraGrep": [_escape(k) for k in focus.get("keywords") or [] if str(k).strip()],
        "services": opt["services"] if opt["mode"] == "manual" else [],
        "before": opt["before"], "after": opt["after"], "windowMs": opt["window_ms"],
        "maxRepeat": opt["max_repeat"], "minLevel": opt["min_level"], "minScore": opt["min_score"],
        "minAnchorLength": opt["min_anchor_length"], "parser": opt["parser"], "exclude": opt["exclude"],
    }
    request["extraGrep"] += opt["extra_grep"]
    stats = _run("slice", request, cancel=cancel)["stats"]
    size = Path(out).stat().st_size
    if size > budget:
        Path(out).unlink(missing_ok=True)
        raise LogSleuthError("Log brief exceeded its conservative token cap")
    return stats


def _escape(text):
    import re
    return re.escape(str(text).strip())
