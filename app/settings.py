"""User settings (data/config/settings.json). Only non-secret values live here;
MCP tokens live in data/config/mcp.json."""
import json
import threading

from . import paths

DEFAULTS = {
    "lang": "vi",                      # "vi" | "en" (UI + server messages)
    "ai_mode": "m365",                 # "m365" | "copilot"
    "m365_model": "GPT-5.6 Sol Think Deeper",
    "m365_headless": True,
    "copilot_model": "gpt-6-luna",     # "" = Copilot CLI's own default (auto)
    "download_dir": "",                # "" = ~/Downloads/TicketAnalyzer/<ISSUE>
    "axel_command": "",                 # "" = axel bundled with the app, then axel on PATH
    "axel_connections": 30,            # parallel connections per attachment
    "refine_model": "",                # small model for Step "format" refine; "" = CLI default
    "collab_space_key": "~",
    "collab_parent_id": "",
    "jira_browse_url": "http://jira.lge.com/issue/browse",
    "review_before_publish": True,
    "log_budget_tokens": 50000,
    "graph_token_budget": 150000,
    "port": 6969,
}
VERSION = 3
M365_MODELS = ["GPT-5.6 Sol Think Deeper", "GPT-5.6 Sol Think", "GPT 6 Luna", "Claude Sonnet", "Claude Opus"]
COPILOT_MODELS = ["gpt-6-luna", "auto", "gpt-5", "gpt-5-mini", "claude-sonnet-4.5", "claude-opus-4.1"]
_lock = threading.Lock()


def _read():
    """File contents merged over defaults, migrated to VERSION."""
    data = dict(DEFAULTS)
    try:
        stored = json.loads(paths.SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = {}
    data.update(stored)
    if stored.get("settings_version", 1) < 3:  # older files stored the previous defaults, not a user choice
        if stored.get("m365_model") in ("GPT 6 Luna", "GPT-5.6 Sol Think Deeper"):
            data["m365_model"] = DEFAULTS["m365_model"]
        if not stored.get("copilot_model"):
            data["copilot_model"] = DEFAULTS["copilot_model"]
    data.pop("settings_version", None)
    try:
        data["log_budget_tokens"] = max(2000, min(50000, int(data["log_budget_tokens"])))
    except (TypeError, ValueError, OverflowError):
        data["log_budget_tokens"] = DEFAULTS["log_budget_tokens"]
    return data


def load():
    with _lock:
        return _read()


def save(patch):
    with _lock:
        current = _read()
        for key, value in (patch or {}).items():
            if key in DEFAULTS:
                if key == "log_budget_tokens":
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 2000 <= value <= 50000 or int(value) != value:
                        raise ValueError("Log token cap must be an integer between 2000 and 50000")
                    value = int(value)
                current[key] = value
        paths.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = paths.SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({**current, "settings_version": VERSION}, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(paths.SETTINGS_FILE)
        return current
