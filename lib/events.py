"""Shared append-only event log for the agent monitor. Owned by Agent 22.

    sys.path.insert(0, f"{ROOT}/lib"); from events import emit, LOG_PATH
    emit(event="tool_start", status="running", tool="Bash", hook_input=stdin_json)

Writes one JSON object per line to ~/.claude/agent-monitor/events.jsonl
(override with CLAUDE_MODS_EVENT_LOG). Directory 0700, file 0600. Never raises:
a logging failure must not break a hook.
"""
import fcntl
import json
import os
import sys
import time

LOG_PATH = os.environ.get(
    "CLAUDE_MODS_EVENT_LOG",
    os.path.join(os.path.expanduser("~"), ".claude", "agent-monitor", "events.jsonl"),
)

FIELDS = ("session_id", "agent_id", "agent_type", "cwd", "transcript_path")
ROTATE_BYTES = 20 * 1024 * 1024


def rotate():
    """Moves the log to .1 once: hooks run in parallel, and a second writer that saw the
    same big file would otherwise rename the fresh log over the one just rotated."""
    lock = os.open(LOG_PATH + ".lock", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return  # another writer is rotating; a hook must never wait on a lock
        # re-checked under the lock: after another writer's rotation the log may not exist yet
        if os.path.isfile(LOG_PATH) and os.path.getsize(LOG_PATH) > ROTATE_BYTES:
            os.replace(LOG_PATH, LOG_PATH + ".1")  # readers see the file shrink and start over at 0
    finally:
        os.close(lock)


def emit(event, status="running", hook_input=None, **extra):
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z",
        "event": event,
        "status": status,
    }
    hook_input = hook_input or {}
    for k in FIELDS:
        rec[k] = hook_input.get(k)
    rec.update({k: v for k, v in extra.items() if v is not None})
    try:
        d = os.path.dirname(LOG_PATH)
        os.makedirs(d, mode=0o700, exist_ok=True)
        if os.path.isfile(LOG_PATH) and os.path.getsize(LOG_PATH) > ROTATE_BYTES:
            rotate()
        fd = os.open(LOG_PATH, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception:
        pass
    return rec


if __name__ == "__main__":
    # usage: events.py <event> <status> [json-extra] ; hook stdin JSON is merged if present
    data = {}
    if not sys.stdin.isatty():
        try:
            data = json.loads(sys.stdin.read() or "{}")
        except Exception:
            data = {}
    extra = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
    emit(sys.argv[1] if len(sys.argv) > 1 else "event",
         sys.argv[2] if len(sys.argv) > 2 else "running",
         hook_input=data, **extra)
