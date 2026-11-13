"""Normalises user-supplied logs into what LogSleuth reads: UTF-8 / UTF-16(BOM) text or binary DLT.

Handled here: archives (zip, tar*, 7z, rar - also nested), gzip / bzip2 / xz files, text in other
encodings (UTF-16 without BOM, Korean cp949, Windows-1252), and JSON documents holding an array of
records (converted to JSON Lines). Results are cached per source file under `workdir`.
"""
import bz2
import codecs
import gzip
import hashlib
import json
import lzma
import tarfile
from pathlib import Path

from . import attachments
from .i18n import t

MAX_DEPTH = 4
MAX_JSON_BYTES = 256 * 1024 ** 2
_CHUNK = 1 << 20
_COMPRESSED = {"gzip": gzip.open, "bz2": bz2.open, "xz": lzma.open}
_MAGIC = [(b"\x1f\x8b", "gzip"), (b"BZh", "bz2"), (b"\xfd7zXZ\x00", "xz"), (b"PK\x03\x04", "archive"),
          (b"PK\x05\x06", "archive"), (b"7z\xbc\xaf\x27\x1c", "archive"), (b"Rar!", "archive"),
          (b"\x28\xb5\x2f\xfd", "zstd")]
_CTRL = bytes(set(range(32)) - {9, 10, 12, 13, 27})
_SENSIBLE_KEYS = ("logs", "entries", "records", "events", "items", "messages", "lines", "data")


def _hangul_share(text):
    high = [c for c in text if ord(c) > 127]
    return sum(1 for c in high if "\uac00" <= c <= "\ud7a3") / len(high) if high else 0


def _text_encoding(head):
    """Codec of a text sample, or None when it looks like binary data."""
    if head.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    if b"\x00" in head:
        half = len(head) / 2
        even, odd = head[0::2].count(0), head[1::2].count(0)
        if odd > half * .3 and even < half * .05:
            return "utf-16-le"
        if even > half * .3 and odd < half * .05:
            return "utf-16-be"
        return None
    if len(head) - len(head.translate(None, _CTRL)) > len(head) * .02:
        return None
    try:
        codecs.getincrementaldecoder("utf-8")().decode(head)
        return "utf-8"
    except UnicodeDecodeError:
        pass
    try:  # cp949 happily decodes Western accents too, so insist on real Hangul
        if _hangul_share(codecs.getincrementaldecoder("cp949")().decode(head)) > .5:
            return "cp949"
    except UnicodeDecodeError:
        pass
    return "cp1252"


def sniff(path):
    """(kind, encoding): dlt | gzip | bz2 | xz | zstd | archive | text | empty | binary."""
    with open(path, "rb") as f:
        head = f.read(65536)
    if not head:
        return "empty", None
    if head[:4] == b"DLT\x01":
        return "dlt", None
    for magic, kind in _MAGIC:
        if head.startswith(magic):
            return kind, None
    if head[257:262] == b"ustar":
        return "archive", None
    enc = _text_encoding(head)
    return ("text", enc) if enc else ("binary", None)


def _key(path):
    st = path.stat()
    return hashlib.sha1(f"{path.resolve()}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:12]


def _walk(folder):
    for f in sorted(Path(folder).rglob("*")):
        if (f.is_file() and f.stat().st_size > 0 and not attachments.is_7z_volume_continuation(f.name)
                and (attachments.kind_of(f.name) == "archive" or attachments.is_log_file(f))):
            yield f


def _json_lines(text):
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if isinstance(data, dict):
        lists = {k: v for k, v in data.items() if isinstance(v, list) and v}
        data = next((lists[k] for k in _SENSIBLE_KEYS if k in lists), next(iter(lists.values()), None))
    if not isinstance(data, list) or not data:
        return None
    if all(isinstance(x, dict) for x in data):
        return [json.dumps(x, ensure_ascii=False) for x in data]
    if all(isinstance(x, str) for x in data):
        return data
    return None


def _maybe_json(path, enc):
    """JSON document (not JSON Lines) with an array of records -> list of lines, else None."""
    if path.stat().st_size > MAX_JSON_BYTES:
        return None
    with open(path, "r", encoding=enc, errors="replace") as f:
        head = f.read(65536).lstrip("\ufeff \t\r\n")
        if head[:1] not in ("[", "{"):
            return None
        if head[0] == "{":
            try:
                json.loads(head.split("\n", 1)[0])
                return None  # first line is a whole object: JSON Lines
            except ValueError:
                pass
        return _json_lines(head + f.read())


def _text(path, enc, work, log):
    lines = None
    try:
        lines = _maybe_json(path, enc)
    except OSError:
        pass
    if lines is not None:
        target = work / (path.stem + ".jsonl")
        if not target.exists():
            target.write_text("\n".join(lines) + "\n", encoding="utf-8")
            log(t("log.prep_json", name=path.name, n=len(lines)))
        return target
    if enc in ("utf-8", "utf-8-sig", "utf-16"):  # LogSleuth reads these as they are
        return path
    target = work / path.name
    if not target.exists():
        tmp = target.with_name(target.name + ".part")
        with open(path, "r", encoding=enc, errors="replace", newline="") as src, \
                open(tmp, "w", encoding="utf-8", newline="") as dst:
            while chunk := src.read(_CHUNK):
                dst.write(chunk)
        tmp.replace(target)
        log(t("log.prep_enc", name=path.name, enc=enc))
    return target


def _decompress(path, kind, work, cancel):
    low = path.name
    stem = low.rsplit(".", 1)[0] if "." in low else low + ".out"
    target = work / (stem or "out")
    if target.exists():
        return target
    tmp = target.with_name(target.name + ".part")
    size = 0
    try:
        with _COMPRESSED[kind](path, "rb") as src, open(tmp, "wb") as dst:
            while chunk := src.read(_CHUNK):
                cancel()
                size += len(chunk)
                if size > attachments.MAX_EXTRACT_BYTES:
                    raise RuntimeError("too large when unpacked")
                dst.write(chunk)
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


def _ready(path, workdir, depth, log, cancel):
    cancel()
    path = Path(path)
    if attachments.is_7z_volume_continuation(path.name):
        return []
    kind, enc = sniff(path)
    if kind == "empty":
        return []
    if kind == "zstd":
        log(t("log.prep_skip", name=path.name, why=t("log.why_zstd")))
        return []
    if kind in ("dlt", "binary"):
        return [path]  # LogSleuth decodes DLT and reports anything else it cannot read
    if depth >= MAX_DEPTH:
        log(t("log.prep_skip", name=path.name, why=t("log.why_depth")))
        return []
    work = Path(workdir) / _key(path)
    work.mkdir(parents=True, exist_ok=True)
    try:
        if kind == "archive" or (kind in _COMPRESSED and tarfile.is_tarfile(path)):
            out = attachments.extract(path, log, cancel, dest=work / "x")
            return [p for f in (_walk(out) if out else []) for p in _ready(f, workdir, depth + 1, log, cancel)]
        if kind in _COMPRESSED:
            return _ready(_decompress(path, kind, work, cancel), workdir, depth + 1, log, cancel)
        return [_text(path, enc, work, log)]
    except OSError as exc:
        log(t("log.prep_skip", name=path.name, why=str(exc)))
        return []


def prepare(files, workdir, log=print, cancel=lambda: None):
    """Readable log files for LogSleuth, in input order, without duplicates."""
    out, seen = [], set()
    for f in files:
        for p in _ready(f, workdir, 0, log, cancel):
            if str(p) not in seen:
                seen.add(str(p))
                out.append(str(p))
    return out
