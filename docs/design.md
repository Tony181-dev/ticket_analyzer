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
| 2 | AI understand ticket | log files given | `02-understanding.md`, `02-focus.json` (time window, keywords, services), `02-log-span.md` |
| 3 | Log brief (LogSleuth) | log files given | `03-log-brief.md`, `03-catalog.md` (when source given; otherwise error-signal + keyword seeding) |
| 4 | Code graph | source folder given | `04-brain.html`, `04-graph.simple.json`, `04-GRAPH_REPORT.md`, `04-graph.json` |
| 5 | AI analysis | always | `05-analysis.md` (editable) |
| 6 | AI format | always | `06-report.xhtml` (editable), preview, raw answer, refine log |
| 7 | Collab publish | after review (configurable) | `07-collab.json` |

* Step 2 gets the **actual clock range of each log file** (`cli.js span`) so the AI maps ticket
  times onto the log clock. If the chosen window keeps 0 lines, step 3 re-slices the whole log.
* Step 6 keeps the legacy contract (fenced JSON string → Confluence storage XHTML). Repair order:
  local deterministic repair → chunked AI refine (small model) only if still invalid.
* Step 7 creates the page; on "already exists" it finds the id (error text → parent's children →
  title lookup) and updates instead.
* Editing an artifact marks downstream steps `stale`; re-run from any step.

## Jira attachments

* Listed from the `jira_get_issue` payload (mcp-atlassian includes them), else Jira REST
  `/rest/api/2/issue/<key>?fields=attachment`.
* Downloaded selectively over Jira REST with the token from the MCP config (Bearer PAT or Basic
  user+API token), streamed to `<download_dir or ~/Downloads/TicketAnalyzer>/<ISSUE>/`. A file already
  present with the same size is skipped. Fallback: MCP `jira_download_attachments` (base64, all files).
* Files marked "Analyze": archives (zip/tar/tgz via stdlib; 7z/rar via Windows `tar.exe`) are extracted
  once into `<name>_extracted/` with path-traversal and size guards; log-like files are appended to the
  run's log inputs, which re-enables steps 2–3.
* "Auto" checkbox on the input card: after step 1, every log/archive attachment is fetched and analyzed.

## Models and language

* M365 model is chosen by its visible name in the M365 picker (default **GPT 6 Luna**); the worker
  searches the top-level menu first, then the GPT submenu. Copilot CLI model goes to `--model` (empty = auto).
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
