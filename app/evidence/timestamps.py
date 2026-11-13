"""Versioned clock-window contract shared by ticket extraction and LogSleuth.

Times refer to the clock printed in the logs, not the server's timezone. Calendar
dates/UTC conversion must never be guessed from a ticket or a log filename.
"""
import json
import re


def normalize_clock(value):
    """Canonical HH:MM:SS[.mmm], or None for missing/invalid values."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})(?::(\d{2})(?:[.,](\d{1,3}))?)?\s*", value)
    if not match:
        return None
    hour, minute, second, fraction = match.groups()
    hour, minute, second = int(hour), int(minute), int(second or 0)
    if hour > 23 or minute > 59 or second > 59:
        return None
    clock = f"{hour:02d}:{minute:02d}:{second:02d}"
    return clock + "." + fraction.ljust(3, "0") if fraction else clock


def normalize_focus(data):
    """Invalid AI shape or either invalid endpoint disables the entire window."""
    data = data if isinstance(data, dict) else {}
    start, end = (normalize_clock(data.get(key)) for key in ("time_start", "time_end"))
    invalid = any(data.get(key) not in (None, "") and normalize_clock(data.get(key)) is None
                  for key in ("time_start", "time_end"))
    if invalid:
        start = end = None

    def strings(key, limit):
        values = data.get(key)
        return [v.strip()[:200] for v in values if isinstance(v, str) and v.strip()][:limit] if isinstance(values, list) else []

    confidence = data.get("confidence")
    return {
        "schema_version": 1, "time_basis": "log-local-clock",
        "time_start": start, "time_end": end,
        "filter_mode": "window" if start or end else "full",
        "confidence": confidence if confidence in ("high", "medium", "low", "none") else "none",
        "reason": str(data.get("reason") or ("Invalid timestamp; full scan." if invalid else "No usable timestamp; full scan."))[:1000],
        "keywords": strings("keywords", 15), "services": strings("services", 10),
    }


def parse_focus(answer):
    """Accept a JSON object or a fenced JSON response, never arbitrary prose times."""
    text = str(answer or "").strip()
    blocks = re.findall(r"```json\s*\n?([\s\S]*?)```", text, flags=re.I)
    try:
        return normalize_focus(json.loads(blocks[-1] if blocks else text))
    except (ValueError, TypeError):
        return normalize_focus({})