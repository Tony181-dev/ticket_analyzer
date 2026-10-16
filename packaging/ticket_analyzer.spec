# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for TicketAnalyzer.exe (one-folder build). Run via build_exe.bat.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

root = Path(SPECPATH).parent
sys.path.insert(0, str(root))

datas = [
    (str(root / "web"), "web"),
    (str(root / "logsleuth"), "logsleuth"),
    (str(root / "instructions"), "instructions"),
    # The M365 worker runs as a script with flat imports, so ship its sources as files.
    (str(root / "app" / "m365_worker"), "app/m365_worker"),
]
binaries = []
# m365_worker ships as source files; codegraph's CLI/MCP server are not used by the app
# (the MCP server would pull mcp + pydantic + cryptography into the bundle).
SKIP = (".m365_worker", "app.codegraph.mcp_server", "app.codegraph.cli")
hiddenimports = [m for m in collect_submodules("app") if not any(x in m for x in SKIP)]
hiddenimports += ["tkinter", "tkinter.filedialog", "uuid", "tempfile", "atexit", "traceback", "winreg"]

pw_datas, pw_binaries, pw_hidden = collect_all("playwright")  # includes the Node driver, not the browsers
datas += pw_datas
binaries += pw_binaries
hiddenimports += pw_hidden

try:  # bundle uv/uvx so mcp-atlassian runs without installing anything
    import uv
    uv_bin = Path(uv.find_uv_bin())
    for exe in (uv_bin, uv_bin.with_name("uvx" + uv_bin.suffix)):
        if exe.exists():
            binaries.append((str(exe), "bin"))
except Exception as exc:  # the app falls back to a uvx found on PATH
    print("WARNING: uv not bundled:", exc)

a = Analysis(
    [str(root / "app" / "__main__.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["matplotlib", "numpy", "pandas", "scipy", "PIL", "pytest", "IPython", "mcp", "pydantic",
              "pydantic_core", "jsonschema", "jsonschema_specifications", "rpds", "email_validator", "cryptography",
              "yaml", "click", "websockets", "setuptools", "starlette", "uvicorn", "fastapi", "httpx", "anyio"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TicketAnalyzer",
    console=True,  # shows the URL and lets the user stop the server with Ctrl+C
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="TicketAnalyzer")
