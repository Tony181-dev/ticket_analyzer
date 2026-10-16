"""Filesystem layout. Everything the app writes lives under data/ (git-ignored)."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
WEB = ROOT / "web"
LOGSLEUTH_CLI = ROOT / "logsleuth" / "cli.js"
INSTRUCTIONS = ROOT / "instructions"
M365_WORKER = APP / "m365_worker" / "worker.py"

DATA = Path(os.getenv("TA_DATA_DIR", ROOT / "data"))
CONFIG = DATA / "config"
SETTINGS_FILE = CONFIG / "settings.json"
MCP_FILE = CONFIG / "mcp.json"
RUNS = DATA / "runs"
CATALOGS = DATA / "catalogs"
GRAPHS = DATA / "graphs"
M365_SESSIONS = DATA / "m365-sessions"

PYTHON = sys.executable
SCRIPTS = Path(sys.executable).parent  # venv Scripts/ (Windows) or bin/


def ensure_dirs():
    for d in (CONFIG, RUNS, CATALOGS, GRAPHS, M365_SESSIONS):
        d.mkdir(parents=True, exist_ok=True)
