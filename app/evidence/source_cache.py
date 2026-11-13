"""Fast fingerprints for source-derived caches.

The manifest tracks relevant source paths, sizes and nanosecond mtimes. It avoids
reading file contents just to decide whether the downstream scanners need to run.
"""
import hashlib
import os
from pathlib import Path

_SOURCE_EXTENSIONS = {
    ".java", ".kt", ".kts", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx",
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".py", ".pyi", ".rs", ".go", ".swift",
    ".dart", ".cs", ".rb", ".php", ".sh", ".bash", ".md", ".txt", ".rst",
}
_EXCLUDED_DIRS = {
    "node_modules", "build", "dist", "out", "__pycache__", "venv", "target",
}


def source_fingerprint(source_dir, cancel=None):
    """Return a stable SHA-256 digest of tracked source-file metadata."""
    root = Path(source_dir).resolve()
    digest = hashlib.sha256()
    for base, dirs, files in os.walk(root):
        if cancel:
            cancel()  # a huge tree on a network share takes a while to walk
        dirs[:] = sorted(
            (name for name in dirs if not name.startswith(".") and name not in _EXCLUDED_DIRS),
            key=str.casefold,
        )
        for name in sorted(files, key=str.casefold):
            if Path(name).suffix.lower() not in _SOURCE_EXTENSIONS:
                continue
            path = Path(base) / name
            try:
                stat = path.stat()
            except OSError:
                continue
            relative = path.relative_to(root).as_posix()
            record = f"{relative}\0{stat.st_size}\0{stat.st_mtime_ns}\n"
            digest.update(record.encode("utf-8", "surrogatepass"))
    return digest.hexdigest()
