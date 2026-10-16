"""Python wrapper around logsleuth/cli.js (Node)."""
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from .. import paths


class LogSleuthError(RuntimeError):
    pass


def _run(command, request, timeout=900):
    node = shutil.which("node")
    if not node:
        raise LogSleuthError("Node.js is not installed. Open Setup.")
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(request, f)
        req = f.name
    try:
        out = subprocess.run([node, str(paths.LOGSLEUTH_CLI), command, req], capture_output=True,
                             timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    finally:
        Path(req).unlink(missing_ok=True)
    text = out.stdout.decode("utf-8", "replace").strip().splitlines()
    try:
        result = json.loads(text[-1]) if text else {}
    except ValueError:
        result = {}
    if out.returncode != 0 or "error" in result:
        raise LogSleuthError(result.get("error") or out.stderr.decode("utf-8", "replace")[-1500:] or "LogSleuth failed")
    return result


def catalog_for(source_dir, force=False):
    """Scan the source tree once and cache the catalog per folder."""
    key = hashlib.sha1(str(Path(source_dir).resolve()).lower().encode()).hexdigest()[:12]
    out = paths.CATALOGS / f"catalog-{Path(source_dir).name}-{key}.md"
    if force or not out.exists():
        paths.CATALOGS.mkdir(parents=True, exist_ok=True)
        _run("scan", {"root": str(source_dir), "out": str(out)})
    return out


def span(files):
    return _run("span", {"files": [str(f) for f in files]}, timeout=600)["files"]


def slice_logs(files, out, catalog=None, focus=None, budget_tokens=50000):
    focus = focus or {}
    request = {
        "catalog": str(catalog) if catalog else None,
        "files": [str(f) for f in files],
        "out": str(out),
        "budgetTokens": int(budget_tokens),
        "timeStart": focus.get("time_start") or "",
        "timeEnd": focus.get("time_end") or "",
        "extraGrep": [_escape(k) for k in focus.get("keywords") or [] if str(k).strip()],
        "services": focus.get("services") or [],
    }
    try:
        return _run("slice", request)["stats"]
    except LogSleuthError as exc:
        if request["services"] and "No catalog entries" in str(exc):
            request["services"] = []  # AI guessed a service name the catalog does not know
            return _run("slice", request)["stats"]
        raise


def _escape(text):
    import re
    return re.escape(str(text).strip())
