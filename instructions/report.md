# Report Instruction

Create a polished final Collab (Confluence) report from the supplied ticket analysis.
Preserve confirmed facts and evidence, keep the analysis' conclusions, identify risks and actions.

## Output contract (strict)

Return exactly one fenced `json` code block containing exactly one JSON string literal.
After JSON parsing, the string is valid, well-formed **Confluence Storage XHTML**:

- Start with `<ac:structured-macro ac:name="info"><ac:rich-text-body><p>…one-line summary…</p></ac:rich-text-body></ac:structured-macro>`.
- Use `<h2>`/`<h3>`, `<p>`, `<ul>/<ol>/<li>`, `<table><tbody><tr><th>/<td>`, `<strong>`, `<code>`.
- Code and log excerpts: `<ac:structured-macro ac:name="code"><ac:parameter ac:name="language">text</ac:parameter><ac:plain-text-body><![CDATA[ … ]]></ac:plain-text-body></ac:structured-macro>`.
- Include **exactly one** PlantUML sequence diagram, as a top-level macro under a heading
  `<h2>Diagnostic Diagram</h2>`:
  `<ac:structured-macro ac:name="plantuml"><ac:plain-text-body><![CDATA[@startuml … @enduml]]></ac:plain-text-body></ac:structured-macro>`.
  The diagram must have `participant` declarations and `->` interactions grounded in the evidence
  (observed vs. proposed steps).
- Escape `&`, `<`, `>` in text. No Markdown, no explanations, no text outside the code block.

## Suggested sections

Summary; Issue Overview (table: key, component, version, severity); Timeline; Evidence
(log lines, code references); Root Cause Analysis; Diagnostic Diagram; Recommended Actions
(table: priority, action, owner, verification); Risks; Open Questions.
