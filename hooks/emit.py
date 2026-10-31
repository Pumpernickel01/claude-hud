#!/usr/bin/env python3
"""Agent-monitor event hooks (shared with claude-mods/monitor/hooks/emit.py; keep them in step): `python3 emit.py <kind>` reads the hook stdin JSON and
appends one record to ~/.claude/agent-monitor/events.jsonl through lib/events.py.

kinds (see REPORT.md for the hooks.json wiring):
  session_start  SessionStart            agent_start  PreToolUse Agent + SubagentStart
  tool_end       PostToolUse *           tool_fail    PostToolUseFailure *
  agent_stop     SubagentStop (sums the subagent transcript's token usage)
  session_stop   Stop                    session_end  SessionEnd
  prompt         UserPromptSubmit (first 120 chars)
  notification   Notification (its notification_type, no message text)
tool_end carries `target` (a file path, host or MCP server name) and `target_kind`
(file | host | mcp) derived from the tool input; the input itself is never logged.
Never blocks, never prints: any failure exits 0 quietly.
"""
import json
import os
import re
import sys
import time
from urllib.parse import urlsplit

if "monitor" in os.environ.get("CLAUDE_MODS_DISABLE", "").split(","):
    sys.exit(0)


def mods_monitor_installed():
    """True when the `mods` plugin (claude-mods) is installed: its monitor writes the same log, so this
    plugin's hooks must stay quiet or every event would be recorded twice."""
    try:
        with open(os.path.expanduser("~/.claude/plugins/installed_plugins.json"), encoding="utf-8") as f:
            return "mods@claude-mods" in json.load(f).get("plugins", {})
    except (OSError, ValueError, AttributeError):
        return False


if not os.environ.get("CLAUDE_HUD_FORCE") and mods_monitor_installed() and os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) != "claude-mods":
    sys.exit(0)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("CLAUDE_PLUGIN_ROOT") or os.path.dirname(HERE)  # hooks/ sits at the repo root here
sys.path.insert(0, os.path.join(ROOT, "lib"))
from events import emit  # noqa: E402

FILE_TOOLS = {"Read": "file_path", "Edit": "file_path", "Write": "file_path", "NotebookEdit": "notebook_path",
              "Glob": "path", "Grep": "path"}
# hosts a shell command reaches: the URL_HOST / SCP_HOST / NET rules of activity-pane/hooks/flows.ts
URL_HOST = re.compile(r"\b(?:https?|wss?|ftp|mqtts?|coap|rtsp|tcp|udp)://(?:[^\s/@'\"]+@)?(\[[^\]]+\]|[^\s/:'\"?#]+)", re.I)
SCP_HOST = re.compile(r"\b[\w.-]+@([\w.-]+):")
NET = {"ssh", "sftp", "mosh", "telnet", "nc", "ncat", "netcat", "socat", "ping", "ping6", "traceroute", "mtr",
       "nmap", "dig", "nslookup", "host", "ftp", "curl", "wget", "http", "https", "xh", "scp", "rsync"}
PREFIX = {"sudo", "time", "exec", "command", "nohup", "env"}


def bash_hosts(command):
    """Hosts a shell command reaches, in order, once each. Never returns the command itself."""
    found = []

    def add(h):
        if h and h not in found:
            found.append(h)
    for segment in re.split(r"\s*(?:&&|\|\||[;|\n])\s*", command or ""):
        tokens = [t.strip("'\"") for t in segment.strip().split()]
        while tokens and (tokens[0] in PREFIX or re.match(r"^\w+=", tokens[0])):
            tokens.pop(0)
        program = (tokens[0] if tokens else "").split("/")[-1]
        before = len(found)
        for m in URL_HOST.finditer(segment):
            add(m.group(1).strip("[]"))
        if program in NET or program == "git":
            for m in SCP_HOST.finditer(segment):
                add(m.group(1))
        if program in NET and len(found) == before:
            for t in tokens[1:]:
                t = re.sub(r"^[^@\s]+@", "", t)
                # shortcut: a bare argument counts only with a dot in it (host.example, an IP); prose inside
                # heredocs otherwise reads "ping the server" as a host. Widen when a dotless host matters.
                if not t.startswith("-") and re.match(r"^[\w-]+(\.[\w-]+)+$", t):
                    add(t)
                    break
    return found


def target_of(tool, ti):
    """(target, kind) a tool call reaches: a file path, a host or an MCP server; (None, None) otherwise."""
    if not isinstance(ti, dict):
        return None, None
    if tool in FILE_TOOLS:
        v = ti.get(FILE_TOOLS[tool])
        return (v, "file") if isinstance(v, str) and v else (None, None)
    if tool == "WebFetch" and isinstance(ti.get("url"), str):
        try:
            host = urlsplit(ti["url"]).hostname  # the parser, not a regex: user@host tricks
        except ValueError:
            host = None
        return (host, "host") if host else (None, None)
    if tool == "Bash":
        hosts = bash_hosts(ti.get("command") if isinstance(ti.get("command"), str) else "")
        return (hosts[0], "host") if hosts else (None, None)  # shortcut: first host only; list them when the HUD draws several
    if isinstance(tool, str) and tool.startswith("mcp__"):
        server = tool.split("__")[1] if tool.count("__") >= 2 else None
        server = re.sub(r"^plugin_", "", server or "")
        return (re.sub(r"^(.+)_\1$", r"\1", server), "mcp") if server else (None, None)  # plugin_x_x -> x
    return None, None


USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def transcript_tokens(path, budget=2.0):
    """Sum usage over a subagent transcript, one usage per message id (the file repeats
    a message once per content block). Stops after `budget` seconds."""
    if not path or not os.path.isfile(path):
        return None
    deadline = time.monotonic() + budget
    seen = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if time.monotonic() > deadline:
                    break
                if '"usage"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("type") != "assistant":
                    continue
                msg = rec.get("message") or {}
                if isinstance(msg.get("usage"), dict):
                    seen[msg.get("id") or i] = msg["usage"]
    except OSError:
        return None
    if not seen:
        return None
    return {k: sum(int(u.get(k) or 0) for u in seen.values()) for k in USAGE_KEYS}


def main():
    kind = sys.argv[1] if len(sys.argv) > 1 else ""
    raw = "" if sys.stdin.isatty() else sys.stdin.read()
    if not raw.strip():
        return
    data = json.loads(raw)
    if kind == "session_start":
        emit("session_start", "running", data, source=data.get("source"))
    elif kind == "agent_start":
        if data.get("hook_event_name") == "PreToolUse":
            ti = data.get("tool_input") or {}
            # stdin agent_id/agent_type here belong to the spawner: record them as the parent
            parent = dict(data)
            parent.pop("agent_id", None)
            parent.pop("agent_type", None)
            emit("agent_start", "running", parent, parent_agent_id=data.get("agent_id"),
                 agent_type=ti.get("subagent_type") or "general-purpose", description=ti.get("description"),
                 task=(ti.get("prompt") or "")[:120] or None, tool_use_id=data.get("tool_use_id"),
                 name=ti.get("name"))  # what SendMessage addresses the agent by, when it has one
        else:
            emit("agent_start", "running", data)
    elif kind in ("tool_end", "tool_fail"):
        tool = data.get("tool_name")
        to = (data.get("tool_input") or {}).get("to") if tool == "SendMessage" else None
        target, target_kind = target_of(tool, data.get("tool_input"))
        emit("tool_end", "failed" if kind == "tool_fail" else "done", data, tool=tool,
             duration_ms=data.get("duration_ms"), tool_use_id=data.get("tool_use_id"), to=to,
             target=target, target_kind=target_kind)
    elif kind == "agent_stop":
        path = data.get("agent_transcript_path")
        emit("agent_stop", "done", data, agent_transcript_path=path, tokens=transcript_tokens(path))
    elif kind == "session_stop":
        emit("session_stop", "done", data)
    elif kind == "session_end":
        emit("session_end", "done", data, reason=data.get("reason"))
    elif kind == "prompt":
        emit("prompt", "done", data, text=(data.get("prompt") or "")[:120] or None)
    elif kind == "notification":
        emit("notification", "done", data, kind=data.get("notification_type"))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
