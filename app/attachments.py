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
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".mp4", ".mov", ".avi", ".mkv", ".pdf", ".doc", ".docx",
            ".xls", ".xlsx", ".ppt", ".pptx", ".exe", ".dll", ".apk", ".so", ".bin", ".img"}
MAX_EXTRACT_BYTES = 8 * 1024 ** 3  # zip-bomb guard: 8 GB unpacked per archive
EXTRACT_MARKER = ".ta-extracted.json"
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


def archive_volume(name):
    """(group key, 1-based index) for numbered 7z/zip and partNN.rar volumes."""
    leaf = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    match = re.fullmatch(r"(.+\.(?:7z|zip))\.(\d{3,})", leaf, re.I)
    if match:
        return match[1].casefold(), int(match[2])
    match = re.fullmatch(r"(.+)\.part(\d+)\.rar", leaf, re.I)
    return (match[1].casefold() + ".part.rar", int(match[2])) if match else None


def is_volume_continuation(name):
    volume = archive_volume(name)
    return bool(volume and volume[1] > 1)


def expand_archive_volumes(names, available):
    """Selecting any split archive part selects its entire available set."""
    groups = {}
    for candidate in available:
        volume = archive_volume(candidate)
        if volume:
            groups.setdefault(volume[0].casefold(), set()).add(str(candidate))
    expanded = set(names)
    for name in tuple(expanded):
        volume = archive_volume(name)
        if volume:
            expanded.update(groups.get(volume[0].casefold(), ()))
    return expanded


def expand_7z_volumes(names, available):
    """Compatibility alias; also supports numbered ZIP and multipart RAR."""
    return expand_archive_volumes(names, available)


def complete_volume_set(names):
    groups = {}
    for name in names:
        if volume := archive_volume(name):
            groups.setdefault(volume[0], set()).add(volume[1])
    return all(min(indices) == 1 and len(indices) == max(indices) for indices in groups.values())


def default_folder(issue):
    return Path.home() / "Downloads" / "TicketAnalyzer" / issue


def kind_of(name):
    low = name.lower()
    if low.endswith(ARCHIVE_EXT) or archive_volume(name):
        return "archive"
    ext = Path(low).suffix
    if ext in IMAGE_EXT:
        return "image"
    if ext in LOG_EXT or re.search(r"\.\d+$", low):  # rotated logs: app.log.1
        return "log"
    return "other"


def is_log_file(path):
    p = Path(path)
    if p.name == EXTRACT_MARKER:
        return False
    low = p.name.lower()
    if archive_volume(low):
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
AXEL_CONNECTIONS = 8           # per attachment; dozens mostly buy connection resets from corporate servers
AXEL_MIN_BYTES = 1024 ** 2     # below this one HTTP stream is as fast as starting axel
AXEL_PART_BYTES = 512 * 1024   # each axel connection gets at least this much of the file
# axel 2.4 waits 20 s before it reconnects a connection the server reset (30 connections on a busy
# server: 20 MB in 19 s instead of 1 s). It reads $HOME/.axelrc, so the app hands it its own.
AXEL_RC = "reconnect_delay = 1\nconnection_timeout = 30\nbuffer_size = 65536\n"
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


def axel_https(exe):
    """axel learned HTTPS in 2.5; the bundled 2.4 speaks plain HTTP (and FTP) only."""
    m = re.search(r"(\d+)\.(\d+)", axel_version(exe) or "")
    return bool(m) and (int(m.group(1)), int(m.group(2))) >= (2, 5)


def _connections():
    try:
        return max(1, min(64, int(settings.load().get("axel_connections") or AXEL_CONNECTIONS)))
    except (TypeError, ValueError):
        return AXEL_CONNECTIONS


def _axel_env():
    """axel's environment: HOME holds the app's .axelrc; cygwin stays quiet about Windows paths."""
    home = paths.DATA / "axel"
    rc = home / ".axelrc"
    try:
        if not rc.is_file() or rc.read_text(encoding="utf-8") != AXEL_RC:
            home.mkdir(parents=True, exist_ok=True)
            rc.write_text(AXEL_RC, encoding="utf-8", newline="\n")
    except OSError:
        pass  # axel still works on its own defaults
    return {**os.environ, "HOME": str(home), "CYGWIN": "nodosfilewarning"}


def _probe(url, headers, timeout=30):
    """(final URL after redirects, total bytes or 0, Range support True/False/None=unknown) from a
    one-byte ranged GET. axel 2.4 cannot follow a relative redirect, so it is given the final URL."""
    try:
        with _open(url, {**headers, "Range": "bytes=0-0"}, timeout=timeout) as r:
            final, ranged = r.geturl(), r.status == 206
            total = re.search(r"/\s*(\d+)\s*$", r.headers.get("Content-Range") or "")
            size = int(total.group(1)) if total else 0 if ranged else int(r.headers.get("Content-Length") or 0)
            return final, size, ranged
    except Exception:
        return url, 0, None


def _axel_plan(axel, url, headers, declared, left):
    """How axel fetches this attachment: {url, size, connections}, or (None, why) when one plain HTTP
    stream is as fast or the only safe way."""
    final, size, ranged = _probe(url, headers)
    size = size or declared or 0
    if ranged is False:
        return None, "no Range support"
    if size and size < AXEL_MIN_BYTES:
        return None, "small file"
    if left is not None and (not size or size > left):
        return None, "download budget"  # the bounded HTTP stream enforces the cap byte by byte
    if urllib.parse.urlsplit(final).scheme.lower() not in (("http", "https") if axel_https(axel) else ("http",)):
        return None, "HTTPS needs axel 2.5+"
    connections = _connections() if not size else max(1, min(_connections(), size // AXEL_PART_BYTES))
    return {"url": final, "size": size, "connections": connections}, ""


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


def _fetch_http(url, headers, target, cancel, report, max_bytes=None):
    part = target.with_name(target.name + ".part")
    try:
        with _open(url, headers, timeout=120) as r, open(part, "wb") as f:
            if r.status != 200:
                raise RuntimeError(f"attachment download requires HTTP 200, received {r.status}")
            if "text/html" in r.headers.get("Content-Type", "").lower() and target.suffix.lower() not in (".html", ".htm", ".xhtml"):
                raise RuntimeError("attachment URL returned an HTML page instead of the file; check Jira sign-in/access")
            total, got = int(r.headers.get("Content-Length") or 0), 0
            if max_bytes is not None and total > max_bytes:
                raise RuntimeError("attachment exceeds remaining download budget")
            while chunk := r.read(min(256 * 1024, max_bytes - got + 1) if max_bytes is not None else 256 * 1024):
                cancel()
                if max_bytes is not None and got + len(chunk) > max_bytes:
                    raise RuntimeError("attachment exceeds remaining download budget")
                f.write(chunk)
                got += len(chunk)
                if total:
                    report(min(99, got * 100 // total))
            cancel()
            if r.headers.get("Content-Length") is not None and got != total:
                raise RuntimeError(f"incomplete attachment download: received {got:,} B, expected {total:,} B")
        part.replace(target)
    except BaseException:
        _discard(part)
        raise


def _fetch_axel(axel, url, headers, target, cancel, report, expected=0, connections=None):
    part = target.with_name(target.name + ".part")
    state = part.with_name(part.name + ".st")
    _discard(part, state)
    # -a: one "[ 45%] [...] [ 2.9MB/s] [00:06]" line per update. The size of the file on disk says
    # nothing about progress: every connection writes at its own offset, so it jumps to ~100% at once.
    args = [axel, "-a", "-n", str(connections or _connections())]
    for key, value in headers.items():
        args += ["-H", f"{key}: {value}"]
    proc = subprocess.Popen(args + ["-o", str(part), url], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, cwd=str(target.parent), env=_axel_env(),
                            creationflags=_NO_WINDOW)
    tail = []

    def drain():  # axel redraws its progress line with \r, so read raw chunks rather than lines
        while chunk := proc.stdout.read1(512):
            text = chunk.decode("utf-8", "replace")
            tail.append(text)
            del tail[:-40]
            for m in _AXEL_PERCENT.finditer(text):
                report(min(99, int(m.group(1))))
    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        while proc.poll() is None:
            cancel()
            time.sleep(0.2)
        reader.join(2)
        if proc.returncode != 0:
            detail = _redact("".join(tail)).strip().splitlines()[-3:]
            raise RuntimeError(f"axel exited with code {proc.returncode}: {' '.join(detail)}")
        got = part.stat().st_size
        if expected and got != expected:
            raise RuntimeError(f"axel wrote {got:,} B, expected {expected:,} B")
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
    finally:
        reader.join(2)  # the pipe hits EOF once axel is gone
        proc.stdout.close()


def download(issue, items, folder, log=print, cancel=lambda: None, progress=lambda *a: None, max_bytes=None):
    """Download `items` (normalized dicts) into `folder` with the Jira credentials; returns {filename: path}.

    progress(phase, index, total, name, pct=None) per file: "download" (started / pct), then "downloaded",
    "present" (same size already on disk) or "failed"; a final "download" with total/total and no name.

    Like the Ez Collab-Jira extension: the attachment URL is fetched with an Authorization header, using
    multi-connection axel (see _axel_plan) and a plain HTTP stream when axel cannot run or fails.
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    base, headers = _jira_auth()
    axel = axel_path()
    log(f"axel: {axel} (≤{_connections()} connections)" if axel
        else "axel: không dùng được / not usable → HTTP")
    done, errors = {}, []
    left = max_bytes
    for n, a in enumerate(items):
        cancel()
        name = a["filename"]
        progress("download", n, len(items), name)
        target = folder / _safe_name(name)
        if target.exists() and a["size"] and target.stat().st_size == a["size"]:
            if left is not None and target.stat().st_size > left:
                errors.append(f"{name}: attachment exceeds remaining download budget")
                log(f"✗ {errors[-1]}")
                progress("failed", n, len(items), name)
                continue
            if left is not None:
                left = max(0, left - target.stat().st_size)
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
        # axel first. Under a byte cap it only starts once the server confirmed the real size fits what
        # is left; otherwise the bounded HTTP stream enforces the cap even when Jira's metadata is wrong.
        for how in ("axel", "http"):
            try:
                if how == "axel":
                    if not axel:
                        continue
                    plan, why = _axel_plan(axel, url, headers, a["size"], left)
                    if not plan:
                        if why != "small file":
                            log(f"  {name}: HTTP ({why})")
                        continue
                    started = time.monotonic()
                    _fetch_axel(axel, plan["url"], headers, target, cancel, report, plan["size"], plan["connections"])
                    how = f"axel ×{plan['connections']}, {time.monotonic() - started:.1f}s"
                else:
                    _fetch_http(url, headers, target, cancel, report, max_bytes=left)
                if left is not None:
                    left -= target.stat().st_size
                log(f"↓ {name} ({target.stat().st_size:,} B) [{how}]")
                done[name] = target
                progress("downloaded", n, len(items), name, 100)
                break
            except Exception as exc:
                cancel()  # re-raises when the user pressed Stop
                problems.append(f"{how}: {_redact(exc)}")
                if how == "axel":
                    log(f"⚠ {name}: axel → HTTP ({_redact(exc)})")
        else:
            errors.append(f"{name}: " + " | ".join(problems))
            log(f"✗ {errors[-1]}")
            progress("failed", n, len(items), name)
    if errors and not done:
        raise RuntimeError("; ".join(errors))
    progress("download", len(items), len(items), "")
    return done


# ---------------------------------------------------------------- extraction
def _volume_reader(archive, digits):
    import multivolumefile

    base = archive.with_suffix("")
    pattern = re.compile(re.escape(base.name) + r"\.\d{" + str(digits) + r"}", re.I)

    class NumericVolumes(multivolumefile.MultiVolume):
        def _glob_files(self, basename):
            # The library's broad basename.* glob also matches .001_extracted/ and .part files.
            return sorted((p for p in base.parent.iterdir() if p.is_file() and pattern.fullmatch(p.name)),
                          key=lambda p: int(p.suffix[1:]))

        def read(self, size=-1):
            # zipfile expects read(n) to cross volume boundaries; MultiVolume.read(n) does not.
            remaining = self._positions[-1] - self.tell()
            remaining = remaining if size < 0 else min(size, remaining)
            chunks = []
            while remaining > 0:
                chunk = super().read(remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)

    return NumericVolumes(base, mode="rb", ext_digits=digits)


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
        volume_stream = _volume_reader(archive, len(index))
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


def extraction_signature(archive):
    archive = Path(archive)
    names = {archive.name}
    if archive_volume(archive.name):
        names = expand_archive_volumes(names, [p.name for p in archive.parent.iterdir() if p.is_file()])
    return [[name, (archive.parent / name).stat().st_size, (archive.parent / name).stat().st_mtime_ns]
            for name in sorted(names)]


def extracted_folder(archive):
    archive = Path(archive)
    out = archive.with_name(archive.name + "_extracted")
    try:
        marker = json.loads((out / EXTRACT_MARKER).read_text(encoding="utf-8"))
        return out if marker == extraction_signature(archive) else None
    except (OSError, ValueError):
        return None


def extract(archive, log=print, cancel=lambda: None, dest=None):
    """Extract once into `dest` (default '<archive>_extracted/'); returns that folder (or None)."""
    archive = Path(archive)
    out = Path(dest) if dest else archive.with_name(archive.name + "_extracted")
    if out.is_symlink() or out.resolve() == archive.parent.resolve() or out.resolve() in archive.resolve().parents:
        raise ValueError("Unsafe extraction destination")
    signature = extraction_signature(archive)
    try:
        complete = json.loads((out / EXTRACT_MARKER).read_text(encoding="utf-8")) == signature
    except (OSError, ValueError):
        complete = False
    if complete:
        log(f"= {archive.name} (đã giải nén / already extracted)")
        return out
    # A nonempty directory is not proof of completion after cancellation/crash or a changed archive.
    if out.is_dir():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    low = archive.name.lower()
    volume_stream = None
    try:
        if re.search(r"\.zip\.\d{3,}$", low):
            width = len(archive.suffix) - 1
            volume_stream = _volume_reader(archive, width)
        if volume_stream is not None or low.endswith(".zip") or zipfile.is_zipfile(archive):
            with zipfile.ZipFile(volume_stream if volume_stream is not None else archive) as z:
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
        cancel()
        (out / EXTRACT_MARKER).write_text(json.dumps(signature), encoding="utf-8")
        log(f"⇲ {archive.name} → {out.name}")
        return out
    except BaseException as exc:
        shutil.rmtree(out, ignore_errors=True)
        if not isinstance(exc, Exception):
            raise
        cancel()  # re-raises when the user pressed Stop
        log(f"✗ {archive.name}: {exc}")
        return None
    finally:
        if volume_stream is not None:
            volume_stream.close()


def logs_in(path):
    p = Path(path)
    if p.is_file():
        return [p] if is_log_file(p) else []
    return sorted(f for f in p.rglob("*")
                  if f.is_file() and f.stat().st_size > 0 and not is_volume_continuation(f.name)
                  and (is_log_file(f) or (kind_of(f.name) == "archive" and not extracted_folder(f))))


def collect(issue, items, analyze_names, folder, log=print, cancel=lambda: None, progress=lambda *a: None,
            max_bytes=None):
    """Download `items`; for names in `analyze_names` extract and return the log files found."""
    files = download(issue, items, folder, log, cancel, progress, max_bytes=max_bytes)
    logs = []
    todo = [n for n in files if n in analyze_names and not is_volume_continuation(n)]
    for k, name in enumerate(todo):
        path = files[name]
        cancel()
        group = expand_archive_volumes([name], [a["filename"] for a in items])
        if not group.issubset(files) or not complete_volume_set(group):
            log(f"✗ {name}: incomplete multipart archive; extraction skipped")
            progress("failed", k, len(todo), name)
            continue
        progress("extract", k, len(todo), name)
        if kind_of(name) == "archive":
            out = extract(path, log, cancel)
            if out:
                logs += logs_in(out)
            else:
                progress("failed", k, len(todo), name)
                continue
        elif path.suffix.lower() == ".gz" and not path.name.lower().endswith(".tar.gz"):
            logs.append(path)  # LogSleuth reads .gz directly
        else:
            logs += logs_in(path)
        progress("extracted", k, len(todo), name)
    if todo:
        progress("extract", len(todo), len(todo), "")
    return [str(p) for p in logs], {k: str(v) for k, v in files.items()}
