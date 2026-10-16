"""Render Confluence Storage XHTML as plain browser HTML for the preview tab.

Offline on purpose: PlantUML is shown as source (sending it to a public
PlantUML server would leak ticket content).
"""
import html
import re

_MACRO = re.compile(r'(?is)<ac:structured-macro\b[^>]*ac:name="([^"]+)"[^>]*>(.*?)</ac:structured-macro>')
_CDATA = re.compile(r"(?is)<ac:plain-text-body>\s*<!\[CDATA\[(.*?)\]\]>\s*</ac:plain-text-body>")
_RICH = re.compile(r"(?is)<ac:rich-text-body>(.*)</ac:rich-text-body>")
_TITLE = re.compile(r'(?is)<ac:parameter ac:name="title">(.*?)</ac:parameter>')


def _macro(match):
    name, body = match.group(1).lower(), match.group(2)
    title = _TITLE.search(body)
    label = html.escape(title.group(1)) if title else name
    code = _CDATA.search(body)
    if code:
        return (f'<div class="cf-macro cf-{name}"><div class="cf-macro-title">{label}</div>'
                f"<pre>{html.escape(code.group(1))}</pre></div>")
    rich = _RICH.search(body)
    inner = _MACRO.sub(_macro, rich.group(1)) if rich else ""
    return f'<div class="cf-macro cf-{name}"><div class="cf-macro-title">{label}</div>{inner}</div>'


def to_html(storage):
    text = _MACRO.sub(_macro, storage or "")
    # Nested macros are matched innermost-last by the lazy regex; run until stable.
    for _ in range(5):
        new = _MACRO.sub(_macro, text)
        if new == text:
            break
        text = new
    text = re.sub(r"(?is)<ac:parameter\b[^>]*>.*?</ac:parameter>", "", text)
    text = re.sub(r'(?is)<ac:link\b[^>]*>.*?<ri:page ri:content-title="([^"]*)"[^>]*/>.*?</ac:link>', r"<u>\1</u>", text)
    text = re.sub(r"(?is)</?(ac|ri):[^>]*>", "", text)
    return text
