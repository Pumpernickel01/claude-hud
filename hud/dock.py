#!/usr/bin/env python3
"""The HUD as a window docked beside the terminal: Chrome in app mode (no tabs, no address bar),
in its own profile so it is a separate process this script can close again.

  python3 dock.py toggle      open the docked window, or close it if it is open
  python3 dock.py open        open (or refocus) it
  python3 dock.py close       close it
  python3 dock.py big         open it full-screen in label-free mode (for a second display)
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


def open_window(big=False):
    ensure_server()
    if pids():
        subprocess.run(["osascript", "-e", f'tell application "{BROWSER}" to activate'], capture_output=True)
        return
    os.makedirs(PROFILE, mode=0o700, exist_ok=True)
    url = URL + ("?big=1" if big else "")
    box = None if big else geometry(*screen())  # system_profiler takes a few seconds, once per open
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


def main():
    cmd = next((a for a in sys.argv[1:] if not a.startswith("--")), "toggle")
    if "--print" in sys.argv:
        W, H = screen()
        print(*geometry(W, H))
        return
    if cmd == "toggle":
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
