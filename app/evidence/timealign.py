"""Which clock does the log print, relative to the time written in the ticket?

A ticket says "failed at 14:30" in the reporter's local time; the attached log may print UTC,
device-local time, or the very same clock. LogSleuth filters by time of day, so a wrong guess
silently drops the incident. This module turns the ticket window into a log-clock window:

  1. a shift set by the user always wins;
  2. otherwise a short list of explainable candidates is tested against the time range each
     log file really covers (same clock; log in UTC while the ticket states a zone; log in UTC
     while the ticket uses the Jira server's zone);
  3. when exactly one candidate lands inside the logs it is used; when none or several do, the
     AI may break the tie from the evidence table - and its answer is rejected unless the
     shifted window is inside a log file too;
  4. with nothing to go on the window is left as written (the pipeline then falls back to the
     full log range if it turns out to be empty).
"""
import re

from .timestamps import MAX_SHIFT_MINUTES, format_offset, normalize_clock, parse_offset

DAY_MS = 24 * 3600 * 1000


def clock_ms(clock):
    clock = normalize_clock(clock) if isinstance(clock, str) else None
    if not clock:
        return None
    hms, _, frac = clock.partition(".")
    h, m, s = (int(x) for x in hms.split(":"))
    return ((h * 60 + m) * 60 + s) * 1000 + int((frac or "0").ljust(3, "0")[:3])


def shift_clock(clock, minutes):
    """Clock moved by `minutes`, wrapping around midnight; None stays None."""
    ms = clock_ms(clock)
    if ms is None:
        return None
    ms = (ms + int(minutes) * 60000) % DAY_MS
    s, frac = divmod(ms, 1000)
    text = f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}"
    return f"{text}.{frac:03d}" if "." in str(clock) else text


def _segments(a, b):
    return [(a, b)] if a <= b else [(a, DAY_MS), (0, b)]


def window_covered(spans, start, end):
    """True/False when the window does/does not touch any log file's time-of-day range; None = unknown."""
    a, b = clock_ms(start), clock_ms(end)
    if a is None and b is None:
        return None
    a = b if a is None else a
    b = a if b is None else b
    known = False
    for span in spans or []:
        first, last = clock_ms(span.get("first")), clock_ms(span.get("last"))
        if first is None or last is None:
            continue
        known = True
        if span.get("wraps"):  # the file crosses midnight: a time-of-day range cannot exclude anything
            return True
        for x1, x2 in _segments(a, b):
            for y1, y2 in _segments(first, last):
                if x1 <= y2 and y1 <= x2:
                    return True
    return False if known else None


def jira_utc_offset(jira_json_text):
    """UTC offset (minutes) carried by the issue's own timestamps, e.g. '...T14:50:11.000+0900'."""
    match = re.search(r'"(?:created|updated)"\s*:\s*"\d{4}-\d{2}-\d{2}T[\d:.]+\s*(Z|[+-]\d{2}:?\d{2})"', jira_json_text or "")
    return parse_offset(match.group(1)) if match else None


def candidates(focus, jira_offset=None):
    """Explainable (shift, source) pairs, most plausible first. shift = log clock - ticket clock, in minutes."""
    out = [{"shift_minutes": 0, "source": "same-clock"}]
    ticket = parse_offset(focus.get("ticket_utc_offset"))
    if ticket:
        out.append({"shift_minutes": -ticket, "source": "log-utc"})
    elif ticket is None and jira_offset:
        out.append({"shift_minutes": -jira_offset, "source": "log-utc-jira-zone"})
    elif ticket == 0 and jira_offset:
        out.append({"shift_minutes": jira_offset, "source": "log-local-jira-zone"})
    return out


def _result(focus, shift, source, reason, options, covered):
    return {
        "shift_minutes": shift, "source": source, "reason": reason, "covered": covered,
        "ticket_start": focus.get("time_start"), "ticket_end": focus.get("time_end"),
        "time_start": shift_clock(focus.get("time_start"), shift), "time_end": shift_clock(focus.get("time_end"), shift),
        "candidates": options,
    }


def align(focus, spans, jira_offset=None, manual_shift=None, ask_ai=None):
    """Log-clock window for the ticket window in `focus`; see the module docstring for the rules."""
    start, end = focus.get("time_start"), focus.get("time_end")
    if not (start or end):
        return _result(focus, 0, "none", "No incident time: the full log range is used.", [], None)
    options = candidates(focus, jira_offset)
    for option in options:
        option["time_start"] = shift_clock(start, option["shift_minutes"])
        option["time_end"] = shift_clock(end, option["shift_minutes"])
        option["covered"] = window_covered(spans, option["time_start"], option["time_end"])
    if manual_shift is not None:
        covered = window_covered(spans, shift_clock(start, manual_shift), shift_clock(end, manual_shift))
        return _result(focus, int(manual_shift), "manual", "Shift set by the user.", options, covered)
    if all(option["covered"] is None for option in options):
        return _result(focus, 0, "same-clock", "Log time range unknown; the ticket clock is used as written.", options, None)
    inside = [option for option in options if option["covered"]]
    if len(inside) == 1:
        pick = inside[0]
        return _result(focus, pick["shift_minutes"], pick["source"],
                       "Only this alignment falls inside the time range of the log files.", options, True)
    if ask_ai:
        try:
            answer = ask_ai(options) or {}
        except Exception as exc:  # the tie-break is optional: never fail the log step over it
            answer = {"error": str(exc)}
        shift = answer.get("shift_minutes")
        if isinstance(shift, (int, float)) and not isinstance(shift, bool) and abs(shift) <= MAX_SHIFT_MINUTES:
            shift = int(shift)
            covered = window_covered(spans, shift_clock(start, shift), shift_clock(end, shift))
            if covered:
                return _result(focus, shift, "ai", str(answer.get("reason") or "Chosen by the AI from the log time ranges.")[:500],
                               options, True)
    if inside:  # several fit and nobody could break the tie: keep the ticket clock as written
        pick = inside[0]
        return _result(focus, pick["shift_minutes"], pick["source"],
                       "Several alignments fit the logs; the most direct one is used.", options, True)
    return _result(focus, 0, "same-clock",
                   "The incident window is outside every log file's time range in all tested alignments.", options, False)


def report(focus, spans, attachments, jira_offset, options):
    """Markdown evidence table: shown to the user and given to the AI for the tie-break."""
    zone = focus.get("ticket_utc_offset") or "not stated"
    lines = ["# Log time range and clock alignment", "",
             f"- Incident window as written in the ticket: `{focus.get('time_start') or '…'}` → `{focus.get('time_end') or '…'}`"
             f" (date: {focus.get('incident_date') or 'not stated'}; zone stated in the ticket: {zone})",
             f"- UTC offset of the Jira server's own timestamps: {format_offset(jira_offset) or 'unknown'}", "",
             "## Log files", "", "| file | format | records | first timestamp | last timestamp | crosses midnight |",
             "|---|---|---:|---|---|---|"]
    for span in spans or []:
        lines.append(f"| {span.get('name', '?')} | {span.get('format', '?')} | {span.get('lines', 0)} | "
                     f"{span.get('firstTs') or span.get('first') or '—'} | {span.get('lastTs') or span.get('last') or '—'} | "
                     f"{'yes' if span.get('wraps') else 'no'} |")
    uploads = [a for a in attachments or [] if a.get("created")]
    if uploads:
        lines += ["", "## Attachment upload times (Jira clock)", "", "| attachment | uploaded |", "|---|---|"]
        lines += [f"| {a['filename']} | {a['created']} |" for a in uploads[:40]]
    if options:
        lines += ["", "## Candidate alignments", "",
                  "| shift (log − ticket, minutes) | meaning | window in log clock | inside a log file's range |", "|---:|---|---|---|"]
        meaning = {"same-clock": "log prints the same clock as the ticket",
                   "log-utc": "log prints UTC; ticket uses the zone it states",
                   "log-utc-jira-zone": "log prints UTC; ticket uses the Jira server's zone",
                   "log-local-jira-zone": "ticket is UTC; log prints the Jira server's zone"}
        for option in options:
            inside = {True: "yes", False: "no", None: "unknown"}[option.get("covered")]
            lines.append(f"| {option['shift_minutes']} | {meaning.get(option['source'], option['source'])} | "
                         f"{option.get('time_start') or '…'} → {option.get('time_end') or '…'} | {inside} |")
    return "\n".join(lines) + "\n"
