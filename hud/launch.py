#!/usr/bin/env python3
"""SessionStart hook: starts the HUD server once (detached) and opens the page the first time.
Skipped with `hud` in CLAUDE_MODS_DISABLE. Never blocks the session: any failure exits 0.
`--no-open` only makes sure the server runs (dock.py uses it); with CLAUDE_HUD_DOCK set, the first
open is the docked app window instead of a browser tab."""
import os
import socket
import subprocess
import sys
import time
import webbrowser

PORT = int(os.environ.get("CLAUDE_MODS_HUD_PORT", "7431"))


def listening():
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.3):
            return True
    except OSError:
        return False


def listener_wanted():
    """CLAUDE_HUD_LISTEN=1 starts the wake-word listener with the session, once it has been run by hand
    and allowed to use the microphone and speech recognition."""
    if os.environ.get("CLAUDE_HUD_LISTEN") != "1":
        return
    # the binary, or a dock.py listen still building it: either means one is on its way
    if subprocess.run(["pgrep", "-f", "Claude HUD Listener.app|dock.py listen"], capture_output=True).returncode == 0:
        return
    dock = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dock.py")
    subprocess.Popen([sys.executable, dock, "listen"], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    if "hud" in os.environ.get("CLAUDE_MODS_DISABLE", "").split(","):
        return
    no_open = "--no-open" in sys.argv[1:]
    if not no_open:
        listener_wanted()  # not from dock.py's own ensure_server(): that would spawn listeners in a chain
    if listening():
        return
    serve = os.path.join(os.path.dirname(os.path.abspath(__file__)), "serve.py")
    # fully detached: a child still attached to the hook would hold the session start
    subprocess.Popen([sys.executable, serve, "--port", str(PORT)], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(20):
        if listening():
            if no_open:
                return
            if os.environ.get("CLAUDE_HUD_DOCK"):
                dock = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dock.py")
                subprocess.Popen([sys.executable, dock, "open"], start_new_session=True,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                webbrowser.open(f"http://127.0.0.1:{PORT}/")
            return
        time.sleep(0.1)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
