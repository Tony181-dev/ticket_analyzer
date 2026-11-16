# Infographic Instruction

Turn the supplied ticket analysis (`05-analysis.md`) into the content of ONE incident infographic:
a single landscape slide that an engineer or manager understands in ten seconds. Use only facts
stated in the analysis. Never invent times, numbers, causes, file paths or line numbers; leave a
field empty (or an empty array) when the analysis does not support it.

## Writing rules

- Same language as the analysis. Plain text only: no Markdown, no HTML, no emoji.
- Short, scannable phrases rather than sentences: lead with the key noun or verb, drop filler
  ("It was observed that…", "The analysis shows…").
- Keep identifiers exact (error codes, status values, symbols, `file:line`), never paraphrase them.
- Respect the length limits below: text past them is cut off in the picture.
- Order every list by importance (timeline: by time).

## JSON contract (Copilot CLI / SVG route)

Return exactly one JSON object, no code fence and no text around it:

```
{
  "title":        "≤ 70 chars - the problem as a headline, e.g. 'BT connect fails with status 133 after resume'",
  "subtitle":     "≤ 80 chars - affected component / feature / build, or empty",
  "severity":     "critical | high | medium | low | unknown  (user impact stated in the analysis)",
  "confidence":   "high | medium | low  (confidence of the top root-cause hypothesis)",
  "summary":      "≤ 220 chars - what happens and who is affected",
  "root_cause":   "≤ 220 chars - the top hypothesis; prefix with 'Hypothesis:' when not verified",
  "key_facts":    [{"label": "≤ 18 chars", "value": "≤ 24 chars"}],
  "timeline":     [{"time": "HH:MM:SS or a short label", "event": "≤ 70 chars"}],
  "evidence":     ["≤ 110 chars - the decisive log line or fact, with its source (file / timestamp)"],
  "code_locations": ["≤ 70 chars - `path/File.kt:123 · Class.method`, only from the analysis"],
  "actions":      ["≤ 100 chars - verb first, most important first"]
}
```

Limits: `key_facts` at most 4 (good candidates: error code, first occurrence, frequency or
reproduction rate, device / build / version); `timeline` at most 5; `evidence` at most 4;
`code_locations` at most 3; `actions` at most 4.

## Image route (M365 image generation)

When asked for an image instead of JSON, draw the same content as one 16:9 landscape infographic:

- Layout: header band (title, subtitle, severity badge) → a row of up to four key-fact tiles →
  two cards side by side (Summary, Root cause with confidence) → three columns (Timeline as a
  vertical line with dots, Evidence as a numbered list, Recommended actions as a checklist).
- Style: flat, modern, dark navy background, one accent colour per card, generous spacing, clear
  visual hierarchy, rounded cards; no photos, no people, no logos, no decorative clutter.
- Text: large, sharp and legible; copy the exact wording, codes and times from the analysis;
  never add text that is not in the analysis; keep to the length limits above.
