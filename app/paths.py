"""Filesystem layout. Everything the app writes lives under data/ (git-ignored).

Packaged (PyInstaller) builds read bundled resources from sys._MEIPASS and keep
data/ next to TicketAnalyzer.exe (or in %LOCALAPPDATA% when that is read-only).
"""
import os
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
APP = ROOT / "app"
WEB = ROOT / "web"
LOGSLEUTH_CLI = ROOT / "logsleuth" / "cli.js"
INSTRUCTIONS = ROOT / "instructions"
M365_WORKER = APP / "m365_worker" / "worker.py"
BUNDLED_BIN = ROOT / "bin"  # uv/uvx shipped inside the EXE build

if FROZEN and os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "0") == "0":
    # PyInstaller's Playwright hook points browsers *inside* the bundle; use the normal
    # per-user cache instead so an already-downloaded Chromium is reused, not fetched again.
    if sys.platform == "win32":
        _pw = Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "ms-playwright"
    elif sys.platform == "darwin":
        _pw = Path.home() / "Library" / "Caches" / "ms-playwright"
    else:
        _pw = Path.home() / ".cache" / "ms-playwright"
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_pw)


def _default_data():
    if not FROZEN:
        return ROOT / "data"
    beside = Path(sys.executable).resolve().parent / "data"
    try:
        beside.mkdir(parents=True, exist_ok=True)
        probe = beside / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        return beside
    except OSError:
        return Path(os.getenv("LOCALAPPDATA", Path.home())) / "TicketAnalyzer" / "data"


DATA = Path(os.getenv("TA_DATA_DIR") or _default_data())
CONFIG = DATA / "config"
SETTINGS_FILE = CONFIG / "settings.json"
MCP_FILE = CONFIG / "mcp.json"
RUNS = DATA / "runs"
CATALOGS = DATA / "catalogs"
GRAPHS = DATA / "graphs"
M365_SESSIONS = DATA / "m365-sessions"

PYTHON = sys.executable
SCRIPTS = BUNDLED_BIN if FROZEN else Path(sys.executable).parent  # where uvx lives


def ensure_dirs():
    for d in (CONFIG, RUNS, CATALOGS, GRAPHS, M365_SESSIONS):
        d.mkdir(parents=True, exist_ok=True)
