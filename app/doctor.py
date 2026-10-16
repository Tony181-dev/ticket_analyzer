"""Setup Doctor: check everything the selected AI mode needs and fix only what
is really missing. Nothing that is already present is reinstalled.

Each check -> {id, label, ok, detail, fix?, action?, needed}
  fix    : automatic repair command id (run with run_fix)
  action : user action the UI renders as a button/form ("tokens", "m365_login", "copilot_login")

Accuracy rules:
  * PATH is re-read from the Windows registry before every check, so tools the
    user installs while the app is running (gh, Node, Copilot CLI) are seen at once.
  * Tools are also looked up in their usual install folders, not only on PATH.
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import events, mcp, paths, settings
from .ai import copilot
from .ai.m365 import worker as m365_worker
from .i18n import t
from .runtime import tool_cmd

_fix_lock = threading.Lock()
_cache = {"m365": None, "mcp_test": {}}
COPILOT_AUTH_FILE = paths.CONFIG / "copilot_auth.json"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run_quiet(args, timeout=30):
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW,
                             env=mcp.child_env(), cwd=str(paths.ROOT), encoding="utf-8", errors="replace")
        return out.returncode, (out.stdout or out.stderr).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)


def _which(name, *extra_dirs):
    found = shutil.which(name)
    if found:
        return found
    for d in extra_dirs:
        for suffix in ("", ".exe", ".cmd"):
            p = Path(os.path.expandvars(str(d))).expanduser() / (name + suffix)
            if p.is_file():
                return str(p)
    return None


def _node():
    exe = _which("node", r"%ProgramFiles%\nodejs", r"%LOCALAPPDATA%\Programs\nodejs")
    if not exe:
        return False, t("dr.node_missing")
    if not shutil.which("node"):  # installed but not on this process' PATH yet
        os.environ["PATH"] = str(Path(exe).parent) + os.pathsep + os.environ.get("PATH", "")
    code, out = _run_quiet([exe, "--version"])
    try:
        major = int(out.lstrip("v").split(".")[0])
    except ValueError:
        major = 0
    return code == 0 and major >= 18, out or "?"


def _chromium():
    if importlib.util.find_spec("playwright") is None:
        return False, t("dr.not_installed")
    code, out = _run_quiet(tool_cmd("chromium-path"), 60)
    out = out.strip().splitlines()[-1] if out.strip() else ""
    return bool(code == 0 and out.strip()), out.strip() or t("dr.chromium_missing")


def _uvx():
    found = _which("uvx", paths.SCRIPTS, Path(sys.base_prefix) / "Scripts", "~/.local/bin", "~/.cargo/bin",
                   r"%LOCALAPPDATA%\Programs\uv")
    if found and not shutil.which("uvx"):
        os.environ["PATH"] = str(Path(found).parent) + os.pathsep + os.environ.get("PATH", "")
    return bool(found), found or t("dr.uv_missing")


def _jsonc(path):
    """Copilot's config.json starts with // comments, so plain json.loads fails."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return {}
    text = re.sub(r"(?m)^\s*//.*$", "", text)
    try:
        return json.loads(text or "{}")
    except ValueError:
        return {}


def _copilot_login():
    """Cheap signals first; the authoritative check is verify_copilot()."""
    if any(os.getenv(k) for k in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")):
        return True, t("dr.cp_env")
    try:
        if json.loads(COPILOT_AUTH_FILE.read_text(encoding="utf-8")).get("ok"):
            return True, t("dr.cp_verified")
    except (OSError, ValueError):
        pass
    gh = _which("gh", r"%ProgramFiles%\GitHub CLI", r"%LOCALAPPDATA%\Programs\GitHub CLI")
    if gh and _run_quiet([gh, "auth", "status"], 20)[0] == 0:
        return True, t("dr.cp_gh")
    home = Path(os.getenv("COPILOT_HOME") or Path.home() / ".copilot")
    for name in ("config.json", "settings.json"):
        data = _jsonc(home / name)
        if any(data.get(k) for k in ("logged_in_users", "loggedInUsers", "last_logged_in_user", "lastLoggedInUser")):
            return True, t("dr.cp_store")
    if os.name == "nt":
        code, out = _run_quiet(["cmdkey", "/list"], 15)
        if code == 0 and "copilot" in out.lower():
            return True, t("dr.cp_store")
    return False, t("dr.cp_missing")


def verify_copilot():
    """Run a tiny prompt through Copilot CLI: the only fully reliable login test."""
    events.publish("doctor_log", line=t("dr.cp_checking"))
    try:
        copilot.probe()
        COPILOT_AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        COPILOT_AUTH_FILE.write_text(json.dumps({"ok": True, "at": time.time()}), encoding="utf-8")
        events.publish("doctor_log", line=t("dr.cp_ok"))
        return True
    except Exception as exc:
        COPILOT_AUTH_FILE.unlink(missing_ok=True)
        events.publish("doctor_log", line=t("dr.cp_fail", error=str(exc)[:400]))
        return False


def copilot_login_flow():
    """Open `copilot login` in a console; verify automatically when it closes."""
    events.publish("doctor_log", line=t("dr.cp_wait"))
    proc = copilot.open_login_terminal()
    try:
        proc.wait(timeout=900)
    except subprocess.TimeoutExpired:
        return False
    return verify_copilot()


def checks(probe_m365=False):
    _refresh_windows_path()
    cfg = settings.load()
    mode = cfg["ai_mode"]
    out = []

    def add(cid, label, ok, detail, needed=True, fix=None, action=None):
        out.append({"id": cid, "label": label, "ok": bool(ok), "detail": str(detail), "needed": needed,
                    "fix": None if ok else fix, "action": None if ok else action})

    add("python", "Python", sys.version_info >= (3, 10), sys.version.split()[0])
    ok, detail = _node()
    add("node", t("dr.node"), ok, detail, fix="node")
    ok, detail = _uvx()
    add("uv", t("dr.uv"), ok, detail, fix="uv")
    status = mcp.status()
    for role, label in (("jira", "MCP Jira"), ("collab", "MCP Collab")):
        conf = status[role]
        tested = _cache["mcp_test"].get(role)
        detail = conf["url"] + (f" · {tested}" if tested else "")
        add(f"mcp_{role}", label, conf["configured"], detail if conf["configured"] else t("dr.no_token"), action="tokens")

    m365 = mode == "m365"
    has_pw = importlib.util.find_spec("playwright") is not None
    add("playwright", t("dr.playwright"), has_pw, t("dr.installed") if has_pw else t("dr.not_installed"),
        needed=m365, fix="playwright")
    if m365 and has_pw:
        ok, detail = _chromium()
        add("chromium", t("dr.chromium"), ok, detail, needed=m365, fix="chromium")
        if probe_m365 and ok:
            _cache["m365"] = m365_worker.check()
        st = _cache["m365"] or m365_worker.status
        if st.get("state") in (None, "stopped"):
            add("m365_login", t("dr.m365_login"), False, t("dr.unchecked"), needed=m365, action="m365_login")
        else:
            add("m365_login", t("dr.m365_login"), st.get("authenticated"),
                t("dr.logged_in") if st.get("authenticated") else (st.get("error") or t("dr.need_login")),
                needed=m365, action="m365_login")

    cli = mode == "copilot"
    if cli:
        ver = copilot.version()
        add("copilot", t("dr.copilot"), bool(ver), ver or t("dr.not_installed"), needed=cli, fix="copilot")
        if ver:
            ok, detail = _copilot_login()
            add("copilot_login", t("dr.copilot_login"), ok, detail, needed=cli, action="copilot_login")
    return {"mode": mode, "ready": all(c["ok"] for c in out if c["needed"]), "checks": out}


PIP = [paths.PYTHON, "-m", "pip", "install", "--disable-pip-version-check", "--retries", "1", "--timeout", "20"]
TRUSTED = ["--trusted-host", "pypi.org", "--trusted-host", "files.pythonhosted.org"]


def _npm():
    return _which("npm", r"%ProgramFiles%\nodejs") or "npm"


# Each entry: list of alternative attempts; the first attempt that succeeds wins.
FIXES = {
    # In the packaged EXE, Playwright and uv are bundled, so there is nothing to pip-install.
    "playwright": lambda: [] if paths.FROZEN else [[PIP + ["playwright"]], [PIP + TRUSTED + ["playwright"]]],
    "chromium": lambda: [[tool_cmd("playwright", "install", "chromium")]],
    "uv": lambda: [] if paths.FROZEN else [[PIP + ["uv"]], [PIP + TRUSTED + ["uv"]]],
    "node": lambda: [[["winget", "install", "-e", "--id", "OpenJS.NodeJS.LTS", "--silent",
                       "--accept-package-agreements", "--accept-source-agreements"]]] if os.name == "nt" else [],
    "copilot": lambda: [[[_npm(), "install", "-g", "@github/copilot"]],
                        [[_npm(), "install", "-g", "--strict-ssl=false", "@github/copilot"]]],
}
# What proves a fix is unnecessary: re-checked right before installing anything.
ALREADY_OK = {
    "playwright": lambda: importlib.util.find_spec("playwright") is not None,
    "chromium": lambda: _chromium()[0],
    "uv": lambda: _uvx()[0],
    "node": lambda: _node()[0],
    "copilot": lambda: bool(copilot.version()),
}


def _stream(args):
    events.publish("doctor_log", line="$ " + " ".join(map(str, args)))
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=str(paths.ROOT),
                                env=mcp.child_env(), creationflags=NO_WINDOW)
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
    """Install one missing piece, streaming output to the UI. Never reinstalls."""
    with _fix_lock:
        _refresh_windows_path()
        if ALREADY_OK.get(fix_id, lambda: False)():
            events.publish("doctor_log", line=t("dr.fixed", id=fix_id))
            return True
        attempts = FIXES.get(fix_id, lambda: [])()
        if not attempts:
            events.publish("doctor_log", line=t("dr.cannot_fix", id=fix_id))
            return False
        for index, commands in enumerate(attempts):
            if index:
                events.publish("doctor_log", line=t("dr.retry"))
            if all(_stream(args) for args in commands):
                _refresh_windows_path()
                events.publish("doctor_log", line=t("dr.fixed", id=fix_id))
                return True
        return False


def fix_all():
    for c in checks()["checks"]:
        if c["needed"] and not c["ok"] and c["fix"]:
            run_fix(c["fix"])
    # Chromium only becomes checkable once Playwright exists.
    if settings.load()["ai_mode"] == "m365" and importlib.util.find_spec("playwright") is not None:
        run_fix("chromium")
    if mcp.status()["jira"]["configured"] and not all(v.startswith("OK") for v in _cache["mcp_test"].values() or ["x"]):
        warm_mcp()
    return checks()


def warm_mcp():
    """First uvx run downloads mcp-atlassian; do it here instead of mid-pipeline."""
    for role in ("jira", "collab"):
        events.publish("doctor_log", line=t("dr.mcp_check", role=role))
        try:
            tools = mcp.list_tools(role)
            missing = [x for x in mcp.REQUIRED_TOOLS[role] if x not in tools]
            _cache["mcp_test"][role] = t("dr.mcp_missing_tools", tools=", ".join(missing)) if missing else f"OK ({len(tools)} tools)"
        except Exception as exc:
            _cache["mcp_test"][role] = t("dr.mcp_error", error=exc)
        events.publish("doctor_log", line=f"MCP {role}: {_cache['mcp_test'][role]}")
    return dict(_cache["mcp_test"])


def _refresh_windows_path():
    """Merge the registry PATH (updated by installers) into this process, keeping order and no duplicates."""
    if os.name != "nt":
        return
    try:
        import winreg
        parts = []
        for root, key in ((winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                          (winreg.HKEY_CURRENT_USER, r"Environment")):
            try:
                with winreg.OpenKey(root, key) as k:
                    parts += os.path.expandvars(winreg.QueryValueEx(k, "Path")[0]).split(os.pathsep)
            except OSError:
                continue
    except ImportError:
        return
    seen, merged = set(), []
    for entry in os.environ.get("PATH", "").split(os.pathsep) + parts:
        norm = entry.strip().rstrip("\\/").lower()
        if norm and norm not in seen:
            seen.add(norm)
            merged.append(entry.strip())
    os.environ["PATH"] = os.pathsep.join(merged)
