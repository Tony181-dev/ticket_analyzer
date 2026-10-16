#!/usr/bin/env sh
# Ticket Analyzer launcher (macOS / Linux). Windows: start.bat
set -e
cd "$(dirname "$0")"
export PIP_DISABLE_PIP_VERSION_CHECK=1
if [ ! -x .venv/bin/python ]; then
  PY=""
  for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then PY="$c"; break; fi
  done
  [ -n "$PY" ] || { echo "Python 3.10+ is required: https://www.python.org/downloads/"; exit 1; }
  echo "[Ticket Analyzer] creating .venv ..."
  "$PY" -m venv --system-site-packages .venv  # reuse what the base Python already has
fi
.venv/bin/python -c "import playwright, uv" 2>/dev/null || .venv/bin/python -m pip install -q -r requirements.txt || echo "[!] pip install failed - use Setup in the UI to retry."
exec .venv/bin/python -m app
