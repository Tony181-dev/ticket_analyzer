"""MCP configuration discovery + a minimal stdio JSON-RPC client.

Config search order (first hit wins, then it is copied into data/config/mcp.json
so the app no longer depends on VS Code):
  1. data/config/mcp.json            (this app)
  2. VS Code user mcp.json           (%APPDATA%/Code/User/mcp.json, Insiders too)
  3. ~/.copilot/mcp-config.json      (Copilot CLI)
Two roles are needed: "jira" and "collab". Both are served by mcp-atlassian.
"""
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import paths

ROLE_NAMES = {"jira": "mcp_jira", "collab": "mcp_collab"}
ROLE_ENV = {
    "jira": ("JIRA_URL", "JIRA_PERSONAL_TOKEN"),
    "collab": ("CONFLUENCE_URL", "CONFLUENCE_PERSONAL_TOKEN"),
}
DEFAULT_URLS = {"jira": "http://jira.lge.com/issue/", "collab": "http://collab.lge.com/main/"}
REQUIRED_TOOLS = {"jira": ["jira_get_issue"], "collab": ["confluence_create_page", "confluence_update_page"]}


def _candidate_files():
    appdata = os.getenv("APPDATA")
    home = Path.home()
    files = []
    if appdata:
        files += [Path(appdata) / "Code" / "User" / "mcp.json",
                  Path(appdata) / "Code - Insiders" / "User" / "mcp.json"]
    files += [home / ".config" / "Code" / "User" / "mcp.json",
              home / "Library" / "Application Support" / "Code" / "User" / "mcp.json",
              home / ".copilot" / "mcp-config.json"]
    return files


def _read_servers(file):
    try:
        data = json.loads(Path(file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data.get("servers") or data.get("mcpServers") or {}


def _usable(entry, role):
    """An entry is usable when it is a stdio server whose URL/token are real values."""
    if not isinstance(entry, dict) or not entry.get("command"):
        return False
    env = entry.get("env") or {}
    url_key, token_key = ROLE_ENV[role]
    values = [str(env.get(url_key, "")), str(env.get(token_key, ""))]
    # VS Code ${input:...} placeholders cannot be resolved outside VS Code.
    return all(v.strip() and "${" not in v for v in values)


def _pick(servers, role):
    named = servers.get(ROLE_NAMES[role])
    if _usable(named, role):
        return named
    for entry in servers.values():
        args = " ".join(map(str, (entry or {}).get("args") or []))
        if "mcp-atlassian" in args + str((entry or {}).get("command", "")) and _usable(entry, role):
            return entry
    return None


def load():
    """Return {"jira": entry|None, "collab": entry|None, "source": path|None}."""
    own = _read_servers(paths.MCP_FILE)
    found = {role: _pick(own, role) for role in ROLE_NAMES}
    if all(found.values()):
        return {**found, "source": str(paths.MCP_FILE)}
    for file in _candidate_files():
        servers = _read_servers(file)
        for role in ROLE_NAMES:
            found[role] = found[role] or _pick(servers, role)
        if all(found.values()):
            _write(found)  # import once so the app is standalone from now on
            return {**found, "source": str(file)}
    return {**found, "source": None}


def _write(found):
    paths.MCP_FILE.parent.mkdir(parents=True, exist_ok=True)
    servers = {ROLE_NAMES[r]: e for r, e in found.items() if r in ROLE_NAMES and e}
    paths.MCP_FILE.write_text(json.dumps({"servers": servers}, indent=2), encoding="utf-8")


def save_tokens(form):
    """Build mcp-atlassian entries from the 2-field form (+ optional URLs)."""
    current = {role: _pick(_read_servers(paths.MCP_FILE), role) for role in ROLE_NAMES}
    for role in ROLE_NAMES:
        url_key, token_key = ROLE_ENV[role]
        token = str(form.get(f"{role}_token", "")).strip()
        url = str(form.get(f"{role}_url", "")).strip() or DEFAULT_URLS[role]
        if not token:
            continue
        env = {url_key: url, token_key: token}
        current[role] = {"type": "stdio", "command": "uvx", "args": ["mcp-atlassian"], "env": env}
    _write(current)
    return load()


def status():
    cfg = load()
    out = {"source": cfg["source"]}
    for role in ROLE_NAMES:
        entry = cfg[role]
        out[role] = {
            "configured": bool(entry),
            "url": ((entry or {}).get("env") or {}).get(ROLE_ENV[role][0], DEFAULT_URLS[role]),
        }
    return out


# ---------------------------------------------------------------------------
# stdio client
# ---------------------------------------------------------------------------
class McpError(RuntimeError):
    pass


def _resolve_command(command):
    found = shutil.which(command)
    if found:
        return found
    for suffix in ("", ".exe", ".cmd"):
        local = paths.SCRIPTS / (command + suffix)
        if local.exists():
            return str(local)
    raise McpError(f"MCP command not found: {command}. Run Setup.")


def child_env(extra=None):
    env = {**os.environ, **(extra or {})}
    env["PATH"] = str(paths.SCRIPTS) + os.pathsep + env.get("PATH", "")
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    # Corporate proxies re-sign TLS; uv must trust the OS certificate store.
    env.setdefault("UV_NATIVE_TLS", "1")
    env.setdefault("NODE_USE_SYSTEM_CA", "1")  # node/npm/copilot (Node >= 22.15)
    return env


class _Session:
    def __init__(self, entry, timeout):
        self.timeout = timeout
        self.proc = subprocess.Popen(
            [_resolve_command(entry["command"]), *map(str, entry.get("args") or [])],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=entry.get("cwd") or None, env=child_env(entry.get("env")),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.lines = []
        self.cond = threading.Condition()
        self.stderr_tail = []
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self):
        for raw in self.proc.stdout:
            with self.cond:
                self.lines.append(raw.decode("utf-8", "replace"))
                self.cond.notify_all()
        with self.cond:
            self.cond.notify_all()

    def _read_stderr(self):
        for raw in self.proc.stderr:
            self.stderr_tail = (self.stderr_tail + [raw.decode("utf-8", "replace").rstrip()])[-20:]

    def send(self, message):
        self.proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def wait_for(self, msg_id):
        deadline = time.time() + self.timeout
        with self.cond:
            while True:
                while self.lines:
                    line = self.lines.pop(0).strip()
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if msg.get("id") == msg_id:
                        if msg.get("error"):
                            raise McpError(json.dumps(msg["error"], ensure_ascii=False))
                        return msg.get("result")
                if self.proc.poll() is not None:
                    raise McpError("MCP server exited: " + " | ".join(self.stderr_tail[-5:]))
                left = deadline - time.time()
                if left <= 0:
                    raise McpError(f"MCP timeout after {self.timeout}s")
                self.cond.wait(min(left, 0.5))

    def close(self):
        try:
            self.proc.kill()
        except OSError:
            pass


def _open(role, timeout):
    entry = load()[role]
    if not entry:
        raise McpError(f"MCP {role} is not configured. Open Setup and enter the {role} token.")
    session = _Session(entry, timeout)
    session.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "ticket-analyzer", "version": "2"}}})
    session.wait_for(1)
    session.send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    return session


def call(role, tool, arguments, timeout=120):
    session = _open(role, timeout)
    try:
        session.send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": tool, "arguments": arguments or {}}})
        return session.wait_for(2)
    finally:
        session.close()


def list_tools(role, timeout=180):
    session = _open(role, timeout)
    try:
        session.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        return [t.get("name") for t in (session.wait_for(2) or {}).get("tools", [])]
    finally:
        session.close()


def result_text(result):
    return "\n".join(str(x.get("text", "")) for x in (result or {}).get("content", []) if x.get("type") == "text")
