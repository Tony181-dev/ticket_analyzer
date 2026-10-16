"""GitHub Copilot CLI provider (`copilot -p ... -s`).

Material is never inlined into the command line (Windows caps it at ~32k
chars): files live in the run folder and the prompt tells the agent which ones
to read. Shell and write tools are denied, so the agent can only read.
The CLI is launched as `node <pkg>/npm-loader.js` so the prompt never passes
through cmd.exe quoting.
"""
import json
import shutil
import subprocess
import threading
from pathlib import Path

from ..mcp import child_env

PACKAGE = "@github/copilot"
_lock = threading.Lock()
_current = {"proc": None}


class CopilotError(RuntimeError):
    pass


def _npm_root():
    npm = shutil.which("npm")
    if not npm:
        return None
    try:
        out = subprocess.run([npm, "root", "-g"], capture_output=True, text=True, timeout=30,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None
    except (OSError, subprocess.SubprocessError):
        return None


def command():
    """argv prefix that launches the CLI, or None when it is not installed.

    Prefers the npm package (node + npm-loader.js, no cmd.exe quoting); falls
    back to any `copilot` executable on PATH (winget / standalone installs).
    """
    node = shutil.which("node")
    root = _npm_root()
    if node and root:
        pkg = root / "@github" / "copilot"
        try:
            bin_rel = json.loads((pkg / "package.json").read_text(encoding="utf-8"))["bin"]["copilot"]
            if (pkg / bin_rel).exists():
                return [node, str(pkg / bin_rel)]
        except (OSError, ValueError, KeyError, TypeError):
            pass
    exe = shutil.which("copilot")
    return [exe] if exe else None


def version():
    cmd = command()
    if not cmd:
        return None
    try:
        out = subprocess.run([*cmd, "--version"], capture_output=True, text=True, timeout=60,
                             env=child_env(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return (out.stdout or out.stderr).strip().splitlines()[0] if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


def ask(prompt, cwd, model="", timeout=900):
    cmd = command()
    if not cmd:
        raise CopilotError("Copilot CLI is not installed. Open Setup.")
    args = [*cmd, "-p", prompt, "-s", "--no-auto-update", "--stream", "off",
            "--allow-all-tools", "--deny-tool", "shell", "--deny-tool", "write",
            "--disable-builtin-mcps", "-C", str(cwd)]
    if model:
        args += ["--model", model]
    with _lock:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                cwd=str(cwd), env=child_env({"NO_COLOR": "1"}),
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        _current["proc"] = proc
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise CopilotError(f"Copilot CLI timed out after {timeout}s")
        finally:
            _current["proc"] = None
    text = out.decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        detail = (err.decode("utf-8", "replace").strip() or text)[-1500:]
        if not detail:
            detail = "no output (cancelled?)"
        if any(k in detail.lower() for k in ("/login", "authenticat", "not logged", "unauthorized")):
            raise CopilotError("Copilot CLI is not signed in (Setup → Copilot sign-in). " + detail)
        raise CopilotError(f"Copilot CLI failed (exit {proc.returncode}): {detail}")
    if not text:
        raise CopilotError("Copilot CLI returned an empty response")
    return text


def cancel():
    proc = _current.get("proc")
    if proc and proc.poll() is None:
        try:
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                subprocess.run(["taskkill", "/pid", str(proc.pid), "/T", "/F"], capture_output=True)
            else:
                proc.kill()
        except OSError:
            pass


def probe():
    """Smallest real request: proves install + login + network. Raises CopilotError."""
    import tempfile
    with tempfile.TemporaryDirectory(prefix="ta-copilot-") as tmp:
        return ask("Reply with exactly the word OK and nothing else.", tmp, timeout=180)


def open_login_terminal():
    """Open a visible terminal running `copilot login` (OAuth browser flow); returns the process."""
    cmd = command()
    if not cmd:
        raise CopilotError("Copilot CLI is not installed")
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    return subprocess.Popen([*cmd, "login"], creationflags=flags, env=child_env())
