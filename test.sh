#!/usr/bin/env bash
# All tests: the hooks' records on a temporary log, then the HUD's own suite.
set -euo pipefail
cd "$(dirname "$0")"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
export CLAUDE_MODS_EVENT_LOG="$TMP/events.jsonl" CLAUDE_HUD_FORCE=1  # record even where claude-mods is installed
B='"session_id":"t1","transcript_path":"/tmp/x.jsonl","cwd":"/tmp/proj"'
printf '%s' "{$B,\"hook_event_name\":\"PostToolUse\",\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"curl https://api.github.com/x && echo SECRET\"},\"tool_use_id\":\"t1\"}" | python3 hooks/emit.py tool_end
printf '%s' "{$B,\"hook_event_name\":\"UserPromptSubmit\",\"prompt\":\"hello hud\"}" | python3 hooks/emit.py prompt
python3 - "$CLAUDE_MODS_EVENT_LOG" <<'PY'
import json, sys
recs = [json.loads(l) for l in open(sys.argv[1])]
assert recs[0]["target"] == "api.github.com" and "SECRET" not in open(sys.argv[1]).read(), recs[0]
assert recs[1]["event"] == "prompt" and recs[1]["text"] == "hello hud", recs[1]
print("hook records ok")
PY
bash hud/test.sh
