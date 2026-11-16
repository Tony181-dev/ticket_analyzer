# AI assistant chat bubble — design

Date: 2026-11-16 · Status: approved (approach A, builder's calls delegated: "cái gì tốt và hợp lý nhất thì bạn làm")

## Intent

A chat bubble in the bottom-right corner of Ticket Analyzer, backed by Copilot CLI with the model from
Settings. It sees the live system (current run, steps, errors, attachments, logs, settings), answers
questions, proactively helps at the most error-prone part of the flow — choosing, downloading and
extracting attachments and picking logs — suggests adjustments, and writes local Markdown bug tickets
for defects of Ticket Analyzer itself.

## Goals / non-goals / anti-goals

- Goals: one-click execution of what the AI proposes; proactive help when the evidence gate opens or a
  step fails; useful bug tickets (environment + log excerpt collected by the app, not invented).
- Non-goals: editing the tool's code from the chat; replacing the pipeline's own AI steps.
- Anti-goals: the AI acting without the user's click; the chat blocking or being killed by the
  pipeline (or vice versa); secrets (MCP tokens) leaking into prompts.

## Approaches considered

- **A (chosen)** — the server writes a bounded snapshot of the system for every turn; Copilot CLI reads
  it read-only (shell/write tools denied); the reply carries a fenced `actions` JSON block that the UI
  renders as buttons; the server validates and executes an action only when the user clicks.
- B — expose the app as an MCP server so Copilot acts directly: more autonomous, but actions happen
  without a click, the read-only guarantee is lost and a wrong action is hard to stop.

## Design

- `app/assistant.py`: chat history (`data/assistant/chat.json`), snapshot (`work/context.md`),
  conversation (`work/conversation.md`), instruction (`instructions/assistant.md`), reply parsing,
  action whitelist + validation + execution, bug tickets (`data/bugs/BUG-*.md`), recent server errors.
- Copilot CLI: own process slot (`slot="assistant"`) so it runs beside a pipeline call and the
  pipeline's Stop does not kill it; `--add-dir` for the run folder, source folder, download folder and
  (dev checkout) the app source so the AI can open artifacts and locate tool bugs; prompt stays a short
  constant (Windows command-line cap), all material is in files.
- Actions: `download_attachments`, `resolve_gate`, `run_steps`, `log_scan`, `update_inputs`,
  `update_settings`, `create_bug`, `open_folder`, `stop_pipeline`. Unknown types and bad values are
  dropped server-side.
- Proactive: when a run's evidence gate opens or a step fails, the UI asks for a focused scan once per
  event (server de-duplicates by trigger key). Setting `assistant_proactive` (default on).
- UI: `web/assistant.js` + CSS; FAB with attention badge, panel with Markdown answers, action cards,
  quick prompts, Scan / bug folder / clear / stop; VI + EN strings.

## Related changes in the same work

- `instructions/ticket-analysis.md`: new "Suggested Solution and Code Location" section (related-code
  verdict, ranked suspected locations, proposed fix, impact); `report.md` keeps it in the Collab page.
- Infographic: `instructions/infographic.md` + richer JSON contract (severity, confidence, key facts,
  code locations) and an auto-growing SVG layout.
- Copilot model list updated to the CLI's supported ids; default stays `gpt-6-luna`.

## Added during the same session (user requests)

- Time zone of the incident time: when step 2 finds no zone, Review/Manual pause the Log step after the
  logs are ready (`pending_tz`: each log's time range, candidate alignments with in-range check, the app's
  suggestion) and the user picks suggested / own zones (live preview) / no time filter; Skip aligns
  automatically and logs a warning. The assistant gets a `resolve_timezone` action and a proactive scan.
- Languages: Korean UI and server messages; analysis, Collab report and infographic follow the UI language
  (localized diagram heading); a warning when publishing a report written in another language.
- M365 Copilot: a full workflow run opens a new conversation; the prompt is verified in the composer before
  sending (attachments alone are never sent; fill → insert_text → key presses, then a clear error).
  M365 accepts at most 3 attachments per prompt: beyond that `ai.m365_attachments` keeps the instruction
  (always last) and any non-text file whole and combines the text evidence, in order and size-balanced, into
  bundle files with `===== FILE: <name> =====` markers; the prompt says which file went where. The worker
  rejects more than 3 attachments.
- Attachment download progress: one bar per file (queued / downloading % / downloaded / already present /
  failed / extracting / extracted).
