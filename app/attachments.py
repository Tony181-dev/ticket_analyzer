"""Jira attachments: list, selective download, safe extraction, log discovery.

Listing   : from the jira_get_issue result (mcp-atlassian includes attachments),
            else Jira REST /rest/api/2/issue/<key>?fields=attachment.
Download  : each attachment URL is fetched with the Jira credentials from the MCP config
            (token -> Bearer, user + password/API token -> Basic), like the Ez Collab-Jira
            extension: multi-connection axel (settings, else the copy bundled in
            app/axel-for-windows-master, else PATH), a plain HTTP stream when axel is unusable.
            A file that already exists with the same size is not downloaded again.
Extraction: zip / tar / tgz via the standard library; 7z (including split volumes) via py7zr;
            rar via Windows tar.exe (libarchive). Paths escaping the target folder are refused.
"""
import base64
import json
import os
import re
import shutil
import ssl
import subprocess
import tarfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from . import mcp, paths, settings

LOG_EXT = {".log", ".txt", ".dlt", ".out", ".slog", ".slog2", ".trace", ".err", ".jsonl", ".ndjson", ".json", ".gz", ".bz2", ".xz", ".zst", ".csv"}
ARCHIVE_EXT = (".zip", ".tar", ".tgz", ".tar.gz", ".tar.bz2", ".tbz2", ".tar.xz", ".7z", ".rar")
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".mp4", ".mov", ".avi", ".mkv", ".pdf", ".doc", ".docx",
            ".xls", ".xlsx", ".ppt", ".pptx", ".exe", ".dll", ".apk", ".so", ".bin", ".img"}
MAX_EXTRACT_BYTES = 8 * 1024 ** 3  # zip-bomb guard: 8 GB unpacked per archive
_SPLIT_7Z = re.compile(r"^(?P<base>.+\.7z)\.(?P<index>\d{3,})$", re.I)


def _split_7z_volume(name):
    leaf = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    match = _SPLIT_7Z.fullmatch(leaf)
    return (match.group("base"), match.group("index")) if match else None


def is_7z_volume(name):
    return _split_7z_volume(name) is not None


def is_7z_volume_continuation(name):
    volume = _split_7z_volume(name)
    return bool(volume and int(volume[1]) > 1)


def expand_7z_volumes(names, available):
    """If a split 7z part is selected, include every matching volume attachment."""
    groups = {}
    for candidate in available:
        volume = _split_7z_volume(candidate)
        if volume:
            groups.setdefault(volume[0].casefold(), set()).add(str(candidate))
    expanded = set(names)
    for name in tuple(expanded):
        volume = _split_7z_volume(name)
        if volume:
            expanded.update(groups.get(volume[0].casefold(), ()))
    return expanded


def default_folder(issue):
    return Path.home() / "Downloads" / "TicketAnalyzer" / issue


def kind_of(name):
    low = name.lower()
    if low.endswith(ARCHIVE_EXT) or is_7z_volume(name):
        return "archive"
    ext = Path(low).suffix
    if ext in LOG_EXT or re.search(r"\.\d+$", low):  # rotated logs: app.log.1
        return "log"
    return "other"


def is_log_file(path):
    p = Path(path)
    low = p.name.lower()
    if is_7z_volume(low):
        return False
    if p.suffix.lower() in SKIP_EXT:
        return False
    if p.suffix.lower() in LOG_EXT or re.search(r"\.\d+$", low):
        return True
    if p.suffix:
        return False
    from . import logprep  # extensionless files (syslog, messages, logcat): decide by content
    try:
        return logprep.sniff(p)[0] in ("text", "dlt", "gzip", "bz2", "xz")
    except OSError:
        return False


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


_UNITS = {"": 1, "b": 1, "byte": 1, "bytes": 1, "k": 1024, "kb": 1024, "kib": 1024,
          "m": 1024 ** 2, "mb": 1024 ** 2, "mib": 1024 ** 2, "g": 1024 ** 3, "gb": 1024 ** 3, "gib": 1024 ** 3,
          "t": 1024 ** 4, "tb": 1024 ** 4, "tib": 1024 ** 4}


def parse_size(value):
    """Bytes from an int or a human string such as "77 kB", "1.5 MB", "1,2 MB", "12345"; 0 when unknown."""
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, (int, float)):
        return max(int(value), 0)
    m = re.match(r"^\s*([\d.,]+)\s*([a-zA-Z]*)\s*$", str(value))
    if not m:
        return 0
    num, unit = m.group(1), m.group(2).lower()
    if unit not in _UNITS:
        return 0
    if "," in num and "." not in num and not re.fullmatch(r"\d{1,3}(,\d{3})+", num):
        num = num.replace(",", ".")  # decimal comma: "1,2 MB"
    else:
        num = num.replace(",", "")   # thousands separator: "12,345"
    try:
        return int(float(num) * _UNITS[unit])
    except ValueError:
        return 0


def _normalize(items):
    out, seen = [], set()
    for a in items:
        name = a.get("filename") or a.get("name")
        aid = str(a.get("id") or "")
        if not name or (aid, name) in seen:
            continue
        seen.add((aid, name))
        out.append({
            "id": aid or name, "filename": name, "size": parse_size(a.get("size")),
            "mime": a.get("mimeType") or a.get("content_type") or a.get("mime_type") or "",
            "created": a.get("created") or "", "url": a.get("content") or a.get("url") or "",
            "kind": kind_of(name),
        })
    return out


def _plain(env, keys):
    """First real value; VS Code ${input:...} placeholders cannot be resolved here."""
    for key in keys:
        value = str(env.get(key) or "").strip()
        if value and "${" not in value:
            return value
    return ""


def _jira_auth():
    """Same rules as the Ez Collab-Jira extension: a token is sent as Bearer, user + password/API token as Basic."""
    entry = mcp.load()["jira"] or {}
    env = entry.get("env") or {}
    base = (env.get("JIRA_URL") or "").rstrip("/")
    pat = _plain(env, ("JIRA_PERSONAL_TOKEN",))
    if pat:
        return base, {"Authorization": "Bearer " + pat}
    secret = _plain(env, ("JIRA_API_TOKEN", "JIRA_PASSWORD", "JIRA_TOKEN", "ATLASSIAN_API_TOKEN"))
    user = _plain(env, ("JIRA_USERNAME", "JIRA_USER", "ATLASSIAN_USERNAME"))
    if user and secret:
        return base, {"Authorization": "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode()}
    if secret:
        return base, {"Authorization": "Bearer " + secret}
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
AXEL_CONNECTIONS = 30
_AXEL_PERCENT = re.compile(r"\[\s*(\d{1,3})%\]")
_AXEL_BANNER = re.compile(r"axel\s+(?:version\s+)?v?\d+\.\d+|usage:\s*axel", re.I)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_probed = {}


def _safe_name(name):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "attachment"


def axel_version(exe):
    """Banner line of a real axel, '' otherwise (some distributions ship an interactive axel.cmd wizard)."""
    if exe not in _probed:
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=15, errors="replace",
                                 stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
            text = (out.stdout or "") + (out.stderr or "")
            _probed[exe] = next((ln.strip() for ln in text.splitlines() if _AXEL_BANNER.search(ln)), "")
        except (OSError, subprocess.SubprocessError):
            _probed[exe] = ""
    return _probed[exe]


def axel_path():
    """Configured command, else the axel bundled with the app, else axel on PATH; None = plain HTTP."""
    if "TA_AXEL" in os.environ:  # tests / debugging override; empty disables axel
        candidates = [os.environ["TA_AXEL"]]
    else:
        candidates = [settings.load().get("axel_command") or "", str(paths.AXEL_DIR / "axel.exe"), "axel"]
    for candidate in candidates:
        exe = shutil.which(candidate.strip()) if candidate.strip() else None
        if exe and (os.name != "nt" or exe.lower().endswith(".exe")) and axel_version(exe):
            return exe
    return None


def _connections():
    try:
        return max(1, min(64, int(settings.load().get("axel_connections") or AXEL_CONNECTIONS)))
    except (TypeError, ValueError):
        return AXEL_CONNECTIONS


def _redact(text):
    return re.sub(r"(Authorization:\s*(?:Bearer|Basic)\s+)\S+", r"\1***", str(text), flags=re.I)


def _discard(*files):
    for f in files:
        for _ in range(6):  # a just-killed process can hold the handle for an instant on Windows
            try:
                Path(f).unlink(missing_ok=True)
                break
            except OSError:
                time.sleep(0.12)


def _fetch_http(url, headers, target, cancel, report):
    part = target.with_name(target.name + ".part")
    try:
        with _open(url, headers, timeout=120) as r, open(part, "wb") as f:
            total, got = int(r.headers.get("Content-Length") or 0), 0
            while chunk := r.read(256 * 1024):
                cancel()
                f.write(chunk)
                got += len(chunk)
                if total:
                    report(got * 100 // total)
        part.replace(target)
    except BaseException:
        _discard(part)
        raise


def _fetch_axel(axel, url, headers, target, cancel, report, expected=0):
    part = target.with_name(target.name + ".part")
    state = part.with_name(part.name + ".st")
    _discard(part, state)
    args = [axel, "-n", str(_connections())]
    for key, value in headers.items():
        args += ["-H", f"{key}: {value}"]
    proc = subprocess.Popen(args + ["-o", str(part), url], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            cwd=str(target.parent), creationflags=_NO_WINDOW)
    tail = []

    def drain():  # axel redraws its progress line with \r, so read raw chunks rather than lines
        while chunk := proc.stdout.read1(512):
            text = chunk.decode("utf-8", "replace")
            tail.append(text)
            del tail[:-40]
            for m in _AXEL_PERCENT.finditer(text):
                report(min(100, int(m.group(1))))
    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        while proc.poll() is None:
            cancel()
            if expected:  # axel does not always print a percentage; the file on disk shows the real progress
                try:
                    report(min(99, part.stat().st_size * 100 // expected))
                except OSError:
                    pass
            time.sleep(0.2)
        reader.join(2)
        if proc.returncode != 0:
            detail = _redact("".join(tail)).strip().splitlines()[-3:]
            raise RuntimeError(f"axel exited with code {proc.returncode}: {' '.join(detail)}")
        part.replace(target)
        _discard(state)
    except BaseException:
        if proc.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/pid", str(proc.pid), "/T", "/F"], capture_output=True, timeout=5,
                               creationflags=_NO_WINDOW)
            else:
                proc.kill()
            proc.wait()
        _discard(part, state)
        raise


def download(issue, items, folder, log=print, cancel=lambda: None, progress=lambda *a: None):
    """Download `items` (normalized dicts) into `folder` with the Jira credentials; returns {filename: path}.

    progress(phase, index, total, name, pct=None) per file: "download" (started / pct), then "downloaded",
    "present" (same size already on disk) or "failed"; a final "download" with total/total and no name.

    Like the Ez Collab-Jira extension: the attachment URL is fetched with an Authorization header, using
    multi-connection axel when it is installed and a plain HTTP stream otherwise.
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    base, headers = _jira_auth()
    axel = axel_path()
    done, errors = {}, []
    for n, a in enumerate(items):
        cancel()
        name = a["filename"]
        progress("download", n, len(items), name)
        target = folder / _safe_name(name)
        if target.exists() and a["size"] and target.stat().st_size == a["size"]:
            log(f"= {name} (đã có / already present)")
            done[name] = target
            progress("present", n, len(items), name, 100)
            continue
        url = a.get("url") or (f"{base}/secure/attachment/{a['id']}/{urllib.parse.quote(name)}" if base else "")
        if not url:
            errors.append(f"{name}: no download URL")
            log(f"✗ {errors[-1]}")
            progress("failed", n, len(items), name)
            continue
        last = [0.0, -1]

        def report(pct, n=n, name=name, last=last):  # throttled: at most ~2 updates a second
            if pct != last[1] and (pct >= 100 or time.monotonic() - last[0] > 0.4):
                last[:] = [time.monotonic(), pct]
                progress("download", n, len(items), name, pct)
        problems = []
        for how in (["axel"] if axel else []) + ["http"]:
            try:
                if how == "axel":
                    _fetch_axel(axel, url, headers, target, cancel, report, a["size"])
                else:
                    _fetch_http(url, headers, target, cancel, report)
                log(f"↓ {name} ({target.stat().st_size:,} B) [{how}]")
                done[name] = target
                progress("downloaded", n, len(items), name, 100)
                break
            except Exception as exc:
                cancel()  # re-raises when the user pressed Stop
                problems.append(f"{how}: {_redact(exc)}")
        else:
            errors.append(f"{name}: " + " | ".join(problems))
            log(f"✗ {errors[-1]}")
            progress("failed", n, len(items), name)
    if errors and not done:
        raise RuntimeError("; ".join(errors))
    progress("download", len(items), len(items), "")
    return done


# ---------------------------------------------------------------- extraction
def _inside(root, member):
    root = Path(root).resolve()
    dest = (root / member).resolve()
    return dest == root or root in dest.parents


def _extract_7z(archive, out, cancel):
    try:
        import py7zr
        from py7zr.callbacks import ExtractCallback
    except ImportError as exc:
        raise RuntimeError("7z extraction requires py7zr; install the app requirements") from exc

    source = archive
    volume_stream = None
    volume = _split_7z_volume(archive.name)
    if volume:
        base_name, index = volume
        if int(index) != 1:
            raise RuntimeError(f"start split 7z extraction from {base_name}.001")
        pattern = re.compile(r"^" + re.escape(base_name) + r"\.(\d{3,})$", re.I)
        found = {}
        for candidate in archive.parent.iterdir():
            match = pattern.fullmatch(candidate.name)
            if match and candidate.is_file():
                found[int(match.group(1))] = candidate
        last = max(found, default=0)
        missing = [f"{base_name}.{n:0{len(index)}d}" for n in range(1, last + 1) if n not in found]
        if 1 not in found:
            raise RuntimeError(f"missing first 7z volume: {base_name}.001")
        if missing:
            raise RuntimeError("missing split 7z volume(s): " + ", ".join(missing))
        try:
            import multivolumefile
        except ImportError as exc:
            raise RuntimeError("split 7z extraction requires multivolumefile") from exc
        volume_stream = multivolumefile.MultiVolume(archive.with_suffix(""), mode="rb", ext_digits=len(index))
        source = volume_stream

    class CancelCallback(ExtractCallback):
        def report_start_preparation(self):
            cancel()

        def report_start(self, *_):
            cancel()

        def report_update(self, *_):
            cancel()

        def report_end(self, *_):
            cancel()

        def report_warning(self, *_):
            cancel()

        def report_postprocess(self):
            cancel()

    try:
        cancel()
        with py7zr.SevenZipFile(source, mode="r", max_extract_size=MAX_EXTRACT_BYTES) as z:
            members = z.list()
            if sum(int(info.uncompressed or 0) for info in members if info.is_file) > MAX_EXTRACT_BYTES:
                raise RuntimeError("archive too large when unpacked")
            for info in members:
                cancel()
                if not _inside(out, info.filename):
                    raise RuntimeError(f"unsafe path in archive: {info.filename}")
                if info.is_symlink:
                    raise RuntimeError(f"symbolic links are not allowed in archives: {info.filename}")
            z.extractall(path=out, callback=CancelCallback())
    finally:
        if volume_stream is not None:
            volume_stream.close()


def extract(archive, log=print, cancel=lambda: None, dest=None):
    """Extract once into `dest` (default '<archive>_extracted/'); returns that folder (or None)."""
    archive = Path(archive)
    out = Path(dest) if dest else archive.with_name(archive.name + "_extracted")
    if out.is_dir() and any(out.iterdir()):
        log(f"= {archive.name} (đã giải nén / already extracted)")
        return out
    out.mkdir(parents=True, exist_ok=True)
    low = archive.name.lower()
    try:
        if low.endswith(".zip") or zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as z:
                total = sum(i.file_size for i in z.infolist())
                if total > MAX_EXTRACT_BYTES:
                    raise RuntimeError(f"archive too large when unpacked ({total:,} B)")
                for info in z.infolist():
                    if not _inside(out, info.filename):
                        raise RuntimeError(f"unsafe path in archive: {info.filename}")
                for info in z.infolist():
                    cancel()
                    z.extract(info, out)
        elif low.endswith((".tar", ".tgz", ".tar.gz", ".tar.bz2", ".tbz2", ".tar.xz")) or (
                not low.endswith((".7z", ".rar")) and tarfile.is_tarfile(archive)):
            with tarfile.open(archive) as tf:
                members = [m for m in tf.getmembers() if (m.isfile() or m.isdir()) and _inside(out, m.name)]
                if sum(m.size for m in members) > MAX_EXTRACT_BYTES:
                    raise RuntimeError("archive too large when unpacked")
                for m in members:
                    cancel()
                    tf.extract(m, out)
        elif low.endswith(".7z") or is_7z_volume(archive.name):
            _extract_7z(archive, out, cancel)
        else:  # .rar: Windows 10+ ships bsdtar (libarchive)
            tar = shutil.which("tar")
            if not tar:
                raise RuntimeError("no extractor for " + archive.suffix)
            proc = subprocess.Popen([tar, "-xf", str(archive), "-C", str(out)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                while True:
                    try:
                        text, _ = proc.communicate(timeout=0.5)
                        break
                    except subprocess.TimeoutExpired:
                        cancel()
            except BaseException:
                proc.kill()
                proc.communicate()
                raise
            if proc.returncode != 0:
                raise RuntimeError((text or "").strip()[-400:])
            for p in out.rglob("*"):
                if not _inside(out, p.relative_to(out)):
                    raise RuntimeError("unsafe path in archive")
        log(f"⇲ {archive.name} → {out.name}")
        return out
    except BaseException as exc:
        shutil.rmtree(out, ignore_errors=True)
        if not isinstance(exc, Exception):
            raise
        cancel()  # re-raises when the user pressed Stop
        log(f"✗ {archive.name}: {exc}")
        return None


def logs_in(path):
    p = Path(path)
    if p.is_file():
        return [p] if is_log_file(p) else []
    return sorted(f for f in p.rglob("*")
                  if f.is_file() and f.stat().st_size > 0 and not is_7z_volume_continuation(f.name)
                  and (is_log_file(f) or kind_of(f.name) == "archive"))


def collect(issue, items, analyze_names, folder, log=print, cancel=lambda: None, progress=lambda *a: None):
    """Download `items`; for names in `analyze_names` extract and return the log files found."""
    files = download(issue, items, folder, log, cancel, progress)
    logs = []
    todo = [n for n in files if n in analyze_names and not is_7z_volume_continuation(n)]
    for k, name in enumerate(todo):
        path = files[name]
        cancel()
        progress("extract", k, len(todo), name)
        if kind_of(name) == "archive":
            out = extract(path, log, cancel)
            if out:
                logs += logs_in(out)
        elif path.suffix.lower() == ".gz" and not path.name.lower().endswith(".tar.gz"):
            logs.append(path)  # LogSleuth reads .gz directly
        else:
            logs += logs_in(path)
        progress("extracted", k, len(todo), name)
    if todo:
        progress("extract", len(todo), len(todo), "")
    return [str(p) for p in logs], {k: str(v) for k, v in files.items()}
