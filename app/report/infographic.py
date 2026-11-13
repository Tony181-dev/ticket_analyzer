"""Build a safe, self-contained infographic from Copilot CLI's text response."""
import html
import json
import re
import textwrap
from xml.etree import ElementTree

from ..i18n import t

MAX_RESPONSE_CHARS = 20000
MAX_ITEMS = 4


def _clean_text(value, name, limit, required=False):
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"Infographic field '{name}' must be text")
    value = " ".join(value.replace("\x00", " ").split())
    if required and not value:
        raise ValueError(f"Infographic field '{name}' is required")
    if len(value) > limit:
        clipped = value[:limit - 1].rsplit(" ", 1)[0].rstrip()
        value = (clipped or value[:limit - 1]) + "…"
    return value


def _list(value, name, limit=MAX_ITEMS):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"Infographic field '{name}' must be a list")
    result = []
    for item in value[:limit]:
        if isinstance(item, str):
            text = _clean_text(item, name, 240)
            if text:
                result.append({"time": "", "event": text} if name == "timeline" else text)
        elif name == "timeline" and isinstance(item, dict):
            event = _clean_text(item.get("event"), "timeline.event", 180)
            when = _clean_text(item.get("time"), "timeline.time", 48)
            if event:
                result.append({"time": when, "event": event})
    return result


def parse_ai_summary(response):
    """Parse and bound the JSON contract returned by Copilot CLI."""
    text = str(response or "").strip().lstrip("\ufeff")
    if len(text) > MAX_RESPONSE_CHARS:
        raise ValueError("Copilot CLI infographic response is too large")
    fenced = re.search(r"(?is)```(?:json)?\s*(.*?)```", text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        data = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ValueError("Copilot CLI must return a JSON object for the infographic") from exc
    if not isinstance(data, dict):
        raise ValueError("Copilot CLI infographic response must be a JSON object")
    summary = {
        "title": _clean_text(data.get("title"), "title", 100, required=True),
        "summary": _clean_text(data.get("summary"), "summary", 360, required=True),
        "root_cause": _clean_text(data.get("root_cause"), "root_cause", 360),
        "timeline": _list(data.get("timeline"), "timeline"),
        "evidence": _list(data.get("evidence"), "evidence"),
        "actions": _list(data.get("actions"), "actions"),
    }
    return summary


def _wrap(value, width, max_lines):
    value = str(value or "").strip()
    lines = textwrap.wrap(value, width=width, break_long_words=True, break_on_hyphens=False) if value else []
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1].rstrip(" .…")
        lines[-1] = (last[:max(1, width - 1)].rstrip() + "…")
    return lines


def _text(x, y, lines, size=22, fill="#E7ECF7", weight=400, line_height=1.28):
    if not lines:
        return ""
    spans = "".join(
        f'<tspan x="{x}" dy="{0 if index == 0 else line_height}em">{html.escape(line)}</tspan>'
        for index, line in enumerate(lines)
    )
    return (f'<text x="{x}" y="{y}" fill="{fill}" font-size="{size}" '
            f'font-weight="{weight}" font-family="Segoe UI, Arial, sans-serif">{spans}</text>')


def _card(x, y, width, height, label, accent):
    return (f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="22" '
            'fill="#151D31" stroke="#2A3550" stroke-width="2"/>'
            f'<rect x="{x + 24}" y="{y + 25}" width="7" height="27" rx="3.5" fill="{accent}"/>'
            f'<text x="{x + 47}" y="{y + 47}" fill="#AAB7D0" font-size="18" '
            f'font-weight="700" letter-spacing="1.4" font-family="Segoe UI, Arial, sans-serif">'
            f'{html.escape(label)}</text>')


def _bullets(items, x, y, width, color, empty_label):
    if not items:
        return _text(x, y, _wrap(empty_label, width, 2), size=20, fill="#8794AC")
    out = []
    for index, item in enumerate(items[:MAX_ITEMS]):
        top = y + index * 94
        out.append(f'<circle cx="{x + 5}" cy="{top - 7}" r="5" fill="{color}"/>')
        out.append(_text(x + 23, top, _wrap(item, width - 4, 3), size=20, line_height=1.25))
    return "".join(out)


def render_svg(data, issue):
    """Render bounded plain text into a self-contained SVG (no AI-supplied markup)."""
    title = data["title"]
    issue_text = _clean_text(str(issue or ""), "issue", 48)
    title_lines = _wrap(title, 54, 2)
    summary_lines = _wrap(data["summary"], 66, 4)
    cause = data["root_cause"] or t("infographic.not_stated")
    cause_lines = _wrap(cause, 68, 4)
    timeline = data["timeline"]
    evidence = data["evidence"]
    actions = data["actions"]
    empty = t("infographic.no_items")

    chunks = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="1000" viewBox="0 0 1600 1000" role="img">',
        f'<title>{html.escape(title)}</title>',
        '<defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="#0B1222"/><stop offset="1" stop-color="#111B30"/>'
        '</linearGradient><linearGradient id="accent" x1="0" y1="0" x2="1" y2="0">'
        '<stop offset="0" stop-color="#70D7C0"/><stop offset="1" stop-color="#7FA7FF"/>'
        '</linearGradient></defs>',
        '<rect width="1600" height="1000" fill="url(#bg)"/>',
        '<circle cx="1470" cy="65" r="190" fill="#344B7B" opacity=".12"/>',
        '<circle cx="85" cy="910" r="210" fill="#36BFA1" opacity=".07"/>',
        '<rect x="60" y="48" width="9" height="30" rx="4.5" fill="url(#accent)"/>',
        f'<text x="88" y="70" fill="#8FA3C7" font-size="17" font-weight="700" letter-spacing="2" '
        f'font-family="Segoe UI, Arial, sans-serif">{html.escape(t("infographic.brand"))}</text>',
        _text(60, 128, title_lines, size=40, weight=700, fill="#F4F7FF", line_height=1.12),
        f'<text x="62" y="196" fill="#91A0BA" font-size="19" font-family="Segoe UI, Arial, sans-serif">'
        f'{html.escape(issue_text)}</text>',
        _card(60, 225, 710, 190, t("infographic.summary"), "#6AD3BB"),
        _text(88, 314, summary_lines, size=21, line_height=1.3),
        _card(790, 225, 750, 190, t("infographic.root_cause"), "#89A8FF"),
        _text(818, 314, cause_lines, size=21, line_height=1.3),
        _card(60, 440, 470, 500, t("infographic.timeline"), "#FFC66D"),
        _card(550, 440, 500, 500, t("infographic.evidence"), "#78A8FF"),
        _card(1070, 440, 470, 500, t("infographic.actions"), "#73D5A8"),
    ]

    if timeline:
        for index, item in enumerate(timeline):
            top = 535 + index * 94
            when = item["time"] or f"{index + 1:02d}"
            chunks.append(f'<circle cx="98" cy="{top - 6}" r="8" fill="#FFC66D"/>')
            chunks.append(_text(120, top, _wrap(when, 30, 1), size=18, fill="#FFC66D", weight=700))
            chunks.append(_text(120, top + 29, _wrap(item["event"], 34, 2), size=19, line_height=1.2))
    else:
        chunks.append(_text(92, 535, _wrap(empty, 40, 2), size=20, fill="#8794AC"))

    chunks.extend([
        _bullets(evidence, 580, 535, 430, "#78A8FF", empty),
        _bullets(actions, 1100, 535, 410, "#73D5A8", empty),
        f'<text x="1540" y="976" text-anchor="end" fill="#66748D" font-size="14" '
        f'font-family="Segoe UI, Arial, sans-serif">Ticket Analyzer · {html.escape(issue_text)}</text>',
        '</svg>',
    ])
    svg = "\n".join(chunks)
    ElementTree.fromstring(svg)  # Fail before writing/uploading if the generated XML is invalid.
    return svg