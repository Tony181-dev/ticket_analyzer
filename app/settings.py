"""User settings (data/config/settings.json). Only non-secret values live here;
MCP tokens live in data/config/mcp.json."""
import json
import threading

from . import paths

DEFAULTS = {
    "ai_mode": "m365",                 # "m365" | "copilot"
    "m365_model": "GPT 5.6 Sol Think deeper",
    "m365_headless": True,
    "copilot_model": "",               # "" = Copilot CLI default
    "refine_model": "",                # small model for Step "format" refine; "" = CLI default
    "collab_space_key": "~",
    "collab_parent_id": "",
    "jira_browse_url": "http://jira.lge.com/issue/browse",
    "review_before_publish": True,
    "log_budget_tokens": 50000,
    "graph_token_budget": 150000,
    "port": 6969,
}

_lock = threading.Lock()


def load():
    with _lock:
        data = dict(DEFAULTS)
        try:
            data.update(json.loads(paths.SETTINGS_FILE.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        return data


def save(patch):
    with _lock:
        current = dict(DEFAULTS)
        try:
            current.update(json.loads(paths.SETTINGS_FILE.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        for key, value in (patch or {}).items():
            if key in DEFAULTS:
                current[key] = value
        paths.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = paths.SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(paths.SETTINGS_FILE)
        return current
