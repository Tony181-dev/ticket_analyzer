"""Native file/folder dialogs. Browsers cannot return local paths, but the
server runs on the user's desktop, so it can open a Tk dialog itself (in a
helper process, so Tk never runs on a server thread)."""
import json
import subprocess

from .i18n import lang, t
from .runtime import tool_cmd
from . import paths


def dialog(kind, title):
    """Runs inside the helper process: show the dialog, print the JSON result."""
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    root.update()
    if kind == "folder":
        r = filedialog.askdirectory(title=title)
        out = [r] if r else []
    else:
        r = filedialog.askopenfilenames(title=title, filetypes=[
            ("Log files", "*.log *.txt *.dlt *.gz *.zip *.out *.slog *.slog2 *.trace *.err *.jsonl *.ndjson *.json"),
            ("All files", "*.*")])
        out = list(r)
    print(json.dumps(out))


def pick(kind):
    en = lang() == "en"
    if kind == "folder":
        title = "Choose a folder" if en else "Chọn thư mục"
    else:
        title = "Choose log files" if en else "Chọn file log"
    out = subprocess.run(tool_cmd("pick", "folder" if kind == "folder" else "files", title),
                         capture_output=True, text=True, timeout=600, cwd=str(paths.ROOT), encoding="utf-8")
    try:
        return json.loads(out.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise RuntimeError(t("err.picker"))
