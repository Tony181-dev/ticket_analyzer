"""Confluence Storage XHTML decoding / repair / validation.

Ported verbatim from the legacy client (ticket_analyzer_tool.py) so the
battle-tested repair rules are unchanged. Only the Tkinter app was dropped.
"""
import html
import json
import re

from .refine_pipeline import repair_required_plantuml, validate_required_plantuml

JIRA_BASE_URL = "http://jira.lge.com/issue/browse"


def last_fence(text, languages=("json", "xml", "html", "")):
    """Return the last fenced block (with its fences) whose language matches.

    Copilot CLI may print a sentence before the answer; the legacy decoder
    expects the response to *be* the fence, so isolate it first.
    """
    blocks = list(re.finditer(r"(?s)```([A-Za-z]*)[ \t]*\r?\n(.*?)\r?\n```", str(text or "")))
    for block in reversed(blocks):
        if block.group(1).lower() in languages:
            return block.group(0)
    return text


def strip_chat_wrappers(text):
    """Remove M365 UI wrappers ("Copilot said:", reasoning notes) from Markdown answers."""
    text = str(text or "").strip()
    text = re.sub(r"(?is)^\s*Copilot\s+said\s*:\s*", "", text, count=1)
    text = re.sub(r"(?is)^\s*Reasoning\s+completed\s+in\s+\d+\s+steps?\s*", "", text, count=1)
    return text.strip()


def clean_report_output(value):
    """Remove only outer Copilot wrappers from the Step 5 response."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    separator = r"(?:\r?\n|<br\s*/?>)"
    text = re.sub(rf"(?is)^\s*Copilot\s+said\s*:\s*{separator}*", "", text, count=1)
    text = re.sub(
        rf"(?is)^\s*Reasoning\s+completed\s+in\s+\d+\s+steps?\s*{separator}*",
        "",
        text,
        count=1,
    )
    text = re.sub(rf"(?is)^\s*JSON\s*{separator}*", "", text, count=1)
    text = re.sub(r"(?is)^\s*```(?:json|xml|html)?\s*", "", text, count=1)
    text = re.sub(r"(?is)\s*```\s*$", "", text, count=1)
    text = re.sub(rf"(?is){separator}*\s*Show\s+more\s+lines\s*$", "", text, count=1)
    text = re.sub(
        rf"(?is){separator}\s*(?:#{{1,6}}\s*)?Sources\s*:?\s*(?:{separator}.*)?$",
        "",
        text,
        count=1,
    )
    text = re.sub(rf"(?is){separator}*\s*Show\s+more\s+lines\s*$", "", text, count=1)
    text = text.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        try:
            decoded = json.loads(text)
            if isinstance(decoded, str):
                text = decoded
        except Exception:
            text = text[1:-1]
            text = text.replace('\\"', '"').replace('\\n', '\n').replace('\\r', '')
    return text.strip()
def parse_step5_storage_response(value):
    """Decode Step 5's fenced JSON string into Confluence storage XHTML."""
    text = clean_report_output(value)
    text = str(text or "").strip()
    if not text:
        return ""
    fence = re.fullmatch(r"(?is)\s*```(?:json|xml|html)?\s*(.*?)\s*```\s*", text)
    if fence:
        text = fence.group(1).strip()
    numbered_prefix = re.match(r"(?is)^\s*\d+\s*\r?\n", text)
    if numbered_prefix and text[numbered_prefix.end():].lstrip().startswith('"'):
        text = text[numbered_prefix.end():].lstrip()
    if text.startswith('"'):
        try:
            decoded, _ = json.JSONDecoder().raw_decode(text)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Step 5 JSON string is invalid at line {exc.lineno}, "
                f"column {exc.colno}: {exc.msg}"
            ) from exc
        if not isinstance(decoded, str):
            raise ValueError("Step 5 response must decode to one JSON string")
        text = decoded.strip()
    for _ in range(2):
        previous = text
        text = text.replace("&amp;lt;", "&lt;").replace("&amp;gt;", "&gt;")
        text = text.replace("&lt;", "<").replace("&gt;", ">")
        text = text.replace("&quot;", '"').replace("&#34;", '"')
        if text == previous:
            break
    return "".join(
        character
        for character in text.strip()
        if character in "\t\n\r" or ord(character) >= 0x20
    )
def repair_storage_xml(text):
    """Repair XML characters without changing the meaning of CDATA content."""
    def split_cdata(match):
        content = match.group(1)
        return "<![CDATA[" + content.replace("]]>", "]]]]><![CDATA[>") + "]]>"
    text = re.sub(
        r"(?s)<!\[CDATA\[(.*?)\]\]>(?=\s*</ac:plain-text-body>)",
        split_cdata,
        text,
    )
    segments = re.split(r"(?s)(<!\[CDATA\[.*?\]\]>)", text)
    for index in range(0, len(segments), 2):
        segments[index] = re.sub(
            r"&(?!amp;|lt;|gt;|quot;|apos;|#\d+;|#x[0-9A-Fa-f]+;)",
            "&amp;",
            segments[index],
        )
    return "".join(segments)
def repair_storage_table_markup(value):
    """Repair malformed table row/cell boundaries without changing other XHTML."""
    text = str(value or "")
    text = re.sub(r"(?is)</(td|th|tr)>\s*</\s*</\1\s*>", r"</\1>", text)
    text = re.sub(r"(?is)</\s*</(td|th|tr)\s*>", r"</\1>", text)
    text = re.sub(r"(?is)</tr>\s*<(td|th)>", r"</tr><tr><\1>", text)
    text = re.sub(r"(?is)(</td>|</th>)\s*<tr>", r"\1</tr><tr>", text)
    text = re.sub(r"(?is)(</td>|</th>)\s*</tbody>", r"\1</tr></tbody>", text)
    text = re.sub(r"(?is)</(td|th)>\s*</\1>", r"</\1>", text)
    return text
def repair_storage_structure(value):
    """Repair recoverable tag nesting errors while preserving text and CDATA."""
    text = str(value or "")
    cdata_blocks = []
    def protect_cdata(match):
        cdata_blocks.append(match.group(0))
        return f"__CDATA_BLOCK_{len(cdata_blocks) - 1}__"
    text = re.sub(r"(?s)<!\[CDATA\[.*?\]\]>", protect_cdata, text)
    tag_pattern = re.compile(r"<(/?)([A-Za-z_][\w:.-]*)(?:\s[^<>]*?)?(/?)>")
    stack = []
    output = []
    cursor = 0
    list_tags = {"li", "ul", "ol"}
    for match in tag_pattern.finditer(text):
        output.append(text[cursor:match.start()])
        closing, name, self_closing = match.groups()
        normalized_name = name.lower()
        if closing:
            matching_index = next(
                (index for index in range(len(stack) - 1, -1, -1) if stack[index] == normalized_name),
                None,
            )
            if matching_index is not None:
                while len(stack) - 1 > matching_index:
                    output.append(f"</{stack.pop()}>")
                output.append(match.group(0))
                stack.pop()
        else:
            if re.fullmatch(r"h[1-6]", normalized_name) and any(item in list_tags for item in stack):
                while stack and any(item in list_tags for item in stack):
                    output.append(f"</{stack.pop()}>")
            output.append(match.group(0))
            if not self_closing:
                stack.append(normalized_name)
        cursor = match.end()
    output.append(text[cursor:])
    while stack:
        output.append(f"</{stack.pop()}>")
    repaired = "".join(output)
    for index, block in enumerate(cdata_blocks):
        repaired = repaired.replace(f"__CDATA_BLOCK_{index}__", block)
    return repaired
def normalize_storage_report(value, issue_key=""):
    """Decode and validate the XHTML contract returned by Step 5."""
    text = parse_step5_storage_response(value)
    if not text:
        return ""
    if not re.search(r"(?is)<(?:ac:|h[1-6]\b|p\b|table\b|ul\b|ol\b|blockquote\b)", text):
        raise ValueError(
            "Step 5 response does not contain Confluence storage XHTML. "
            "Check that report-instruction.md is being sent to the model."
        )
    storage = repair_storage_xml(text)
    storage = repair_storage_table_markup(storage)
    storage = repair_storage_structure(storage)
    storage = repair_required_plantuml(storage)
    storage = "".join(
        character
        for character in storage
        if character in "\t\n\r" or ord(character) >= 0x20
    )
    import xml.etree.ElementTree as ET
    wrapped = (
        '<root xmlns:ac="http://atlassian.com/content" '
        'xmlns:ri="http://atlassian.com/resource/identifier">'
        + storage
        + '</root>'
    )
    try:
        ET.fromstring(wrapped)
    except ET.ParseError as exc:
        line, column = getattr(exc, "position", (0, 0))
        raise ValueError(
            f"Locally generated Confluence storage XHTML is invalid at "
            f"line {line}, column {column}: {exc}"
        ) from exc
    validate_required_plantuml(storage)
    return storage

def inline_storage(text):
    value = html.escape(text, quote=False)
    value = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', value)
    value = re.sub(r"`([^`]+)`", r"<code>\1</code>", value)
    value = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", value)
    value = re.sub(r"__([^_]+)__", r"<strong>\1</strong>", value)
    value = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", value)
    return value
def markdown_to_storage(markdown_text, issue_key=""):
    """Convert common Markdown to Confluence storage-format XHTML."""
    lines = (markdown_text or "").replace("\r\n", "\n").split("\n")
    output = []
    paragraph = []
    list_type = None
    in_code = False
    code_language = ""
    code_lines = []
    table_lines = []
    def flush_paragraph():
        nonlocal paragraph
        if paragraph:
            output.append("<p>" + "<br/>".join(inline_storage(item) for item in paragraph) + "</p>")
            paragraph = []
    def close_list():
        nonlocal list_type
        if list_type:
            output.append(f"</{list_type}>")
            list_type = None
    def flush_table():
        nonlocal table_lines
        if not table_lines:
            return
        rows = [[cell.strip() for cell in row.strip().strip("|").split("|")] for row in table_lines]
        if len(rows) >= 2 and all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in rows[1]):
            rows.pop(1)
        output.append('<table><tbody>')
        for row_index, cells in enumerate(rows):
            tag = "th" if row_index == 0 else "td"
            output.append("<tr>" + "".join(f"<{tag}>{inline_storage(cell)}</{tag}>" for cell in cells) + "</tr>")
        output.append("</tbody></table>")
        table_lines = []
    for line in lines:
        stripped = line.strip()
        fence = re.match(r"^```\s*([A-Za-z0-9_+-]*)", stripped)
        if fence:
            flush_paragraph(); close_list(); flush_table()
            if not in_code:
                in_code = True; code_language = fence.group(1); code_lines = []
            else:
                params = f'<ac:parameter ac:name="language">{html.escape(code_language)}</ac:parameter>' if code_language else ""
                code = html.escape("\n".join(code_lines), quote=False)
                output.append(f'<ac:structured-macro ac:name="code">{params}<ac:plain-text-body><![CDATA[{code}]]></ac:plain-text-body></ac:structured-macro>')
                in_code = False; code_language = ""; code_lines = []
            continue
        if in_code:
            code_lines.append(line)
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            flush_paragraph(); close_list(); table_lines.append(stripped); continue
        flush_table()
        if not stripped:
            flush_paragraph(); close_list(); continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading:
            flush_paragraph(); close_list(); level = len(heading.group(1)); output.append(f"<h{level}>{inline_storage(heading.group(2))}</h{level}>"); continue
        item = re.match(r"^[-*+]\s+(.+)$", stripped)
        ordered = re.match(r"^\d+[.)]\s+(.+)$", stripped)
        if item or ordered:
            flush_paragraph(); desired = "ul" if item else "ol"
            if list_type != desired:
                close_list(); list_type = desired; output.append(f"<{desired}>")
            output.append("<li>" + inline_storage((item or ordered).group(1)) + "</li>")
            continue
        quote = re.match(r"^>\s?(.*)$", stripped)
        if quote:
            flush_paragraph(); close_list(); output.append("<blockquote><p>" + inline_storage(quote.group(1)) + "</p></blockquote>"); continue
        paragraph.append(stripped)
    if in_code:
        code = html.escape("\n".join(code_lines), quote=False)
        output.append(f'<ac:structured-macro ac:name="code"><ac:plain-text-body><![CDATA[{code}]]></ac:plain-text-body></ac:structured-macro>')
    flush_paragraph(); close_list(); flush_table()
    if issue_key:
        jira_url = f"{JIRA_BASE_URL.rstrip('/')}/{html.escape(issue_key)}"
        banner = '<p><ac:structured-macro ac:name="info"><ac:rich-text-body><p>Auto-generated Jira ticket analysis. Source: '
        banner += f'<a href="{jira_url}">{html.escape(issue_key)}</a>'
        banner += '</p></ac:rich-text-body></ac:structured-macro></p>'
        output.insert(0, banner)
    return "\n".join(output)
