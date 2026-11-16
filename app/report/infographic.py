"""Build a safe, self-contained infographic from Copilot CLI's text response.

The AI only supplies bounded plain text (instructions/infographic.md); the layout, colours and
markup are drawn here, so nothing the model writes can inject SVG. Cards grow with their content
and the canvas grows with the cards, so text is wrapped instead of overflowing.
"""
import html
import json
import re
import textwrap
from xml.etree import ElementTree

from ..i18n import t

MAX_RESPONSE_CHARS = 20000
MAX_ITEMS = 4
LIMITS = {"key_facts": 4, "timeline": 5, "evidence": 4, "code_locations": 3, "actions": 4}
SEVERITIES = ("critical", "high", "medium", "low", "unknown")
CONFIDENCES = ("high", "medium", "low")

W, M = 1600, 60                     # canvas width, outer margin
CW = W - 2 * M                      # content width
FONT = "Segoe UI, Arial, sans-serif"
MONO = "Cascadia Mono, Consolas, monospace"
INK, MUTED, FAINT = "#E7ECF7", "#91A0BA", "#66748D"
CARD, CARD_LINE = "#151D31", "#2A3550"
SEVERITY_COLOR = {"critical": "#FF6B6B", "high": "#FF9F43", "medium": "#FECA57", "low": "#54D69C", "unknown": "#8FA3C7"}
CONFIDENCE_COLOR = {"high": "#54D69C", "medium": "#FECA57", "low": "#FF9F43"}
ACCENT = {"summary": "#6AD3BB", "cause": "#89A8FF", "timeline": "#FFC66D", "evidence": "#78A8FF", "actions": "#73D5A8"}


def _clean_text(value, name, limit, required=False):
    if value is None and not required:
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
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
    for item in value:
        if len(result) >= limit:
            break
        if name == "key_facts":
            if isinstance(item, dict):
                label = _clean_text(item.get("label"), "key_facts.label", 24)
                val = _clean_text(item.get("value"), "key_facts.value", 32)
                if label or val:
                    result.append({"label": label, "value": val})
        elif isinstance(item, str):
            text = _clean_text(item, name, 240)
            if text:
                result.append({"time": "", "event": text} if name == "timeline" else text)
        elif name == "timeline" and isinstance(item, dict):
            event = _clean_text(item.get("event"), "timeline.event", 180)
            when = _clean_text(item.get("time"), "timeline.time", 48)
            if event:
                result.append({"time": when, "event": event})
    return result


def _choice(value, allowed, default):
    value = str(value or "").strip().lower()
    return value if value in allowed else default


def parse_ai_summary(response):
    """Parse and bound the JSON contract returned by Copilot CLI."""
    text = str(response or "").strip().lstrip("﻿")
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
    return {
        "title": _clean_text(data.get("title"), "title", 100, required=True),
        "subtitle": _clean_text(data.get("subtitle"), "subtitle", 100),
        "severity": _choice(data.get("severity"), SEVERITIES, "unknown"),
        "confidence": _choice(data.get("confidence"), CONFIDENCES, ""),
        "summary": _clean_text(data.get("summary"), "summary", 360, required=True),
        "root_cause": _clean_text(data.get("root_cause"), "root_cause", 360),
        "key_facts": _list(data.get("key_facts"), "key_facts", LIMITS["key_facts"]),
        "timeline": _list(data.get("timeline"), "timeline", LIMITS["timeline"]),
        "evidence": _list(data.get("evidence"), "evidence", LIMITS["evidence"]),
        "code_locations": _list(data.get("code_locations"), "code_locations", LIMITS["code_locations"]),
        "actions": _list(data.get("actions"), "actions", LIMITS["actions"]),
    }


# ---------------------------------------------------------------- drawing helpers
def _chars(width, size, mono=False):
    """Characters of `size` px text that fit in `width` px (a conservative average glyph width)."""
    return max(8, int(width / (size * (0.61 if mono else 0.5))))


def _wrap(value, width, max_lines):
    value = str(value or "").strip()
    lines = textwrap.wrap(value, width=width, break_long_words=True, break_on_hyphens=False) if value else []
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1].rstrip(" .…")
        lines[-1] = (last[:max(1, width - 1)].rstrip() + "…")
    return lines


def _text(x, y, lines, size=22, fill=INK, weight=400, line_height=1.28, family=FONT, anchor=None):
    if not lines:
        return ""
    spans = "".join(
        f'<tspan x="{x}" dy="{0 if index == 0 else line_height}em">{html.escape(line)}</tspan>'
        for index, line in enumerate(lines)
    )
    extra = f' text-anchor="{anchor}"' if anchor else ""
    return (f'<text x="{x}" y="{y}" fill="{fill}" font-size="{size}" '
            f'font-weight="{weight}" font-family="{family}"{extra}>{spans}</text>')


def _block_height(lines, size, line_height):
    return round(len(lines) * size * line_height) if lines else 0


def _card(x, y, width, height, label, accent):
    return (f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="22" '
            f'fill="{CARD}" stroke="{CARD_LINE}" stroke-width="2"/>'
            f'<rect x="{x + 24}" y="{y + 25}" width="7" height="27" rx="3.5" fill="{accent}"/>'
            f'<text x="{x + 47}" y="{y + 47}" fill="#AAB7D0" font-size="18" '
            f'font-weight="700" letter-spacing="1.4" font-family="{FONT}">{html.escape(label)}</text>')


def _pill(right, y, text, color):
    """Rounded badge whose right edge sits at `right`; returns (svg, width)."""
    width = int(len(text) * 15 * 0.66) + 34
    x = right - width
    svg = (f'<rect x="{x}" y="{y}" width="{width}" height="34" rx="17" fill="{color}" fill-opacity=".16" '
           f'stroke="{color}" stroke-opacity=".55" stroke-width="1.5"/>'
           f'<circle cx="{x + 17}" cy="{y + 17}" r="5" fill="{color}"/>'
           f'<text x="{x + 29}" y="{y + 22.5}" fill="{color}" font-size="15" font-weight="700" letter-spacing=".8" '
           f'font-family="{FONT}">{html.escape(text)}</text>')
    return svg, width


def _numbered(items, x, y, width, color, empty, check=False):
    """Numbered (or check-marked) list; returns (svg, height)."""
    if not items:
        lines = _wrap(empty, _chars(width, 20), 2)
        return _text(x, y + 20, lines, size=20, fill="#8794AC"), _block_height(lines, 20, 1.25) + 6
    out, top = [], y
    for index, item in enumerate(items):
        lines = _wrap(item, _chars(width - 46, 20), 3)
        cy = top + 13
        out.append(f'<circle cx="{x + 13}" cy="{cy}" r="13" fill="{color}" fill-opacity=".18"/>')
        if check:
            out.append(f'<path d="M{x + 7} {cy} l4.5 4.5 l8 -9" fill="none" stroke="{color}" stroke-width="2.6" '
                       'stroke-linecap="round" stroke-linejoin="round"/>')
        else:
            out.append(f'<text x="{x + 13}" y="{cy + 5.5}" fill="{color}" font-size="15" font-weight="700" '
                       f'text-anchor="middle" font-family="{FONT}">{index + 1}</text>')
        out.append(_text(x + 40, top + 20, lines, size=20, line_height=1.25))
        top += max(30, _block_height(lines, 20, 1.25)) + 22
    return "".join(out), top - y - 22


def _timeline(items, x, y, width, empty):
    """Vertical timeline; returns (svg, height)."""
    color = ACCENT["timeline"]
    if not items:
        lines = _wrap(empty, _chars(width, 20), 2)
        return _text(x, y + 20, lines, size=20, fill="#8794AC"), _block_height(lines, 20, 1.25) + 6
    out, top, dots = [], y, []
    for index, item in enumerate(items):
        when = _wrap(item["time"] or f"#{index + 1}", _chars(width - 40, 18), 1)
        event = _wrap(item["event"], _chars(width - 40, 19), 2)
        dots.append(top + 7)
        out.append(_text(x + 32, top + 14, when, size=18, fill=color, weight=700))
        out.append(_text(x + 32, top + 42, event, size=19, line_height=1.22))
        top += 24 + _block_height(event, 19, 1.22) + 24
    line = (f'<line x1="{x + 8}" y1="{dots[0]}" x2="{x + 8}" y2="{dots[-1]}" stroke="{color}" '
            'stroke-opacity=".35" stroke-width="2.5"/>') if len(dots) > 1 else ""
    marks = "".join(f'<circle cx="{x + 8}" cy="{d}" r="8" fill="#151D31" stroke="{color}" stroke-width="3"/>'
                    for d in dots)
    return line + marks + "".join(out), top - y - 24


def render_svg(data, issue):
    """Render bounded plain text into a self-contained SVG (no AI-supplied markup)."""
    data = {"subtitle": "", "severity": "unknown", "confidence": "", "key_facts": [], "code_locations": [], **data}
    title = data["title"]
    issue_text = _clean_text(str(issue or ""), "issue", 48)
    empty = t("infographic.no_items")
    body = []

    # header: brand, badges, title, subtitle
    body.append(f'<rect x="{M}" y="48" width="9" height="30" rx="4.5" fill="url(#accent)"/>')
    body.append(f'<text x="{M + 28}" y="70" fill="#8FA3C7" font-size="17" font-weight="700" letter-spacing="2" '
                f'font-family="{FONT}">{html.escape(t("infographic.brand"))}</text>')
    right = W - M
    severity = data["severity"]
    if severity != "unknown":
        svg, width = _pill(right, 44, f'{t("infographic.severity")} · {t("infographic.sev." + severity)}',
                           SEVERITY_COLOR[severity])
        body.append(svg)
        right -= width + 12
    if data["confidence"]:
        svg, _ = _pill(right, 44, f'{t("infographic.confidence")} · {t("infographic.conf." + data["confidence"])}',
                       CONFIDENCE_COLOR[data["confidence"]])
        body.append(svg)
    title_lines = _wrap(title, _chars(CW, 40), 2)
    body.append(_text(M, 132, title_lines, size=40, weight=700, fill="#F4F7FF", line_height=1.15))
    y = 132 + _block_height(title_lines, 40, 1.15) - 6
    meta = " · ".join(part for part in (issue_text, data["subtitle"]) if part)
    if meta:
        body.append(_text(M + 2, y + 12, _wrap(meta, _chars(CW, 20), 1), size=20, fill=MUTED))
        y += 34
    y += 26

    # key facts tiles
    facts = data["key_facts"]
    if facts:
        gap = 16
        width = (CW - gap * (len(facts) - 1)) / len(facts)
        for index, fact in enumerate(facts):
            x = round(M + index * (width + gap))
            body.append(f'<rect x="{x}" y="{y}" width="{round(width)}" height="92" rx="18" fill="#121A2D" '
                        f'stroke="{CARD_LINE}" stroke-width="1.5"/>')
            body.append(_text(x + 22, y + 32, _wrap(fact["label"].upper(), _chars(width - 44, 15), 1),
                              size=15, fill=MUTED, weight=700))
            body.append(_text(x + 22, y + 70, _wrap(fact["value"], _chars(width - 44, 27), 1),
                              size=27, fill="#F4F7FF", weight=700))
        y += 92 + 24

    # summary + root cause
    left_w, gap = 720, 24
    right_x, right_w = M + left_w + gap, CW - left_w - gap
    summary = _wrap(data["summary"], _chars(left_w - 56, 21), 5)
    cause = _wrap(data["root_cause"] or t("infographic.not_stated"), _chars(right_w - 56, 21), 5)
    cause_h = _block_height(cause, 21, 1.3)
    code = [_wrap(c, _chars(right_w - 84, 16, mono=True), 1)[0] for c in data["code_locations"] if c]
    code_h = (34 + len(code) * 28) if code else 0
    inner = max(_block_height(summary, 21, 1.3), cause_h + code_h)
    height = max(170, 80 + inner + 26)
    body.append(_card(M, y, left_w, height, t("infographic.summary"), ACCENT["summary"]))
    body.append(_text(M + 28, y + 92, summary, size=21, line_height=1.3))
    body.append(_card(right_x, y, right_w, height, t("infographic.root_cause"), ACCENT["cause"]))
    body.append(_text(right_x + 28, y + 92, cause, size=21, line_height=1.3))
    if code:
        top = y + 92 + cause_h + 12
        body.append(f'<text x="{right_x + 28}" y="{top + 4}" fill="#AAB7D0" font-size="14" font-weight="700" '
                    f'letter-spacing="1.2" font-family="{FONT}">{html.escape(t("infographic.code"))}</text>')
        for index, location in enumerate(code):
            row = top + 16 + index * 28
            body.append(f'<rect x="{right_x + 28}" y="{row}" width="{right_w - 56}" height="24" rx="6" '
                        'fill="#0E1526" stroke="#26314A"/>')
            body.append(_text(right_x + 40, row + 17, [location], size=16, fill="#C9D6FF", family=MONO))
    y += height + 24

    # timeline / evidence / actions
    cols = [(M, 470), (M + 490, 500), (M + 1010, 470)]
    pieces = [_timeline(data["timeline"], cols[0][0] + 30, y + 80, cols[0][1] - 60, empty),
              _numbered(data["evidence"], cols[1][0] + 30, y + 80, cols[1][1] - 60, ACCENT["evidence"], empty),
              _numbered(data["actions"], cols[2][0] + 30, y + 80, cols[2][1] - 60, ACCENT["actions"], empty, check=True)]
    height = max(300, 80 + max(h for _, h in pieces) + 34)
    for (x, width), key, (svg, _) in zip(cols, ("timeline", "evidence", "actions"), pieces):
        body.append(_card(x, y, width, height, t("infographic." + key), ACCENT[key]))
        body.append(svg)
    y += height

    total = max(900, y + 56)
    body.append(f'<text x="{W - M}" y="{total - 24}" text-anchor="end" fill="{FAINT}" font-size="14" '
                f'font-family="{FONT}">Ticket Analyzer · {html.escape(issue_text)}</text>')
    chunks = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{total}" viewBox="0 0 {W} {total}" role="img">',
        f'<title>{html.escape(title)}</title>',
        '<defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="#0B1222"/><stop offset="1" stop-color="#111B30"/>'
        '</linearGradient><linearGradient id="accent" x1="0" y1="0" x2="1" y2="0">'
        '<stop offset="0" stop-color="#70D7C0"/><stop offset="1" stop-color="#7FA7FF"/>'
        '</linearGradient></defs>',
        f'<rect width="{W}" height="{total}" fill="url(#bg)"/>',
        '<circle cx="1470" cy="65" r="190" fill="#344B7B" opacity=".12"/>',
        f'<circle cx="85" cy="{total - 90}" r="210" fill="#36BFA1" opacity=".07"/>',
        *body,
        '</svg>',
    ]
    svg = "\n".join(chunk for chunk in chunks if chunk)
    ElementTree.fromstring(svg)  # Fail before writing/uploading if the generated XML is invalid.
    return svg
