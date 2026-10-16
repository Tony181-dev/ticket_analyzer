#!/usr/bin/env python3
"""
refine_pipeline.py  --  structure-safe, small-context Step 5.2 report refinement.

DROP-IN REPLACEMENT for client/refine_pipeline.py. Shared by both the pro client
(client/ticket_analyzer_tool.py) and the ez client
(client/ez-mode/ticket_analyzer_tool_ez.py).

Signature used by the caller:

    refined = refine_report(
        base_storage,
        llm_call,                       # llm_call(fragment, index, total) -> str
        max_input_tokens=REFINE_MAX_INPUT_TOKENS,     # e.g. 12000
        max_output_tokens=REFINE_MAX_OUTPUT_TOKENS,   # e.g. 4000
        reserve_tokens=REFINE_PROMPT_RESERVE_TOKENS,  # e.g. 1500
        log=self.log,                   # optional: log(str)
        cancel=lambda: self.check_cancelled(run_id),  # optional: raises to abort
    )

WHY
---
Step 5.2 sends the report to a small "format-guard" model (gpt-4o-mini:
~12k token INPUT window, ~4k token OUTPUT window) whose only job is to scan the
data and guarantee well-formed output before it is pushed to Collab. The binding
constraint is the OUTPUT window: the model cannot emit more than ~4k tokens, so
any fragment whose refined form is larger is silently truncated -> broken report.

This module fixes that with a chunk -> refine -> merge pass that is:
  * OUTPUT-BUDGET AWARE, STRUCTURE-SAFE (never splits CDATA / ac:structured-macro
    / table / fenced code), RECURSIVE for oversized prose, NON-DESTRUCTIVE
    (failed fragments keep their original text), CANCELLABLE, and BYTE-EXACT on
    merge.

Standard library only.
"""

from __future__ import annotations

import re
from typing import Callable, List, Optional


_CHARS_PER_TOKEN = 3.6
_INPUT_SAFETY_TOKENS = 512
_OUTPUT_SAFETY_TOKENS = 512
_MIN_CHUNK_TOKENS = 200


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return int(len(text) / _CHARS_PER_TOKEN) + 1


def _chunk_token_budget(max_input_tokens: int, max_output_tokens: int, reserve_tokens: int) -> int:
    out_budget = int(max_output_tokens) - _OUTPUT_SAFETY_TOKENS
    in_budget = int(max_input_tokens) - int(reserve_tokens) - _INPUT_SAFETY_TOKENS
    return max(_MIN_CHUNK_TOKENS, min(out_budget, in_budget))


_FENCE_RE = re.compile(r"^\s*```")
_HEADING_RE = re.compile(r"^\s*(?:#{1,6}\s|<h[1-6]\b)")
_MACRO_OPEN_RE = re.compile(r"<ac:structured-macro\b")
_MACRO_CLOSE_RE = re.compile(r"</ac:structured-macro>")
_TABLE_OPEN_RE = re.compile(r"<table\b")
_TABLE_CLOSE_RE = re.compile(r"</table>")
_PLANTUML_MACRO_RE = re.compile(
    r'(?is)<ac:structured-macro\b(?=[^>]*\bac:name=["\']plantuml["\'])[^>]*>'
    r'.*?</ac:structured-macro>'
)
_CDATA_RE = re.compile(r"(?is)<!\[CDATA\[(.*?)\]\]>")
_DIAGRAM_HEADING_RE = re.compile(
    r"(?is)<h[2-3]\b[^>]*>\s*Diagnostic\s+Diagrams?\s*</h[2-3]>"
)
_SEQUENCE_PARTICIPANT_RE = re.compile(
    r"(?im)^\s*(?:actor|boundary|control|entity|database|collections|queue|participant)\s+"
)
_SEQUENCE_INTERACTION_RE = re.compile(
    r"(?im)^\s*[A-Za-z_][A-Za-z0-9_]*\s+"
    r"(?:-\[[^\]]+\]>|--\[[^\]]+\]>|--?>|<--?)\s+"
    r"[A-Za-z_][A-Za-z0-9_]*\s*:"
)
_SEQUENCE_STYLE_LINES = (
    "hide footbox",
    "skinparam BackgroundColor white",
    "skinparam sequenceMessageAlign center",
    "skinparam responseMessageBelowArrow true",
    "skinparam ParticipantPadding 24",
    "skinparam BoxPadding 10",
)


def _fallback_plantuml_macro() -> str:
    return (
        '<ac:structured-macro ac:name="plantuml"><ac:plain-text-body><![CDATA['
        '@startuml\n'
        'hide footbox\n'
        'skinparam BackgroundColor white\n'
        'skinparam sequenceMessageAlign center\n'
        'skinparam responseMessageBelowArrow true\n'
        'skinparam ParticipantPadding 24\n'
        'skinparam BoxPadding 10\n'
        'participant "Issue Evidence" as Evidence #E8F5E9\n'
        'participant "Technical Assessment" as Assessment #FFF3E0\n'
        'participant "Required Verification" as Verify #ECEFF1\n'
        'Evidence -[#2E7D32]> Assessment: [Observed] Supply available ticket facts\n'
        'note over Assessment #ECEFF1\n'
        '  [Unknown] Decisive failure mechanism is not proven\n'
        'end note\n'
        'Assessment -[#D97706]> Verify: [Proposed] Run the P0 discriminating check\n'
        'Verify --[#607D8B]> Assessment: Pending evidence required for confirmation\n'
        '@enduml'
        ']]></ac:plain-text-body></ac:structured-macro>'
    )


def _plantuml_macro_is_complete(macro: str) -> bool:
    cdata = _CDATA_RE.search(macro or "")
    if not cdata:
        return False
    body = cdata.group(1).strip()
    return (
        body.count("@startuml") == 1
        and body.count("@enduml") == 1
        and body.startswith("@startuml")
        and body.endswith("@enduml")
        and bool(body[len("@startuml") : -len("@enduml")].strip())
        and bool(_SEQUENCE_PARTICIPANT_RE.search(body))
        and bool(_SEQUENCE_INTERACTION_RE.search(body))
    )


def _plantuml_macro_has_style(macro: str) -> bool:
    cdata = _CDATA_RE.search(macro or "")
    if not cdata:
        return False
    normalized_lines = {
        line.strip().lower() for line in cdata.group(1).splitlines()
    }
    return all(line.lower() in normalized_lines for line in _SEQUENCE_STYLE_LINES)


def _ensure_plantuml_sequence_style(macro: str) -> str:
    cdata = _CDATA_RE.search(macro or "")
    if not cdata:
        return macro
    body = cdata.group(1)
    lines = body.splitlines(keepends=True)
    normalized_lines = {line.strip().lower() for line in lines}
    missing = [
        line for line in _SEQUENCE_STYLE_LINES if line.lower() not in normalized_lines
    ]
    if not missing:
        return macro
    start_index = next(
        (index for index, line in enumerate(lines) if line.strip() == "@startuml"),
        None,
    )
    if start_index is None:
        return macro
    newline = "\r\n" if "\r\n" in body else "\n"
    insertion = "".join(line + newline for line in missing)
    lines.insert(start_index + 1, insertion)
    styled_body = "".join(lines)
    return macro[:cdata.start(1)] + styled_body + macro[cdata.end(1):]


def repair_required_plantuml(storage: str) -> str:
    """Preserve valid diagrams and repair the mandatory diagram contract."""
    text = str(storage or "")
    found = False
    prefix = (
        "<h2>Diagnostic Diagram</h2>"
        "<h3>Evidence-to-decision sequence</h3>"
        "<p>This diagram separates supplied evidence from the missing proof and "
        "the next decisive verification.</p>"
    )

    def repair_macro(match):
        nonlocal found
        found = True
        macro = match.group(0)
        if not _plantuml_macro_is_complete(macro):
            return _fallback_plantuml_macro()
        return _ensure_plantuml_sequence_style(macro)

    repaired = _PLANTUML_MACRO_RE.sub(repair_macro, text)
    if found:
        if not _DIAGRAM_HEADING_RE.search(repaired):
            repaired = _PLANTUML_MACRO_RE.sub(
                lambda match: prefix + "\n" + match.group(0),
                repaired,
                count=1,
            )
        return repaired
    heading = "" if _DIAGRAM_HEADING_RE.search(repaired) else "\n" + prefix
    return repaired.rstrip() + heading + "\n" + _fallback_plantuml_macro()


def validate_required_plantuml(storage: str) -> None:
    """Require at least one complete PlantUML macro in a final report."""
    macros = _PLANTUML_MACRO_RE.findall(storage or "")
    if not macros:
        raise ValueError(
            "Step 5.2 could not produce the required top-level PlantUML macro"
        )
    if len(macros) != 1:
        raise ValueError(
            "Step 5.2 requires exactly one diagnostic PlantUML sequence diagram"
        )
    for index, macro in enumerate(macros, 1):
        if not _plantuml_macro_is_complete(macro):
            raise ValueError(f"Step 5.2 could not repair PlantUML diagram {index}")
        if not _plantuml_macro_has_style(macro):
            raise ValueError(f"Step 5.2 could not style PlantUML diagram {index}")


class _Block:
    __slots__ = ("text", "atomic")

    def __init__(self, text: str, atomic: bool = False):
        self.text = text
        self.atomic = atomic

    def tokens(self) -> int:
        return estimate_tokens(self.text)


def split_blocks(report: str) -> List[_Block]:
    lines = report.splitlines(keepends=True)
    blocks: List[_Block] = []
    buf: List[str] = []
    buf_protected = False
    in_fence = False
    in_cdata = False
    macro_depth = 0
    table_depth = 0

    def protected_now() -> bool:
        return in_fence or in_cdata or macro_depth > 0 or table_depth > 0

    def flush(atomic: Optional[bool] = None):
        nonlocal buf, buf_protected
        if not buf:
            return
        text = "".join(buf)
        is_atomic = buf_protected if atomic is None else atomic
        blocks.append(_Block(text, is_atomic))
        buf = []
        buf_protected = False

    for line in lines:
        stripped = line.strip()
        if _FENCE_RE.match(line) and not in_cdata:
            if not in_fence:
                flush()
                in_fence = True
                buf_protected = True
                buf.append(line)
            else:
                buf.append(line)
                in_fence = False
                flush(atomic=True)
            continue
        if in_fence:
            buf.append(line)
            continue

        opened_cdata = "<![CDATA[" in line
        closed_cdata = "]]>" in line
        was_protected = protected_now()

        if opened_cdata and not in_cdata:
            if not was_protected:
                flush()
                buf_protected = True
            in_cdata = True

        if not in_cdata:
            m_open = len(_MACRO_OPEN_RE.findall(line))
            m_close = len(_MACRO_CLOSE_RE.findall(line))
            t_open = len(_TABLE_OPEN_RE.findall(line))
            t_close = len(_TABLE_CLOSE_RE.findall(line))
            if (m_open or t_open) and not was_protected:
                flush()
                buf_protected = True
            macro_depth = max(0, macro_depth + m_open - m_close)
            table_depth = max(0, table_depth + t_open - t_close)

        buf.append(line)

        if closed_cdata and in_cdata:
            in_cdata = False

        if not protected_now():
            if buf_protected:
                flush(atomic=True)
                continue
            if stripped == "":
                flush()
            elif _HEADING_RE.match(line) and len(buf) > 1:
                tail = buf.pop()
                flush()
                buf.append(tail)

    flush()
    return blocks


def _split_keep_sep(text: str, sep_regex: str) -> List[str]:
    parts = re.split(f"({sep_regex})", text)
    tokens: List[str] = []
    i = 0
    while i < len(parts):
        content = parts[i]
        sep = parts[i + 1] if i + 1 < len(parts) else ""
        piece = content + sep
        if piece:
            tokens.append(piece)
        i += 2
    return tokens


def _split_text_by_budget(text: str, budget_tokens: int) -> List[str]:
    if estimate_tokens(text) <= budget_tokens:
        return [text]
    for sep_regex in (r"\n\s*\n", r"\n", r"(?<=[.!?])\s+", r"\s+"):
        tokens = _split_keep_sep(text, sep_regex)
        if len(tokens) <= 1:
            continue
        pieces: List[str] = []
        cur = ""
        for tok in tokens:
            candidate = cur + tok
            if not cur or estimate_tokens(candidate) <= budget_tokens:
                cur = candidate
            else:
                pieces.append(cur)
                cur = tok
        if cur:
            pieces.append(cur)
        result: List[str] = []
        for piece in pieces:
            if estimate_tokens(piece) <= budget_tokens:
                result.append(piece)
            else:
                result.extend(_split_text_by_budget(piece, budget_tokens))
        if len(result) > 1:
            return result
    max_chars = max(1, int(budget_tokens * _CHARS_PER_TOKEN))
    return [text[i : i + max_chars] for i in range(0, len(text), max_chars)]


class _Chunk:
    __slots__ = ("text", "refinable", "reason")

    def __init__(self, text: str, refinable: bool, reason: str = ""):
        self.text = text
        self.refinable = refinable
        self.reason = reason


def chunk_report(report: str, budget_tokens: int, passthrough_oversized_atomic: bool = True) -> List[_Chunk]:
    blocks = split_blocks(report)
    chunks: List[_Chunk] = []
    pending: List[_Block] = []

    def pending_tokens() -> int:
        return estimate_tokens("".join(b.text for b in pending))

    def flush_pending():
        if pending:
            chunks.append(_Chunk("".join(b.text for b in pending), True))
            pending.clear()

    for block in blocks:
        if block.text.strip() == "":
            if pending:
                pending.append(block)
            else:
                chunks.append(_Chunk(block.text, False, "whitespace"))
            continue
        if block.atomic:
            flush_pending()
            if block.tokens() > budget_tokens and passthrough_oversized_atomic:
                chunks.append(_Chunk(block.text, False, "oversized-atomic"))
            else:
                chunks.append(_Chunk(block.text, True))
            continue
        if block.tokens() > budget_tokens:
            flush_pending()
            for piece in _split_text_by_budget(block.text, budget_tokens):
                chunks.append(_Chunk(piece, True))
            continue
        if pending and pending_tokens() + block.tokens() > budget_tokens:
            flush_pending()
        pending.append(block)

    flush_pending()
    return chunks


_BAD_STARTS = (
    "i'm sorry", "i am sorry", "as an ai", "i cannot", "i can't",
    "sorry, i", "here is the corrected", "here's the corrected",
)


def _looks_like_valid_refine(original: str, refined: Optional[str]) -> bool:
    if not refined or not refined.strip():
        return False
    r = refined.strip()
    low = r.lower()
    if low.startswith(_BAD_STARTS) and "<" not in r[:200]:
        return False
    if len(r) < len(original.strip()) * 0.55:
        return False
    return True


def refine_report(
    base_storage: str,
    llm_call: Callable[..., str],
    max_input_tokens: int = 12000,
    max_output_tokens: int = 4000,
    reserve_tokens: int = 1500,
    log: Optional[Callable[[str], None]] = None,
    cancel: Optional[Callable[[], None]] = None,
    max_retries: int = 2,
    passthrough_oversized_atomic: bool = True,
) -> str:
    def _log(msg: str):
        if log:
            try:
                log("[refine] " + msg)
            except Exception:
                pass

    def _cancel():
        if cancel:
            cancel()

    if not base_storage or not base_storage.strip():
        return base_storage

    budget = _chunk_token_budget(max_input_tokens, max_output_tokens, reserve_tokens)
    chunks = chunk_report(base_storage, budget, passthrough_oversized_atomic)
    refinable = [c for c in chunks if c.refinable]
    total = len(refinable)
    _log(
        f"budget={budget} tok/chunk  report={estimate_tokens(base_storage)} tok  "
        f"chunks={len(chunks)} (refinable={total})  "
        f"in={max_input_tokens} out={max_output_tokens} reserve={reserve_tokens}"
    )

    out_parts: List[str] = []
    refine_index = 0
    for chunk in chunks:
        if not chunk.refinable:
            out_parts.append(chunk.text)
            if chunk.reason == "oversized-atomic":
                _log(
                    f"passthrough oversized atomic block "
                    f"({estimate_tokens(chunk.text)} tok > budget) to avoid truncation"
                )
            continue
        idx0 = refine_index
        refine_index += 1
        refined_text = chunk.text
        last_note = ""
        for attempt in range(1, max_retries + 1):
            _cancel()
            try:
                candidate = llm_call(chunk.text, idx0, total)
                if _looks_like_valid_refine(chunk.text, candidate):
                    refined_text = candidate
                    last_note = ""
                    break
                last_note = "rejected (empty/refusal/truncated)"
                _log(f"fragment {idx0 + 1}/{total} attempt {attempt}: {last_note}")
            except Exception as exc:  # noqa: BLE001
                _cancel()
                last_note = str(exc) or exc.__class__.__name__
                _log(f"fragment {idx0 + 1}/{total} attempt {attempt} error: {last_note}")
        if last_note:
            _log(f"fragment {idx0 + 1}/{total}: kept ORIGINAL ({last_note})")
        out_parts.append(refined_text)

    merged = "".join(out_parts)
    _log(f"done: merged {len(chunks)} chunks ({total} refinable)")
    return merged


if __name__ == "__main__":
    import json

    sample = (
        "<ac:structured-macro ac:name=\"info\"><ac:rich-text-body><p>Banner</p></ac:rich-text-body></ac:structured-macro>\n"
        "<h1>Ticket Analysis</h1>\n"
        "<p>" + ("Prose about the issue. " * 500) + "</p>\n\n"
        "<ac:structured-macro ac:name=\"code\"><ac:plain-text-body><![CDATA[\n"
        + ("def f():\n    return 1  # " + "x" * 60 + "\n") * 200
        + "]]></ac:plain-text-body></ac:structured-macro>\n\n"
        "<table><tbody>\n"
        + "".join(f"<tr><td>row {i}</td><td>{'y'*80}</td></tr>\n" for i in range(150))
        + "</tbody></table>\n\n"
        "<h2>Conclusion</h2>\n<p>" + ("Final notes. " * 400) + "</p>\n"
    )
    IN, OUT, RES = 12000, 4000, 1500
    budget = _chunk_token_budget(IN, OUT, RES)
    calls = {"n": 0, "max_seen": 0}

    def fake_llm(fragment, index, total):
        calls["n"] += 1
        t = estimate_tokens(fragment)
        calls["max_seen"] = max(calls["max_seen"], t)
        assert t <= budget, f"fragment {index+1}/{total} exceeded budget: {t} > {budget}"
        return fragment

    result = refine_report(sample, fake_llm, max_input_tokens=IN, max_output_tokens=OUT, reserve_tokens=RES, log=print)
    print(json.dumps({
        "input_chars": len(sample), "output_chars": len(result), "budget_tokens": budget,
        "llm_calls": calls["n"], "max_fragment_tokens_sent": calls["max_seen"],
        "roundtrip_identical": result == sample,
    }, indent=2))
    assert result == sample, "non-destructive round trip failed"

    class Stop(Exception):
        pass

    def cancel_after_two():
        cancel_after_two.n += 1
        if cancel_after_two.n > 2:
            raise Stop()
    cancel_after_two.n = 0
    try:
        refine_report(sample, fake_llm, max_input_tokens=IN, max_output_tokens=OUT, reserve_tokens=RES, cancel=cancel_after_two)
        raise AssertionError("cancellation was not propagated")
    except Stop:
        pass
    print("SELF-TEST PASSED")
