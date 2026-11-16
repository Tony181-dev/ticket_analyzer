"""GitHub Copilot CLI provider (`copilot -p ... --output-format json`).

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
from . import stopflag

PACKAGE = "@github/copilot"
# One process slot per caller: the pipeline and the assistant chat each run one CLI process at a time,
# side by side, and stopping one never kills the other.
_slots_lock = threading.Lock()
_slots = {}
_USAGE_FIELDS = {
    "input_tokens": "inputTokens",
    "output_tokens": "outputTokens",
    "cache_read_tokens": "cacheReadTokens",
    "cache_write_tokens": "cacheWriteTokens",
    "reasoning_tokens": "reasoningTokens",
}


def _count(value):
    try:
        return max(0, int(value)) if not isinstance(value, bool) else 0
    except (TypeError, ValueError, OverflowError):
        return 0


def _parse_output(text):
    """Extract the final text and per-model usage events from Copilot JSONL output."""
    messages = []
    final_messages = []
    usage = {key: 0 for key in _USAGE_FIELDS}
    usage["model_calls"] = 0
    saw_event = False
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        if event_type == "assistant.message":
            saw_event = True
            content = data.get("content")
            if isinstance(content, str) and content.strip():
                messages.append(content.strip())
                if not data.get("toolRequests") and not data.get("tool_requests"):
                    final_messages.append(content.strip())
        elif event_type == "assistant.usage":
            saw_event = True
            usage["model_calls"] += 1
            for key, source in _USAGE_FIELDS.items():
                usage[key] += _count(data.get(source))

    if not saw_event:
        return text.strip(), None
    return "\n\n".join(final_messages or messages).strip(), usage


def _notify_usage(on_usage, usage):
    if not on_usage or not usage or not usage.get("model_calls"):
        return
    try:
        on_usage(usage)
    except Exception:
        pass  # Accounting must never make an otherwise successful AI call fail.


def _slot(name):
    with _slots_lock:
        return _slots.setdefault(name, {"lock": threading.Lock(), "proc": None})


def _execute(args, cwd, timeout, cancellable, slot="pipeline"):
    state = _slot(slot)
    with state["lock"]:
        if cancellable and stopflag.requested.is_set():
            raise CopilotError("Cancelled")
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                cwd=str(cwd), env=child_env({"NO_COLOR": "1"}),
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        state["proc"] = proc
        if cancellable and stopflag.requested.is_set():  # Stop landed between the check and the launch
            cancel(slot)
        timed_out = False
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            timed_out = True
            out, err = exc.output or b"", exc.stderr or b""
        finally:
            state["proc"] = None
    return proc, out, err, timed_out


def _unsupported(detail, option):
    message = str(detail or "").lower()
    return option in message and any(
        text in message for text in ("unknown option", "unrecognized option", "unknown argument"))


def _unsupported_json_output(detail):
    return _unsupported(detail, "--output-format")


def _without(args, option):
    """`args` minus every `option VALUE` pair."""
    out, skip = [], False
    for arg in args:
        if skip:
            skip = False
        elif arg == option:
            skip = True
        else:
            out.append(arg)
    return out


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


def ask(prompt, cwd, model="", timeout=900, cancellable=False, effort=None, context=None, on_usage=None,
        extra_dirs=(), slot="pipeline"):
    """Run one read-only prompt. `extra_dirs` are readable besides `cwd` (`--add-dir`); `slot` names the
    process slot (see _slots) so independent callers do not queue behind each other."""
    cmd = command()
    if not cmd:
        raise CopilotError("Copilot CLI is not installed. Open Setup.")
    args = [*cmd, "-p", prompt, "--output-format", "json", "-s", "--no-auto-update", "--stream", "off",
            "--allow-all-tools", "--deny-tool", "shell", "--deny-tool", "write",
            "--disable-builtin-mcps", "-C", str(cwd)]
    for folder in dict.fromkeys(str(d) for d in extra_dirs or () if d):
        args += ["--add-dir", folder]
    if model:
        args += ["--model", model]
    if context:
        args += ["--context", context]
    if effort:
        args += ["--effort", effort]
    for _ in range(3):  # an older CLI may lack an optional flag: drop it and try again
        proc, out, err, timed_out = _execute(args, cwd, timeout, cancellable, slot)
        raw = (out or b"").decode("utf-8", "replace")
        text, usage = _parse_output(raw)
        detail = err.decode("utf-8", "replace").strip() or text or raw.strip()
        if timed_out or proc.returncode == 0:
            break
        option = next((o for o in ("--output-format", "--add-dir") if o in args and _unsupported(detail, o)), None)
        if not option:
            break
        args = _without(args, option)
    _notify_usage(on_usage, usage)
    if timed_out:
        raise CopilotError(f"Copilot CLI timed out after {timeout}s")
    if proc.returncode != 0:
        detail = (err.decode("utf-8", "replace").strip() or text or raw.strip())[-1500:]
        if not detail:
            detail = "no output (cancelled?)"
        if any(k in detail.lower() for k in ("/login", "authenticat", "not logged", "unauthorized")):
            raise CopilotError("Copilot CLI is not signed in (Setup → Copilot sign-in). " + detail)
        raise CopilotError(f"Copilot CLI failed (exit {proc.returncode}): {detail}")
    if not text:
        raise CopilotError("Copilot CLI returned an empty response")
    return text


def cancel(slot="pipeline"):
    proc = _slot(slot).get("proc")
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
