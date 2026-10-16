"""Native file/folder dialogs. Browsers cannot return local paths, but the
server runs on the user's desktop, so it can open a Tk dialog itself."""
import json
import subprocess

from . import paths

_SCRIPT = r"""
import json, sys, tkinter as tk
from tkinter import filedialog
root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True); root.update()
kind = sys.argv[1]
if kind == 'folder':
    r = filedialog.askdirectory(title='Chọn folder source code')
    out = [r] if r else []
else:
    r = filedialog.askopenfilenames(title='Chọn file log', filetypes=[
        ('Log files', '*.log *.txt *.dlt *.gz *.out *.slog *.slog2 *.trace *.err *.jsonl *.ndjson *.json'),
        ('All files', '*.*')])
    out = list(r)
print(json.dumps(out))
"""


def pick(kind):
    out = subprocess.run([paths.PYTHON, "-c", _SCRIPT, "folder" if kind == "folder" else "files"],
                         capture_output=True, text=True, timeout=600)
    try:
        return json.loads(out.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise RuntimeError("Không mở được hộp thoại chọn file (thiếu Tkinter?). Hãy dán đường dẫn vào ô.")
