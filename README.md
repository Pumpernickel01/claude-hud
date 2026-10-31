# claude-hud

A live, animated HUD of your Claude Code session, in a window docked beside
your terminal (or on a second display). Think of it as the screen Jarvis would
have: the main session in the middle, every agent it spawns around it, curved
edges with dots flowing while an agent runs, a ring of the files, hosts and MCP
servers the tools reach with a dot flying to each one touched, the tool an
agent just used flashing above it, your prompt as a bubble, notifications as
toasts (click one to bring the terminal to the front), a status strip, a
timeline to scrub back through the session, and sound.

It is a Claude Code plugin: hooks record every agent start, tool call and
stop to `~/.claude/agent-monitor/events.jsonl`; a stdlib Python server
streams that file to one HTML page drawn on a canvas. No framework, no build
step, no dependencies beyond `python3` and a Chromium-based browser for the
docked window (any browser for a plain tab). macOS first; the page and server
run anywhere, the dock command is macOS.

## Install

```
/plugin marketplace add Pumpernickel01/claude-hud
/plugin install claude-hud@claude-hud
```

Restart Claude Code. The server starts with each session and the page opens
once at http://127.0.0.1:7431/. If you already use
[claude-mods](https://github.com/Pumpernickel01/claude-mods), you have all of
this already (its `hud` folder); this plugin's hooks stay quiet when they see
`mods@claude-mods` installed, so the log is never written twice.

## "Hey Claude": always listening, orb, and talking to Jarvis

`bin/hud listen --install` makes the listener a login item (a launchd agent,
restarted if it dies), so it is always on, like a phone's assistant.
`hud/listen.swift` is built once with the Swift compiler from the Command Line
Tools and uses Apple's on-device speech recognition: nothing is sent anywhere
and nothing is kept until it hears "Hey Claude". The first time it runs, macOS
asks for the microphone and for speech recognition; allow both.
`bin/hud listen --uninstall` removes it; `bin/hud listen` runs it once by hand.

On "Hey Claude": an orb pulses in the middle of the screen for as long as it
listens, the HUD opens full screen in wake mode (the graph dims, a level
meter, your words as they are heard with a block cursor), and what you said
goes to Jarvis. The answer is typed out on screen and read aloud. For eight
seconds after the answer the next sentence needs no wake word, so a
conversation flows. Say only "Hey Claude" and the wake screen opens with its
input focused so you can type instead; esc closes it, `/` focuses it any time.
One conversation continues for as long as the server runs.

`~/.cache/claude-hud/listen --orb` shows the animation on its own;
`--text "hey claude what time is it" --dry-run` runs the wake handler without
a microphone.

What it costs and does: every ask is a headless Claude call that loads your
whole plugin stack, about $0.35 per question at the default model on this
setup, resumed or not. `CLAUDE_HUD_MODEL` picks a cheaper model. The call runs
with default permissions in your home directory: it answers and reads, it
cannot run commands or edit files, so "open Spotify" needs the planned
mac-operator agent, not this. Its events land in the log like any session's,
so the HUD follows Jarvis's work while it answers and returns to your
terminal session when it ends. English only; speech from a video in the room
can trigger it; there is no speaker verification.

## Docked beside the terminal

```
hud            # toggle: open the docked window, or close it
hud open       # open or refocus it
hud big        # full screen, label-free, for a monitor across the room
hud close
```

`bin/hud` runs `hud/dock.py`: Chrome in app mode (no tabs, no address bar),
in its own profile so it is a separate process the command can close again,
sized as a column on the right of the main display. Put `bin` on your PATH or
alias it. Environment:

| Variable | Meaning | Default |
| :-- | :-- | :-- |
| `CLAUDE_HUD_DOCK` | `right`, `left`, or `x,y,w,h` in points | `right` |
| `CLAUDE_HUD_FRACTION` | share of the screen width the column takes | `0.38` |
| `CLAUDE_HUD_BROWSER` | app name of a Chromium-based browser | `Google Chrome` |
| `CLAUDE_MODS_HUD_PORT` | server port | `7431` |
| `CLAUDE_MODS_DISABLE` | `hud` stops the auto-start; `monitor` stops recording | |

With `CLAUDE_HUD_DOCK` set in your shell, the session's first open is the
docked window instead of a browser tab.

### A hotkey

Ghostty 1.4's keybinds can only send text or run Ghostty's own actions, so the
toggle lives in a macOS Shortcut (one-time setup, about a minute):

1. Shortcuts app → New Shortcut → add the action **Run Shell Script**.
2. Script: `python3 ~/claude-hud/hud/dock.py toggle` (your checkout path;
   with the plugin installed, `~/.claude/plugins/marketplaces/claude-hud/hud/dock.py`).
3. In the shortcut's details, add a keyboard shortcut, e.g. ⌃⌥H, and allow it
   to run as a Quick Action.

The hotkey then works in any app, Ghostty included.

## On the page

- Hover a node for its task, elapsed time, tool calls and tokens. Drag to
  move it (it stays pinned, marked by a yellow dot; double-click unpins).
- `c` clears finished agents · `f` big-screen mode · `m` mute · `l` back to
  live from the timeline.
- Many finished siblings fold into one dashed node. Side panel sections
  collapse on click; the tool ticker shows 15 rows and expands.
- `?session=<id>` pins a session; `?big=1` starts in big-screen mode.

## Data and privacy

Records carry session and agent ids, the working directory and transcript
paths, tool names, durations, an agent's description and the first 120
characters of its prompt, where each tool call reached (a file path, a host
or an MCP server name), the first 120 characters of each prompt you type, and
each notification's type. Tool inputs (commands, file contents, search text),
outputs, error texts and message bodies are never logged. What you say
after "Hey Claude" becomes a prompt to a headless Claude session and is
logged like any typed prompt (first 120 characters); Jarvis's `state` records
hold only the state name. The listener keeps no audio and sends none. The log is 0600 in
a 0700 directory and rotates at 20 MB. The server listens on localhost only.
`POST /ask` and `POST /state` need a token the server makes for each run and writes
to `~/.cache/claude-hud/token` (0600); the page gets it inline, the listener reads
the file. It guards against cross-site request forgery; it is
not a local login, since any program on this Mac can run `claude` itself. `POST /focus` activates the terminal app the hook started the server from
and nothing else.

## Limits

- The page follows whichever session logged last; pin one with `?session=`.
- A shell command that reaches several hosts shows the first.
- Tokens are known when an agent stops, not while it runs.
- Sound stays off until the first click or key (browser policy).
- Jarvis answers come whole, not streamed; one ask at a time.
- The docked column is placed on the main display; `hud big` or an explicit
  `x,y,w,h` for another one.

## Tests

`bash test.sh`: the hooks' records on a temporary log, the event model under
`node --test`, a server smoke test, the dock geometry and the launcher.

## License

MIT. The hooks and `lib/events.py` are shared with claude-mods; keep the two
in step.
