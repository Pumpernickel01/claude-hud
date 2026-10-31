#!/usr/bin/env bash
# Model tests (node) and a server smoke test on a temporary log.
set -euo pipefail
cd "$(dirname "$0")"
node --test model.test.mjs
TMP=$(mktemp -d); LOG="$TMP/events.jsonl"
printf '{"ts":"2026-10-30T10:00:00.000Z","event":"session_start","status":"running","session_id":"t1","cwd":"/x"}\n' > "$LOG"
PORT=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])')
cat > "$TMP/claude" <<'STUB'
#!/bin/sh
# a stand-in for `claude -p`: echoes the prompt back as the result, slowly enough to overlap a second ask
sleep 0.6; printf '{"type":"result","result":"echo: %s","session_id":"s","total_cost_usd":0.01,"duration_ms":5,"is_error":false}' "$(cat)"
STUB
chmod +x "$TMP/claude"
CLAUDE_MODS_EVENT_LOG="$LOG" CLAUDE_HUD_CLAUDE="$TMP/claude" CLAUDE_HUD_TOKEN_FILE="$TMP/token" TERM_PROGRAM= __CFBundleIdentifier= python3 serve.py --port "$PORT" >/dev/null 2>&1 &
SRV=$!; trap 'kill $SRV 2>/dev/null; wait $SRV 2>/dev/null; rm -rf "$TMP"' EXIT
sleep 0.5
python3 - "$PORT" "$LOG" "$TMP/token" <<'PY'
import sys, time, urllib.request, http.client, json
port, log = int(sys.argv[1]), sys.argv[2]
page = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3)
assert page.status == 200 and b"<canvas" in page.read(), "index.html not served"
mod = urllib.request.urlopen(f"http://127.0.0.1:{port}/model.mjs", timeout=3)
assert mod.headers["Content-Type"].startswith("text/javascript"), mod.headers["Content-Type"]
try:
    urllib.request.urlopen(f"http://127.0.0.1:{port}/../serve.py", timeout=3); raise SystemExit("unexpected path served")
except urllib.error.HTTPError as e:
    assert e.code == 404, e.code
f = http.client.HTTPConnection("127.0.0.1", port, timeout=3); f.request("POST", "/focus", headers={"Origin": f"http://127.0.0.1:{port}"}); assert f.getresponse().status == 404, "focus without a known terminal must 404"
f = http.client.HTTPConnection("127.0.0.1", port, timeout=3); f.request("POST", "/focus", headers={"Origin": "http://evil.example"}); assert f.getresponse().status == 403, "foreign origin must be refused"
f = http.client.HTTPConnection("127.0.0.1", port, timeout=3); f.request("GET", "/events", headers={"Host": "hud.evil.example"}); assert f.getresponse().status == 403, "foreign Host must be refused (DNS rebinding)"
c = http.client.HTTPConnection("127.0.0.1", port, timeout=5); c.request("GET", "/events"); r = c.getresponse()
assert r.headers["Content-Type"].startswith("text/event-stream")
stream = b""  # everything read so far: records and chat events may arrive in either order within a poll
def read_until(needle):
    global stream
    t0 = time.time()
    while needle not in stream and time.time() - t0 < 4: stream += r.fp.readline()
    assert needle in stream, (needle, stream[-300:])
read_until(b'"session_start"'); read_until(b"event: synced")
with open(log, "a") as f: f.write(json.dumps({"ts": "2026-10-30T10:00:01.000Z", "event": "tool_end", "status": "done", "session_id": "t1", "tool": "Bash", "duration_ms": 5}) + "\n")
read_until(b'"tool_end"')
# Jarvis: an ask is echoed to every open stream as a chat event; a second ask while one runs is refused
token = open(sys.argv[3]).read().strip()
assert token in urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3).read().decode(), "the page carries this run's token"
def post(path, body, origin=f"http://127.0.0.1:{port}", tok=token):
    k = http.client.HTTPConnection("127.0.0.1", port, timeout=5); k.request("POST", path, body=json.dumps(body), headers={"Content-Type": "application/json", "Origin": origin, "X-HUD-Token": tok}); return k.getresponse().status
assert post("/ask", {"text": "ping"}, tok="wrong") == 403, "no token, no ask"
assert post("/ask", {"text": "ping"}) == 202
assert post("/ask", {"text": "again"}) == 409, "a second ask must wait"
assert post("/ask", {"text": "   "}) == 400
assert post("/state", {"name": "listening"}) == 204 and post("/state", {"name": "bogus"}) == 400
assert post("/state", {"name": "listening", "text": "what time"}) == 204  # words heard so far: a hearing event, not a log record
j = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/jarvis", timeout=3).read()); assert j["state"] == "listening" and "busy" in j, j
read_until(b'event: chat'); read_until(b'"kind": "ask"')
read_until(b'"state":"listening"'); read_until(b'"kind": "hearing", "text": "what time", "phase": "listening"'); read_until(b'echo: ping')
assert post("/cancel", {}) == 200, "nothing to cancel: 200"
assert post("/ask", {"text": "slow one"}) == 202
time.sleep(0.15); assert post("/cancel", {}) == 204, "a running ask is cancelled: 204"
read_until(b'"kind": "error", "text": "cancelled"')
assert stream.count(b'"state":"listening"') == 1, "a live transcript must not log a second state record"
print("server smoke test ok")
PY
# dock geometry: a right column, a left column, an explicit box
python3 - <<'PY'
import sys; sys.path.insert(0, '.')
from dock import geometry
assert geometry(2000, 1000, 'right', 0.4) == (1200, 25, 800, 975), geometry(2000, 1000, 'right', 0.4)
assert geometry(2000, 1000, 'left', 0.25) == (0, 25, 500, 975)
assert geometry(2000, 1000, '10,20,300,400') == (10, 20, 300, 400)
print("dock geometry ok")
PY
# listener: built once, the wake handler exercised with text (no microphone); skipped without swiftc
if command -v swiftc >/dev/null; then
  BIN=$(CLAUDE_HUD_APP="$TMP/Listener.app" python3 -c 'import dock; print(dock.build_listener())' | tail -1)
  codesign --verify "$TMP/Listener.app" && echo "listener app bundle signed ok"
  OUT=$(CLAUDE_HUD_TOKEN_FILE="$TMP/token" CLAUDE_HUD_LOG="$TMP/listen.log" "$BIN" --text "Hey Claude, what time is it" --dry-run 2>&1)
  echo "$OUT" | grep -q 'ask: what time is it' && echo "$OUT" | grep -q '/state' && echo "listener wake handler ok" || { echo "listener: $OUT"; exit 1; }
  OUT=$(CLAUDE_HUD_TOKEN_FILE="$TMP/token" CLAUDE_HUD_LOG="$TMP/listen.log" "$BIN" --text "Hey Claude, open the thing, never mind" --dry-run 2>&1)
  echo "$OUT" | grep -q 'cancelled by voice' && ! echo "$OUT" | grep -q '/ask' && echo "listener voice cancel ok" || { echo "listener cancel: $OUT"; exit 1; }
fi
# the launchd agent plist, without touching launchd
python3 - <<'PY'
import sys, plistlib; sys.path.insert(0, '.'); import dock
d = plistlib.loads(dock.agent_plist('/x/Listener.app', '/x/a b.log').encode())
assert d['ProgramArguments'] == ['/usr/bin/open', '-W', '-g', '-a', '/x/Listener.app'] and d['KeepAlive'] is True and d['StandardErrorPath'] == '/x/a b.log'
print("agent plist ok")
PY
# launcher: a listening port means no second server and no browser; --no-open never starts a listener
CLAUDE_MODS_HUD_PORT="$PORT" BROWSER=/usr/bin/false python3 launch.py
CLAUDE_MODS_HUD_PORT="$PORT" CLAUDE_HUD_LISTEN=1 python3 launch.py --no-open
pgrep -f "dock.py listen" >/dev/null && { echo "launch.py --no-open must not start a listener"; exit 1; } || echo "no listener from --no-open ok"
[ "$(pgrep -f "serve.py --port $PORT" | wc -l | tr -d ' ')" = "1" ] && echo "launcher skips a running server ok"
