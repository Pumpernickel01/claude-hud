#!/usr/bin/env python3
"""The HUD as a window docked beside the terminal: Chrome in app mode (no tabs, no address bar),
in its own profile so it is a separate process this script can close again.

  python3 dock.py toggle      open the docked window, or close it if it is open
  python3 dock.py open        open (or refocus) it
  python3 dock.py close       close it
  python3 dock.py big         open it full-screen in label-free mode (for a second display)
  python3 dock.py wake        the HUD full screen, in wake mode (what the listener runs on "Hey Claude")
  python3 dock.py listen      build (once) and run the "Hey Claude" wake-word listener, see listen.swift
  python3 dock.py listen --install    run it always: a launchd agent that starts at login and stays up
  python3 dock.py listen --uninstall
  python3 dock.py --print     show the computed window geometry and exit

CLAUDE_HUD_DOCK picks the side: right (default), left, or "x,y,w,h" in pixels.
CLAUDE_HUD_FRACTION is the share of the screen width it takes (default 0.38).
CLAUDE_HUD_BROWSER names another Chromium-based app (default "Google Chrome").
Needs the server running; starts it through launch.py when it is not."""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("CLAUDE_MODS_HUD_PORT", "7431"))
URL = f"http://127.0.0.1:{PORT}/"
PROFILE = os.path.expanduser("~/.cache/claude-hud/chrome")
BROWSER = os.environ.get("CLAUDE_HUD_BROWSER", "Google Chrome")
MENU_BAR = 25


def screen():
    """Main display size in points (what Chrome's window flags use), from system_profiler."""
    try:
        out = subprocess.run(["system_profiler", "SPDisplaysDataType", "-json"], capture_output=True, text=True, timeout=10).stdout
        for gpu in json.loads(out).get("SPDisplaysDataType", []):
            for disp in gpu.get("spdisplays_ndrvs", []):
                if disp.get("spdisplays_main") == "spdisplays_yes":
                    w, h = disp["_spdisplays_resolution"].split("@")[0].split("x")  # "1728 x 1117 @ 120.00Hz"
                    return int(w), int(h)
    except (ValueError, KeyError, OSError, subprocess.SubprocessError):
        pass
    return 1512, 982  # a 14-inch MacBook Pro's default scaling


def geometry(width, height, dock=None, fraction=None):
    """(x, y, w, h) of the window: a column at the right or left of the screen, or an explicit box."""
    dock = dock or os.environ.get("CLAUDE_HUD_DOCK") or "right"
    if "," in dock:
        x, y, w, h = (int(v) for v in dock.split(","))
        return x, y, w, h
    frac = float(fraction or os.environ.get("CLAUDE_HUD_FRACTION") or 0.38)
    w = int(width * frac)
    x = width - w if dock == "right" else 0
    return x, MENU_BAR, w, height - MENU_BAR


def pids():
    out = subprocess.run(["pgrep", "-f", f"user-data-dir={PROFILE}"], capture_output=True, text=True).stdout
    return [int(p) for p in out.split()]


def ensure_server():
    subprocess.run([sys.executable, os.path.join(HERE, "launch.py"), "--no-open"], timeout=10)


def place(x, y, w, h, tries=40):
    """Chrome on macOS ignores --window-position/--window-size for an app window, so the window is moved
    through Chrome's own scripting once it exists: the window whose tab shows the HUD's URL. The first
    set often lands while the window is still being created, so it is repeated until the bounds read back
    close to what was asked. Numbers and the URL travel as argv; only the app name is in the script text."""
    app = BROWSER.replace("\\", "\\\\").replace('"', '\\"')  # the app name must be literal: AppleScript needs its dictionary at compile time
    script = ["osascript", "-e", "on run argv",
              "-e", f'tell application "{app}"',
              "-e", "set w to first window whose URL of active tab starts with (item 2 of argv)",
              "-e", "set x1 to (item 3 of argv) as integer", "-e", "set y1 to (item 4 of argv) as integer",
              "-e", "set x2 to (item 5 of argv) as integer", "-e", "set y2 to (item 6 of argv) as integer",
              "-e", "set bounds of w to {x1, y1, x2, y2}",
              "-e", "delay 0.5",  # the move applies a moment later; reading at once returns the old bounds
              "-e", "set b to bounds of w",
              "-e", "return (item 1 of b as text) & \",\" & (item 2 of b as text) & \",\" & (item 3 of b as text) & \",\" & (item 4 of b as text)",
              "-e", "end tell", "-e", "end run", "--", BROWSER, URL, str(x), str(y), str(x + w), str(y + h)]
    for _ in range(tries):
        r = subprocess.run(script, capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            try:
                got = [int(v) for v in r.stdout.strip().split(",")]
            except ValueError:
                got = []
            if len(got) == 4 and abs(got[0] - x) <= 40 and abs(got[1] - y) <= 40 and abs(got[2] - x - w) <= 40:
                return True
        time.sleep(0.25)
    return False


def open_window(big=False, wake=False):
    """big: full screen, label-free. wake: full screen with the page in wake mode; an open window is
    resized to the screen instead (the page enters wake mode on its own from the listening state)."""
    ensure_server()
    W, H = screen()  # a few seconds of system_profiler, once per open
    if pids():
        subprocess.run(["osascript", "-e", f'tell application "{BROWSER}" to activate'], capture_output=True)
        if wake:
            place(0, 0, W, H, tries=4)
        return
    os.makedirs(PROFILE, mode=0o700, exist_ok=True)
    url = URL + ("?big=1" if big else "?wake=1" if wake else "")
    box = None if big else (0, 0, W, H) if wake else geometry(W, H)
    subprocess.Popen(["open", "-na", BROWSER, "--args", f"--user-data-dir={PROFILE}", f"--app={url}",
                      "--no-first-run", "--no-default-browser-check"] + (["--start-fullscreen"] if big else []),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if box:
        for _ in range(50):  # scripting Chrome while it starts is slow and fails: wait for its process first
            if pids():
                break
            time.sleep(0.2)
        time.sleep(1.0)
        place(*box)


def close_window():
    for pid in pids():
        try:
            os.kill(pid, 15)
        except OSError:
            pass


# The listener is a real app bundle: macOS grants the microphone and speech recognition to an app it can
# name and remember. A bare binary started by launchd has no such identity and is refused without a prompt.
APP = os.path.expanduser(os.environ.get("CLAUDE_HUD_APP", "~/Applications/Claude HUD Listener.app"))
LISTENER = os.path.join(APP, "Contents", "MacOS", "listen")
PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>listen</string>
<key>CFBundleIdentifier</key><string>dev.claude-hud.listen</string>
<key>CFBundleName</key><string>Claude HUD Listener</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>0.1</string>
<key>CFBundleVersion</key><string>1</string>
<key>LSUIElement</key><true/>
<key>LSMinimumSystemVersion</key><string>14.0</string>
<key>NSMicrophoneUsageDescription</key><string>Listens for "Hey Claude" on this Mac; audio never leaves it.</string>
<key>NSSpeechRecognitionUsageDescription</key><string>Recognizes "Hey Claude" and what you ask next, on this Mac only.</string>
</dict></plist>
"""


def build_listener():
    """Compiles listen.swift once (or when it changed) into the app bundle and signs it ad hoc, so the
    permission grants stick to it across rebuilds."""
    src = os.path.join(HERE, "listen.swift")
    if os.path.isfile(LISTENER) and os.path.getmtime(LISTENER) >= os.path.getmtime(src):
        return LISTENER
    os.makedirs(os.path.dirname(LISTENER), exist_ok=True)
    with open(os.path.join(APP, "Contents", "Info.plist"), "w", encoding="utf-8") as f:
        f.write(PLIST)
    os.makedirs(os.path.join(APP, "Contents", "Resources"), exist_ok=True)
    with open(os.path.join(APP, "Contents", "Resources", "hud_dir"), "w", encoding="utf-8") as f:
        f.write(HERE + "\n")  # the app gets no environment when macOS launches it: this is how it finds dock.py
    print("building the listener (once)…", flush=True)
    subprocess.run(["swiftc", "-swift-version", "5", "-O", src, "-o", LISTENER], check=True)
    subprocess.run(["codesign", "--force", "--sign", "-", "--identifier", "dev.claude-hud.listen", APP], check=True)
    return LISTENER


AGENT_LABEL = "dev.claude-hud.listen"
AGENT_PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{AGENT_LABEL}.plist")


def agent_plist(app, log):
    """A launchd agent: the listener from login on, restarted if it dies. It goes through `open`, the
    normal app launch path, which is what makes macOS treat it as an app, ask for the microphone and list
    it in Privacy & Security; an executable run straight from launchd is refused without a word."""
    from xml.sax.saxutils import escape as x
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>{AGENT_LABEL}</string>
<key>ProgramArguments</key><array><string>/usr/bin/open</string><string>-W</string><string>-g</string><string>-a</string><string>{x(app)}</string></array>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
<key>ThrottleInterval</key><integer>10</integer>
<key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
<key>StandardErrorPath</key><string>{x(log)}</string>
<key>StandardOutPath</key><string>{x(log)}</string>
</dict></plist>
"""


def install_agent():
    build_listener()
    log = os.path.expanduser("~/.cache/claude-hud/listen.log")
    os.makedirs(os.path.dirname(AGENT_PLIST), exist_ok=True)
    with open(AGENT_PLIST, "w", encoding="utf-8") as f:
        f.write(agent_plist(APP, log))
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{AGENT_LABEL}"], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", AGENT_PLIST], check=True)
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)  # event markers and lengths only, never words
    os.close(fd)
    print(f"installed {AGENT_PLIST}: the listener now runs from login on; log in {log}\n"
          "The first run asks for the microphone and speech recognition: allow both.")


def uninstall_agent():
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{AGENT_LABEL}"], capture_output=True)
    if os.path.isfile(AGENT_PLIST):
        os.remove(AGENT_PLIST)
    print("listener agent removed")


def listen(extra):
    ensure_server()
    env = dict(os.environ, CLAUDE_HUD_DIR=HERE, CLAUDE_MODS_HUD_PORT=str(PORT))
    os.execve(build_listener(), [LISTENER] + extra, env)


def main():
    cmd = next((a for a in sys.argv[1:] if not a.startswith("--")), "toggle")
    if "--print" in sys.argv and cmd != "listen":
        W, H = screen()
        print(*geometry(W, H))
        return
    if cmd == "listen" and "--install" in sys.argv:
        install_agent()
    elif cmd == "listen" and "--uninstall" in sys.argv:
        uninstall_agent()
    elif cmd == "listen":
        listen([a for a in sys.argv[2:]])
    elif cmd == "wake":
        open_window(wake=True)
    elif cmd == "toggle":
        close_window() if pids() else open_window()
    elif cmd == "open":
        open_window()
    elif cmd == "big":
        open_window(big=True)
    elif cmd == "close":
        close_window()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
