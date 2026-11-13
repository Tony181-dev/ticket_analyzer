# Ticket Analyzer v2 — design

Author: hoang6.nguyen

## Goal

Replace three separate VS Code extensions with one local web app that the user installs once
(`start.bat`) and runs: Jira ticket → AI analysis → AI formatting → publish to Collab, with
either M365 Copilot (browser automation) or GitHub Copilot CLI as the AI backend.

| Legacy extension | What it did | Where it lives now |
|---|---|---|
| `1_Local_server_proxy_and_365` | HTTP proxy on :6969, M365 Playwright worker, MCP calls via VS Code `mcp.json`, Tkinter 7-step client | `app/m365_worker/` (worker unchanged), `app/mcp.py`, `app/pipeline.py`, `app/report/` |
| `0_logsleuth` | Source → log-statement catalog; huge logs → LLM-sized brief | `logsleuth/src/` (unchanged) + `logsleuth/cli.js` |
| `0_codegraph` | Source → graph.json / graph.simple.json / brain.html | `app/codegraph/` (unchanged) + `app/evidence/graph.py` |

## Architecture

```
browser (web/)  ──HTTP/SSE──▶  app/server.py  (stdlib ThreadingHTTPServer, 127.0.0.1 only)
                                   │
                                   ├─ pipeline.Executor  (one run at a time, cancellable)
                                   │     ├─ mcp.py ──stdio JSON-RPC──▶ uvx mcp-atlassian (jira / collab)
                                   │     ├─ ai/  m365.py ──NDJSON──▶ m365_worker/worker.py ─▶ Playwright
                                   │     │       copilot.py ──▶ node @github/copilot/npm-loader.js -p
                                   │     ├─ evidence/logsleuth.py ──▶ node logsleuth/cli.js scan|slice|span
                                   │     ├─ evidence/graph.py (in-process CodeGraph)
                                   │     └─ report/ storage.py (legacy XHTML repair) · refine_pipeline.py · collab.py
                                   └─ doctor.py (checks + auto-fix), picker.py (native Tk dialogs)
```

The server uses only the standard library so the UI (and Setup Doctor) always comes up, even
before `pip install` has succeeded.

## Steps

| # | Step | Runs when | Output |
|---|---|---|---|
| 1 | Jira (`jira_get_issue`) | always | `01-jira.md`, `01-jira.json` |
| 2 | AI understand ticket | log files given | Reads only the Jira ticket; `02-understanding.md`, `02-focus.json` (incident time window) |
| 3 | Log brief (LogSleuth) | log files given | Uses the source catalog when available; otherwise matches ticket keywords and generic error signals. `03-log-brief.md`; `03-catalog.md` only with source |
| 4 | Code graph | source folder given + Code Graph enabled | `04-brain.html`, `04-graph.simple.json`, `04-GRAPH_REPORT.md`, `04-graph.json` |
| 5 | AI analysis | always | `05-analysis.md` (editable); `05-source-evidence.md` when LogSleuth identifies source statements |
| 6 | AI format | always | `06-report.xhtml` (editable), preview, raw answer, refine log |
| 7 | AI infographic | Auto/Skip toggle on Step 7; M365 generates an image, Copilot CLI returns structured content rendered locally as SVG | `06-infographic.*`, attached to and embedded at the end of the Collab page |
| 8 | Collab publish | after review (configurable); when infographic is enabled, publish is complete only after the attachment and page update succeed | `07-collab.json` |

* Step 2 sees only the Jira ticket and extracts an explicitly stated incident time. It never reads
  logs or source. If no usable incident time is stated, the time filter is empty and LogSleuth scans
  the full time range of every log. The output is a versioned log-local-clock contract with
  validated HH:MM:SS[.mmm] values or null; invalid/missing JSON disables the time window.
* Step 3 runs whenever log files are supplied. With a selected Source code folder it reuses the
  LogSleuth catalog; without one it falls back to ticket keywords and generic error signals. Code
  Graph uses the same folder but has an independent checkbox, so it can be disabled without losing
  log cataloging.
  Auto uses default tuning, scans source and matches all catalog services using the validated
  window (full range if missing; retries full range only when the window contains no records).
  Manual exposes catalog/service/time/tuning controls, pauses after Slice so the user can review the
  brief, and continues only when Proceed is clicked; it never widens the chosen window.
  The final Markdown is guarded by a conservative UTF-8 byte/token upper bound, including all
  headings/tables; cap is 2,000-50,000 and never exceeds the application setting. See
  [LogSleuth integration](logsleuth-integration.md) for contracts, controls and limitations.
* Step 5 uses high reasoning effort and the Copilot CLI long-context tier. When LogSleuth matched
  source statements, it also includes bounded source excerpts around those exact `file:line`
  references; the compact graph alone is only a structural index, not proof of method behavior.
* Step 6 keeps the legacy contract (fenced JSON string → Confluence storage XHTML). Repair order:
  local deterministic repair → chunked AI refine (small model) only if still invalid.
* The run-level `continue_on_optional_error` option applies only to Steps 2–4. A failed step remains
  marked as an error, and later analysis includes only successful artifacts; Jira and required
  analysis/format/publish failures still stop the pipeline.
* Step 7's Auto/Skip control is shown in the Step 7 card. M365 uses native image generation; Copilot CLI returns bounded JSON and the app builds a self-contained SVG without executing model-provided markup. In Auto, image generation/attachment failures stop the publish path rather than silently producing a report-only success.
* Step 8 creates the page; on "already exists" it finds the id (error text → parent's children →
  title lookup) and updates instead.
* Editing an artifact marks downstream steps `stale`; re-run from any step.

## Jira attachments

* Listed from the `jira_get_issue` payload (mcp-atlassian includes them), else Jira REST
  `/rest/api/2/issue/<key>?fields=attachment`.
* Downloaded selectively over Jira REST with the token from the MCP config (Bearer PAT or Basic
  user+API token), streamed to `<download_dir or ~/Downloads/TicketAnalyzer>/<ISSUE>/`. A file already
  present with the same size is skipped. Fallback: MCP `jira_download_attachments` (base64, all files).
* Files marked "Analyze": archives (zip/tar/tgz via stdlib; 7z via `py7zr`; rar via Windows `tar.exe`) are extracted
  once into `<name>_extracted/` with path-traversal and size guards; log-like files are appended to the
  run's log inputs, which re-enables steps 2–3.
* "Auto" checkbox on the input card: after step 1, every log/archive attachment is fetched and analyzed.

## Models and language

* M365 model is chosen by its visible name in the M365 picker (default **GPT-5.6 Sol Think Deeper**); the worker
  searches the top-level menu first, then the GPT submenu. Copilot CLI model goes to `--model` (default `gpt-6-luna`; empty = auto).
* UI and server messages are Vietnamese / English (`settings.lang`, `app/i18n.py`, `web/i18n.js`).

## Packaging

`build_exe.bat` → PyInstaller one-folder build (`packaging/ticket_analyzer.spec`) bundling Playwright
(driver only), uv/uvx, web assets, LogSleuth and instructions → `dist/TicketAnalyzer-<ver>-win64.zip`.
Helper processes re-launch the same executable (`--m365-worker`, `--pick`, `--chromium-path`,
`--playwright`; see `app/runtime.py`). Browsers stay in the per-user Playwright cache so an existing
Chromium is reused. Data lives next to the exe (or `%LOCALAPPDATA%/TicketAnalyzer` if read-only).

## Setup Doctor

Checks are mode-aware (M365 vs CLI) and never reinstall what exists: PATH is re-read from the
registry before each check, tools are also searched in their usual install folders, and every fix
re-checks right before installing. `start.bat` creates the venv with `--system-site-packages` and skips
pip entirely when the requirements already import. Copilot sign-in is detected from env tokens,
`gh auth status`, Copilot config (JSON with comments) or the Windows credential store, and can be
verified with a one-word test prompt (also run automatically when the `copilot login` window closes).
M365 login is probed once in the background at app startup when Playwright/Chromium are available;
the cached result is optional when Copilot CLI is selected.
Auto-fixes: `pip install playwright|uv`,
`playwright install chromium`, `winget install OpenJS.NodeJS.LTS`, `npm i -g @github/copilot`.
User actions: Jira/Collab tokens (2-field form, only when no usable `mcp_jira`/`mcp_collab`
exists in VS Code or Copilot CLI MCP config), M365 login (headed Chromium), `copilot login`.
`UV_NATIVE_TLS=1` is set for child processes so uv works behind TLS-intercepting proxies.

## Security notes

* Bound to 127.0.0.1; POST/PUT reject cross-origin requests; static/file routes refuse path traversal.
* Tokens stay in `data/config/mcp.json` (git-ignored) and are never sent to the browser.
* Copilot CLI runs with shell and write tools denied and builtin MCPs disabled.
* The report preview never sends PlantUML to an external renderer.
