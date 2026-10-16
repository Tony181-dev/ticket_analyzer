"""How the app re-launches itself for helper processes.

Source checkout : python -m app --<tool> ...
Packaged EXE    : TicketAnalyzer.exe --<tool> ...   (no separate python.exe exists)

Helper tools (dispatched in app/__main__.py):
  --m365-worker              the legacy M365 Playwright worker (NDJSON on stdin/stdout)
  --pick <files|folder> <t>  native file/folder dialog, prints a JSON list
  --chromium-path            prints Playwright's Chromium path if it is installed
  --playwright <args...>     the Playwright CLI (e.g. install chromium)
"""
import sys

from . import paths


def tool_cmd(tool, *args):
    if paths.FROZEN:
        return [sys.executable, f"--{tool}", *map(str, args)]
    return [sys.executable, "-u", "-m", "app", f"--{tool}", *map(str, args)]


def run_tool(argv):
    """Handle a helper invocation; returns True when argv was a helper call."""
    if not argv or not argv[0].startswith("--"):
        return False
    tool, args = argv[0][2:], argv[1:]
    if tool == "m365-worker":
        import runpy
        worker = paths.M365_WORKER
        sys.path.insert(0, str(worker.parent))  # worker.py uses flat imports (browser, sessions, ...)
        sys.argv = [str(worker)]
        runpy.run_path(str(worker), run_name="__main__")
        return True
    if tool == "pick":
        from .picker import dialog
        dialog(args[0] if args else "files", args[1] if len(args) > 1 else "")
        return True
    if tool == "chromium-path":
        import os
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        try:
            exe = pw.chromium.executable_path
            print(exe if os.path.exists(exe) else "")
        finally:
            pw.stop()
        return True
    if tool == "playwright":
        from playwright.__main__ import main
        sys.argv = ["playwright", *args]
        main()
        return True
    return False
