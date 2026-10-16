"""Jira attachments: list, selective download, safe extraction, log discovery.

Listing   : from the jira_get_issue result (mcp-atlassian includes attachments),
            else Jira REST /rest/api/2/issue/<key>?fields=attachment.
Download  : Jira REST with the token from the MCP config (selective, streamed to
            disk). Falls back to the MCP tool jira_download_attachments.
            A file that already exists with the same size is not downloaded again.
Extraction: zip / tar / tgz via the standard library; 7z / rar via Windows tar.exe
            (libarchive). Paths escaping the target folder are refused.
"""
import base64
import json
import os
import re
import shutil
import ssl
import subprocess
import tarfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from . import mcp

LOG_EXT = {".log", ".txt", ".dlt", ".out", ".slog", ".slog2", ".trace", ".err", ".jsonl", ".ndjson", ".json", ".gz", ".csv"}
ARCHIVE_EXT = (".zip", ".tar", ".tgz", ".tar.gz", ".tar.bz2", ".tbz2", ".tar.xz", ".7z", ".rar")
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".mp4", ".mov", ".avi", ".mkv", ".pdf", ".doc", ".docx",
            ".xls", ".xlsx", ".ppt", ".pptx", ".exe", ".dll", ".apk", ".so", ".bin", ".img"}
MAX_EXTRACT_BYTES = 8 * 1024 ** 3  # zip-bomb guard: 8 GB unpacked per archive


def default_folder(issue):
    return Path.home() / "Downloads" / "TicketAnalyzer" / issue


def kind_of(name):
    low = name.lower()
    if low.endswith(ARCHIVE_EXT):
        return "archive"
    ext = Path(low).suffix
    if ext in LOG_EXT or re.search(r"\.\d+$", low):  # rotated logs: app.log.1
        return "log"
    return "other"


def is_log_file(path):
    p = Path(path)
    low = p.name.lower()
    if p.suffix.lower() in SKIP_EXT:
        return False
    return p.suffix.lower() in LOG_EXT or bool(re.search(r"\.(log|txt)\.\d+$|\.\d+$", low)) or "log" in low and p.suffix == ""


# ---------------------------------------------------------------- listing
def from_issue_json(text):
    """Attachments from the jira_get_issue payload (simplified or raw REST shape)."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return []
    found = []

    def walk(value):
        if isinstance(value, dict):
            for key in ("attachments", "attachment"):
                items = value.get(key)
                if isinstance(items, list) and items and all(isinstance(x, dict) for x in items):
                    found.extend(items)
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
    walk(data)
    return _normalize(found)


def _normalize(items):
    out, seen = [], set()
    for a in items:
        name = a.get("filename") or a.get("name")
        aid = str(a.get("id") or "")
        if not name or (aid, name) in seen:
            continue
        seen.add((aid, name))
        out.append({
            "id": aid or name, "filename": name, "size": int(a.get("size") or 0),
            "mime": a.get("mimeType") or a.get("content_type") or a.get("mime_type") or "",
            "created": a.get("created") or "", "url": a.get("content") or a.get("url") or "",
            "kind": kind_of(name),
        })
    return out


def _jira_auth():
    entry = mcp.load()["jira"] or {}
    env = entry.get("env") or {}
    base = (env.get("JIRA_URL") or "").rstrip("/")
    if env.get("JIRA_PERSONAL_TOKEN"):
        return base, {"Authorization": "Bearer " + env["JIRA_PERSONAL_TOKEN"]}
    if env.get("JIRA_USERNAME") and env.get("JIRA_API_TOKEN"):
        raw = f"{env['JIRA_USERNAME']}:{env['JIRA_API_TOKEN']}".encode()
        return base, {"Authorization": "Basic " + base64.b64encode(raw).decode()}
    return base, {}


def _open(url, headers, timeout=60):
    req = urllib.request.Request(url, headers={"Accept": "*/*", **headers})
    ctx = ssl.create_default_context()  # Windows: includes the OS (corporate) root store
    return urllib.request.urlopen(req, timeout=timeout, context=ctx)


def list_rest(issue):
    base, headers = _jira_auth()
    if not base:
        return []
    with _open(f"{base}/rest/api/2/issue/{urllib.parse.quote(issue)}?fields=attachment", headers) as r:
        data = json.load(r)
    return _normalize(((data.get("fields") or {}).get("attachment")) or [])


# ---------------------------------------------------------------- download
def _safe_name(name):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "attachment"


def download(issue, items, folder, log=print, cancel=lambda: None):
    """Download `items` (normalized dicts) into `folder`; returns {filename: path}."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    base, headers = _jira_auth()
    done, failed = {}, []
    for a in items:
        cancel()
        target = folder / _safe_name(a["filename"])
        if target.exists() and a["size"] and target.stat().st_size == a["size"]:
            log(f"= {a['filename']} (đã có / already present)")
            done[a["filename"]] = target
            continue
        url = a.get("url") or (f"{base}/secure/attachment/{a['id']}/{urllib.parse.quote(a['filename'])}" if base else "")
        try:
            if not url:
                raise RuntimeError("no download URL")
            tmp = target.with_suffix(target.suffix + ".part")
            with _open(url, headers, timeout=120) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f, 1024 * 1024)
            tmp.replace(target)
            log(f"↓ {a['filename']} ({target.stat().st_size:,} B)")
            done[a["filename"]] = target
        except Exception as exc:
            log(f"REST {a['filename']}: {exc}")
            failed.append(a)
    if failed:
        done.update(_download_via_mcp(issue, failed, folder, log))
    return done


def _download_via_mcp(issue, items, folder, log):
    """Fallback: mcp-atlassian returns every attachment base64-encoded."""
    wanted = {a["filename"] for a in items}
    try:
        result = mcp.call("jira", "jira_download_attachments", {"issue_key": issue}, timeout=600)
    except Exception as exc:
        log(f"MCP jira_download_attachments: {exc}")
        return {}
    done = {}
    for item in (result or {}).get("content", []):
        res = item.get("resource") if item.get("type") == "resource" else None
        blob = (res or {}).get("blob") or item.get("data")
        uri = (res or {}).get("uri") or item.get("name") or ""
        name = urllib.parse.unquote(uri.rsplit("/", 1)[-1]) if uri else ""
        if not blob or name not in wanted:
            continue
        target = Path(folder) / _safe_name(name)
        target.write_bytes(base64.b64decode(blob))
        log(f"↓ {name} (MCP)")
        done[name] = target
    return done


# ---------------------------------------------------------------- extraction
def _inside(root, member):
    root = Path(root).resolve()
    dest = (root / member).resolve()
    return dest == root or root in dest.parents


def extract(archive, log=print):
    """Extract once into '<archive>_extracted/'; returns that folder (or None)."""
    archive = Path(archive)
    out = archive.with_name(archive.name + "_extracted")
    if out.is_dir() and any(out.iterdir()):
        log(f"= {archive.name} (đã giải nén / already extracted)")
        return out
    out.mkdir(exist_ok=True)
    low = archive.name.lower()
    try:
        if low.endswith(".zip"):
            with zipfile.ZipFile(archive) as z:
                total = sum(i.file_size for i in z.infolist())
                if total > MAX_EXTRACT_BYTES:
                    raise RuntimeError(f"archive too large when unpacked ({total:,} B)")
                for info in z.infolist():
                    if not _inside(out, info.filename):
                        raise RuntimeError(f"unsafe path in archive: {info.filename}")
                z.extractall(out)
        elif low.endswith((".tar", ".tgz", ".tar.gz", ".tar.bz2", ".tbz2", ".tar.xz")):
            with tarfile.open(archive) as tf:
                members = [m for m in tf.getmembers() if (m.isfile() or m.isdir()) and _inside(out, m.name)]
                if sum(m.size for m in members) > MAX_EXTRACT_BYTES:
                    raise RuntimeError("archive too large when unpacked")
                tf.extractall(out, members=members)
        else:  # .7z / .rar: Windows 10+ ships bsdtar (libarchive)
            tar = shutil.which("tar")
            if not tar:
                raise RuntimeError("no extractor for " + archive.suffix)
            proc = subprocess.run([tar, "-xf", str(archive), "-C", str(out)], capture_output=True, text=True,
                                  timeout=1800, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if proc.returncode != 0:
                raise RuntimeError((proc.stderr or proc.stdout).strip()[-400:])
            for p in out.rglob("*"):
                if not _inside(out, p.relative_to(out)):
                    raise RuntimeError("unsafe path in archive")
        log(f"⇲ {archive.name} → {out.name}")
        return out
    except Exception as exc:
        shutil.rmtree(out, ignore_errors=True)
        log(f"✗ {archive.name}: {exc}")
        return None


def logs_in(path):
    p = Path(path)
    if p.is_file():
        return [p] if is_log_file(p) else []
    return sorted(f for f in p.rglob("*") if f.is_file() and is_log_file(f) and f.stat().st_size > 0)


def collect(issue, items, analyze_names, folder, log=print, cancel=lambda: None):
    """Download `items`; for names in `analyze_names` extract and return the log files found."""
    files = download(issue, items, folder, log, cancel)
    logs = []
    for name, path in files.items():
        if name not in analyze_names:
            continue
        cancel()
        if kind_of(name) == "archive":
            out = extract(path, log)
            if out:
                logs += logs_in(out)
        elif path.suffix.lower() == ".gz" and not path.name.lower().endswith(".tar.gz"):
            logs.append(path)  # LogSleuth reads .gz directly
        else:
            logs += logs_in(path)
    return [str(p) for p in logs], {k: str(v) for k, v in files.items()}
