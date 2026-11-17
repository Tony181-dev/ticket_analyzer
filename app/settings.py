"""User settings (data/config/settings.json). Only non-secret values live here;
MCP tokens live in data/config/mcp.json."""
import json
import threading

from . import paths

DEFAULTS = {
    "lang": "vi",                      # "vi" | "en" | "ko" (UI, server messages and report language)
    "ai_mode": "m365",                 # "m365" | "copilot"
    "m365_model": "GPT-5.6 Sol Think Deeper",
    "m365_headless": True,
    "copilot_model": "gpt-6-luna",     # "" = Copilot CLI's own default (auto)
    "download_dir": "",                # "" = ~/Downloads/TicketAnalyzer/<ISSUE>
    "axel_command": "",                 # "" = axel bundled with the app, then axel on PATH
    "axel_connections": 8,             # parallel connections per attachment (fewer for small files)
    "refine_model": "",                # small model for Step "format" refine; "" = CLI default
    "collab_space_key": "~",
    "collab_parent_id": "",
    "jira_browse_url": "http://jira.lge.com/issue/browse",
    "review_before_publish": True,
    "log_budget_tokens": 50000,
    "graph_token_budget": 150000,
    "port": 6969,
    # Defaults for a new ticket (each run keeps its own copy and can override them).
    "default_gate": "skip",            # "skip" | "review" | "manual": how evidence decisions are made
    "default_use_attachments": True,   # use the ticket's images and log/archive attachments
    "default_use_codegraph": True,
    "default_use_infographic": True,
    "auto_download_max_mb": 2000,      # Auto downloads / Review proposal: total evidence bytes; 0 = no limit
    "ai_retries": 1,                   # extra attempts after a transient AI failure
    "assistant_proactive": True,       # chat bubble: auto-scan when the evidence gate opens or a step fails
}
VERSION = 5
M365_MODELS = ["GPT-5.6 Sol Think Deeper", "GPT-5.6 Sol Think", "GPT 6 Luna", "Claude Sonnet", "Claude Opus"]
# Copilot CLI `--model` ids (CLI command reference, "Supported models"). The app default stays gpt-6-luna;
# the CLI's own default is claude-sonnet-5.5, which "auto" may pick. Any other id can be typed in the UI.
COPILOT_MODELS = ["gpt-6-luna", "auto", "claude-sonnet-5.5", "claude-opus-5.5", "claude-haiku-5.5", "claude-haiku-4.5",
                  "gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-astra", "gpt-5.3-codex", "gemini-3.8-flash"]
GATES = ("skip", "review", "manual")
_CHOICES = {"lang": ("vi", "en", "ko"), "ai_mode": ("m365", "copilot"), "default_gate": GATES}
_RANGES = {"log_budget_tokens": (2000, 50000), "graph_token_budget": (5000, 400000), "axel_connections": (1, 64),
           "port": (1024, 65535), "auto_download_max_mb": (0, 1000000), "ai_retries": (0, 3)}
_LABELS = {"log_budget_tokens": "Log token cap"}
_lock = threading.Lock()


def _valid(key, value):
    """The value in its stored type; ValueError when it cannot be a legal setting."""
    default = DEFAULTS[key]
    if key in _CHOICES:
        if value not in _CHOICES[key]:
            raise ValueError(f"{key} must be one of: {', '.join(_CHOICES[key])}")
        return value
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be true or false")
        return value
    if isinstance(default, int):
        lo, hi = _RANGES[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi or int(value) != value:
            raise ValueError(f"{_LABELS.get(key, key)} must be an integer between {lo} and {hi}")
        return int(value)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    return value.strip()


def _read():
    """File contents merged over defaults, migrated to VERSION; a hand-edited bad value falls back to its default."""
    data = dict(DEFAULTS)
    try:
        stored = json.loads(paths.SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = {}
    if not isinstance(stored, dict):
        stored = {}
    if stored.get("settings_version", 1) < 3:  # older files stored the previous defaults, not a user choice
        if stored.get("m365_model") in ("GPT 6 Luna", "GPT-5.6 Sol Think Deeper"):
            stored["m365_model"] = DEFAULTS["m365_model"]
        if not stored.get("copilot_model"):
            stored["copilot_model"] = DEFAULTS["copilot_model"]
    if stored.get("settings_version", 1) < 5 and stored.get("axel_connections") == 30:
        stored["axel_connections"] = DEFAULTS["axel_connections"]  # the old default, not a user choice
    for key, value in stored.items():
        if key not in DEFAULTS:
            continue
        try:
            data[key] = _valid(key, value)
        except ValueError:
            if key in _RANGES and isinstance(value, (int, float)) and not isinstance(value, bool):
                lo, hi = _RANGES[key]  # out of range: clamp rather than silently jump back to the default
                data[key] = int(max(lo, min(hi, value)))
    return data


def load():
    with _lock:
        return _read()


def save(patch):
    with _lock:
        current = _read()
        for key, value in (patch or {}).items():
            if key in DEFAULTS:
                current[key] = _valid(key, value)
        paths.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = paths.SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({**current, "settings_version": VERSION}, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(paths.SETTINGS_FILE)
        return current
