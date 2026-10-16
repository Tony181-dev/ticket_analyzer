"""Setup Doctor: check everything the selected AI mode needs and fix what can
be fixed automatically. Only token entry and logins need the user.

Each check -> {id, label, ok, detail, fix?, action?, needed}
  fix    : automatic repair command id (run with run_fix)
  action : user action the UI renders as a button/form ("tokens", "m365_login", "copilot_login")
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from . import events, mcp, paths, settings
from .ai import copilot
from .ai.m365 import worker as m365_worker

_fix_lock = threading.Lock()
_cache = {"m365": None, "mcp_test": {}}


def _run_quiet(args, timeout=30):
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return out.returncode, (out.stdout or out.stderr).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)


def _node():
    exe = shutil.which("node")
    if not exe:
        return False, "Chưa cài Node.js"
    code, out = _run_quiet([exe, "--version"])
    try:
        major = int(out.lstrip("v").split(".")[0])
    except ValueError:
        major = 0
    return code == 0 and major >= 18, out or "?"


def _chromium():
    if importlib.util.find_spec("playwright") is None:
        return False, "Thiếu Playwright"
    code, out = _run_quiet([paths.PYTHON, "-c",
                            "from playwright.sync_api import sync_playwright as s;p=s().start();"
                            "import os;e=p.chromium.executable_path;print(e if os.path.exists(e) else '');p.stop()"], 60)
    return bool(code == 0 and out.strip()), out.strip() or "Chưa tải Chromium"


def _uvx():
    found = shutil.which("uvx") or next((str(paths.SCRIPTS / n) for n in ("uvx.exe", "uvx") if (paths.SCRIPTS / n).exists()), None)
    return bool(found), found or "Chưa cài uv"


def _copilot_login():
    if any(os.getenv(k) for k in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")):
        return True, "Dùng token trong biến môi trường"
    try:
        cfg = json.loads((Path.home() / ".copilot" / "config.json").read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        cfg = {}
    users = cfg.get("logged_in_users") or cfg.get("last_logged_in_user")
    return bool(users), "Đã đăng nhập" if users else "Chưa đăng nhập GitHub Copilot"


def checks(probe_m365=False):
    cfg = settings.load()
    mode = cfg["ai_mode"]
    out = []

    def add(cid, label, ok, detail, needed=True, fix=None, action=None):
        out.append({"id": cid, "label": label, "ok": bool(ok), "detail": str(detail), "needed": needed,
                    "fix": None if ok else fix, "action": None if ok else action})

    add("python", "Python", sys.version_info >= (3, 10), sys.version.split()[0])
    ok, detail = _node()
    add("node", "Node.js ≥ 18 (LogSleuth, Copilot CLI)", ok, detail, fix="node")
    ok, detail = _uvx()
    add("uv", "uv / uvx (chạy mcp-atlassian)", ok, detail, fix="uv")
    status = mcp.status()
    for role, label in (("jira", "MCP Jira"), ("collab", "MCP Collab")):
        conf = status[role]
        tested = _cache["mcp_test"].get(role)
        detail = conf["url"] + (f" · {tested}" if tested else "")
        add(f"mcp_{role}", label, conf["configured"], detail if conf["configured"] else "Chưa có token", action="tokens")

    m365 = mode == "m365"
    has_pw = importlib.util.find_spec("playwright") is not None
    add("playwright", "Playwright (điều khiển M365)", has_pw, "đã cài" if has_pw else "chưa cài", needed=m365, fix="playwright")
    if m365 and has_pw:
        ok, detail = _chromium()
        add("chromium", "Chromium cho Playwright", ok, detail, needed=m365, fix="chromium")
        if probe_m365 and ok:
            _cache["m365"] = m365_worker.check()
        st = _cache["m365"] or m365_worker.status
        if st.get("state") in (None, "stopped"):
            add("m365_login", "Đăng nhập M365 Copilot", False, "Chưa kiểm tra", needed=m365, action="m365_login")
        else:
            add("m365_login", "Đăng nhập M365 Copilot", st.get("authenticated"),
                "Đã đăng nhập" if st.get("authenticated") else (st.get("error") or "Cần đăng nhập"), needed=m365, action="m365_login")

    cli = mode == "copilot"
    if cli:
        ver = copilot.version()
        add("copilot", "GitHub Copilot CLI", bool(ver), ver or "Chưa cài", needed=cli, fix="copilot")
        if ver:
            ok, detail = _copilot_login()
            add("copilot_login", "Đăng nhập Copilot CLI", ok, detail, needed=cli, action="copilot_login")
    return {"mode": mode, "ready": all(c["ok"] for c in out if c["needed"]), "checks": out}


PIP = [paths.PYTHON, "-m", "pip", "install", "--disable-pip-version-check", "--retries", "1", "--timeout", "20"]
TRUSTED = ["--trusted-host", "pypi.org", "--trusted-host", "files.pythonhosted.org"]
# Each entry: list of alternative attempts; the first attempt that succeeds wins.
FIXES = {
    "playwright": lambda: [[PIP + ["playwright"]], [PIP + TRUSTED + ["playwright"]]],
    "chromium": lambda: [[[paths.PYTHON, "-m", "playwright", "install", "chromium"]]],
    "uv": lambda: [[PIP + ["uv"]], [PIP + TRUSTED + ["uv"]]],
    "node": lambda: [[["winget", "install", "-e", "--id", "OpenJS.NodeJS.LTS", "--silent",
                       "--accept-package-agreements", "--accept-source-agreements"]]] if os.name == "nt" else [],
    "copilot": lambda: [[[shutil.which("npm") or "npm", "install", "-g", "@github/copilot"]],
                        [[shutil.which("npm") or "npm", "install", "-g", "--strict-ssl=false", "@github/copilot"]]],
}


def _stream(args):
    events.publish("doctor_log", line="$ " + " ".join(map(str, args)))
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env=mcp.child_env(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as exc:
        events.publish("doctor_log", line=f"✗ {exc}")
        return False
    for raw in proc.stdout:
        line = raw.decode("utf-8", "replace").rstrip()
        if line:
            events.publish("doctor_log", line=line[-300:])
    if proc.wait() != 0:
        events.publish("doctor_log", line=f"✗ exit {proc.returncode}")
        return False
    return True


def run_fix(fix_id):
    """Run one repair, streaming output lines to the UI. Returns True on success."""
    attempts = FIXES.get(fix_id, lambda: [])()
    if not attempts:
        events.publish("doctor_log", line=f"Không tự cài được '{fix_id}' trên máy này — hãy cài tay.")
        return False
    with _fix_lock:
        for index, commands in enumerate(attempts):
            if index:
                events.publish("doctor_log", line="↻ Thử lại theo cách khác (mạng có thể chặn SSL)…")
            if all(_stream(args) for args in commands):
                if fix_id == "node" and os.name == "nt":
                    _refresh_windows_path()
                events.publish("doctor_log", line=f"✓ {fix_id} xong")
                return True
        return False


def fix_all():
    report = checks()
    for c in report["checks"]:
        if c["needed"] and not c["ok"] and c["fix"]:
            run_fix(c["fix"])
    if importlib.util.find_spec("playwright") is not None and settings.load()["ai_mode"] == "m365":
        if not _chromium()[0]:
            run_fix("chromium")
    if mcp.status()["jira"]["configured"]:
        warm_mcp()
    return checks()


def warm_mcp():
    """First uvx run downloads mcp-atlassian; do it here instead of mid-pipeline."""
    for role in ("jira", "collab"):
        events.publish("doctor_log", line=f"Kiểm tra kết nối MCP {role}…")
        try:
            tools = mcp.list_tools(role)
            missing = [t for t in mcp.REQUIRED_TOOLS[role] if t not in tools]
            _cache["mcp_test"][role] = ("thiếu tool " + ", ".join(missing)) if missing else f"OK ({len(tools)} tools)"
        except Exception as exc:
            _cache["mcp_test"][role] = f"lỗi: {exc}"
        events.publish("doctor_log", line=f"MCP {role}: {_cache['mcp_test'][role]}")
    return dict(_cache["mcp_test"])


def _refresh_windows_path():
    """winget updates the registry PATH; pick it up without restarting the server."""
    try:
        import winreg
        parts = []
        for root, key in ((winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                          (winreg.HKEY_CURRENT_USER, r"Environment")):
            with winreg.OpenKey(root, key) as k:
                parts.append(os.path.expandvars(winreg.QueryValueEx(k, "Path")[0]))
        os.environ["PATH"] = os.pathsep.join(parts + [os.environ.get("PATH", "")])
    except OSError:
        pass
