# Ticket Analyzer v2 — design

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

## Setup Doctor

Checks are mode-aware (M365 vs CLI). Auto-fixes: `pip install playwright|uv`,
`playwright install chromium`, `winget install OpenJS.NodeJS.LTS`, `npm i -g @github/copilot`.
User actions: Jira/Collab tokens (2-field form, only when no usable `mcp_jira`/`mcp_collab`
exists in VS Code or Copilot CLI MCP config), M365 login (headed Chromium), `copilot login`.
`UV_NATIVE_TLS=1` is set for child processes so uv works behind TLS-intercepting proxies.

## Security notes

* Bound to 127.0.0.1; POST/PUT reject cross-origin requests; static/file routes refuse path traversal.
* Tokens stay in `data/config/mcp.json` (git-ignored) and are never sent to the browser.
* Copilot CLI runs with shell and write tools denied and builtin MCPs disabled.
* The report preview never sends PlantUML to an external renderer.
