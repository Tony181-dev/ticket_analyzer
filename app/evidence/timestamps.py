"""Versioned incident-time contract shared by ticket extraction, the evidence gate and LogSleuth.

`time_start` / `time_end` are the clock *as written in the ticket* (or typed by the user). Which
clock the log prints is decided later, when the log files are known (app/evidence/timealign.py):
the window is shifted by a whole UTC-offset difference only when the evidence supports it, or
by the value the user set by hand. Calendar dates are never used to filter.
"""
import json
import re

SCHEMA_VERSION = 2
MAX_EXTRA_WINDOWS = 2  # time ranges typed by hand at step 2, on top of the incident window
# Abbreviations people actually write in tickets. Ambiguous ones (CST, IST...) are left out on purpose.
_ZONES = {"UTC": 0, "GMT": 0, "Z": 0, "KST": 540, "JST": 540, "ICT": 420, "WIB": 420, "SGT": 480, "HKT": 480,
          "AWST": 480, "CET": 60, "CEST": 120, "EET": 120, "EEST": 180, "BST": 60, "WET": 0, "WEST": 60,
          "MSK": 180, "EST": -300, "EDT": -240, "PST": -480, "PDT": -420, "MST": -420, "MDT": -360,
          "AEST": 600, "AEDT": 660, "NZST": 720, "NZDT": 780}
MAX_SHIFT_MINUTES = 14 * 60


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


def parse_offset(value):
    """UTC offset in minutes from '+09:00', 'UTC+9', '+0900', 'KST', 9 ... or None when unknown."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        minutes = int(round(value * 60)) if abs(value) <= 14 else int(value)
        return minutes if abs(minutes) <= MAX_SHIFT_MINUTES else None
    text = str(value).strip().upper()
    if not text:
        return None
    if text in _ZONES:
        return _ZONES[text]
    match = re.fullmatch(r"(?:UTC|GMT)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?", text)
    if not match:
        return None
    sign, hours, minutes = match.groups()
    total = int(hours) * 60 + int(minutes or 0)
    if int(minutes or 0) > 59 or total > MAX_SHIFT_MINUTES:
        return None
    return -total if sign == "-" else total


def format_offset(minutes):
    """'+09:00' for 540; '' for None."""
    if minutes is None:
        return ""
    sign = "-" if minutes < 0 else "+"
    return f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def normalize_shift(value):
    """A hand-set log shift in minutes (None = decide automatically)."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        minutes = int(round(float(value)))
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Log time shift must be a number of minutes")
    if abs(minutes) > MAX_SHIFT_MINUTES:
        raise ValueError("Log time shift must be within ±14 hours")
    return minutes


def _attachment_picks(value):
    """[{filename, reason}] from the AI's list of strings/objects; None when the AI gave no list at all."""
    if not isinstance(value, list):
        return None
    out, seen = [], set()
    for item in value[:60]:
        if isinstance(item, str):
            name, reason = item, ""
        elif isinstance(item, dict):
            name = item.get("filename") or item.get("name") or ""
            reason = item.get("reason") or ""
        else:
            continue
        name = str(name).strip()[:300]
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            out.append({"filename": name, "reason": str(reason).strip()[:300]})
    return out


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
    date = data.get("incident_date")
    date = date.strip() if isinstance(date, str) and re.fullmatch(r"\s*\d{4}-\d{2}-\d{2}\s*", date) else None
    picks = _attachment_picks(data.get("attachments"))
    extra = normalize_windows(data.get("extra_windows"))
    return {
        "schema_version": SCHEMA_VERSION, "time_basis": "ticket-clock",
        "time_start": start, "time_end": end,
        # Same clock (and time zone) as time_start/time_end; a log line in ANY window is kept.
        "extra_windows": extra,
        "filter_mode": "window" if start or end or extra else "full",
        "ticket_utc_offset": format_offset(parse_offset(data.get("ticket_utc_offset"))) or None,
        "incident_date": date,
        "confidence": confidence if confidence in ("high", "medium", "low", "none") else "none",
        "reason": str(data.get("reason") or ("Invalid timestamp; full scan." if invalid else "No usable timestamp; full scan."))[:1000],
        "keywords": strings("keywords", 15), "services": strings("services", 10),
        # None = the AI did not answer the attachment question (fall back to a heuristic);
        # [] = it looked and found nothing worth downloading.
        "attachments": picks,
    }


def normalize_windows(value):
    """Up to MAX_EXTRA_WINDOWS extra ranges [{time_start, time_end}]; empty or invalid rows are dropped."""
    out = []
    for row in value if isinstance(value, list) else []:
        if not isinstance(row, dict):
            continue
        raw = [row.get(key) for key in ("time_start", "time_end")]
        clocks = [normalize_clock(v) for v in raw]
        if any(v not in (None, "") and c is None for v, c in zip(raw, clocks)) or not any(clocks):
            continue
        out.append({"time_start": clocks[0], "time_end": clocks[1]})
    return out[:MAX_EXTRA_WINDOWS]


def parse_focus(answer):
    """Accept a JSON object or a fenced JSON response, never arbitrary prose times."""
    text = str(answer or "").strip()
    blocks = re.findall(r"```json\s*\n?([\s\S]*?)```", text, flags=re.I)
    try:
        return normalize_focus(json.loads(blocks[-1] if blocks else text))
    except (ValueError, TypeError):
        return normalize_focus({})
