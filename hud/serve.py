#!/usr/bin/env python3
"""HUD server: serves index.html and streams ~/.claude/agent-monitor/events.jsonl as
Server-Sent Events. Localhost only: the log carries paths and prompt snippets.

  python3 serve.py [--port 7431] [--open]
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.environ.get("CLAUDE_MODS_EVENT_LOG",
                          os.path.join(os.path.expanduser("~"), ".claude", "agent-monitor", "events.jsonl"))
FILES = {"/": ("index.html", "text/html; charset=utf-8"),
         "/model.mjs": ("model.mjs", "text/javascript; charset=utf-8")}  # hardcoded: mimetypes lacks .mjs
POLL_S, PING_S = 0.25, 15.0
BUNDLE = {  # TERM_PROGRAM -> app to activate, as hooks/notify/notify.py maps it
    "apple_terminal": "com.apple.Terminal", "ghostty": "com.mitchellh.ghostty",
    "iterm2": "com.googlecode.iterm2", "vscode": "com.microsoft.VSCode"}
# the app to bring forward on a toast click: the terminal by TERM_PROGRAM, else the app macOS says launched us
# (under herdr TERM_PROGRAM is "herdr", a program with no bundle; __CFBundleIdentifier still names the terminal)
TERMINAL = BUNDLE.get((os.environ.get("TERM_PROGRAM") or "").lower()) or os.environ.get("__CFBundleIdentifier")


class Tail:
    """Complete lines appended since the last read; a shrunken file (rotation) restarts at 0."""

    def __init__(self, path, offset=0):
        self.path, self.offset, self.rest = path, offset, b""

    def read(self):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return []
        if size < self.offset:
            self.offset, self.rest = 0, b""
        if size == self.offset:
            return []
        with open(self.path, "rb") as f:
            f.seek(self.offset)
            data = self.rest + f.read()
            self.offset = f.tell()
        *lines, self.rest = data.split(b"\n")
        return [l for l in lines if l.strip()]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def local(self):
        """True for a request addressed to this server by a loopback name: a web page elsewhere that rebinds
        its DNS name to 127.0.0.1 sends its own Host and gets nothing; a POST must also come from our origin."""
        host = (self.headers.get("Host") or "").lower()
        port = self.server.server_address[1]
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"):
            return False
        origin = self.headers.get("Origin")
        return self.command != "POST" or origin in (f"http://{host}", None)

    def do_GET(self):
        if not self.local():
            self.send_error(403)
            return
        path = self.path.split("?", 1)[0]
        if path in FILES:
            name, ctype = FILES[path]
            with open(os.path.join(HERE, name), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src data:; connect-src 'self'")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/events":
            self.stream()
        else:
            self.send_error(404)

    def do_POST(self):
        if not self.local():
            self.send_error(403)
            return
        if self.path == "/focus" and TERMINAL:
            subprocess.Popen(["osascript", "-e", "on run argv", "-e", "tell application id (item 1 of argv) to activate",
                              "-e", "end run", "--", TERMINAL], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.send_response(204)
            self.end_headers()
        else:
            self.send_error(404)

    def send_lines(self, lines):
        for line in lines:
            try:
                json.loads(line)  # forward only well-formed records
            except ValueError:
                continue
            self.wfile.write(b"data: " + line + b"\n\n")
        self.wfile.flush()

    def stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        tail = Tail(LOG_PATH)  # shortcut: replays the whole log on connect; fine under the 20 MB rotation
        try:
            self.send_lines(tail.read())
            self.wfile.write(b"event: synced\ndata: 1\n\n")  # replay done: the page may start animating
            self.wfile.flush()
            last = time.monotonic()
            while True:
                time.sleep(POLL_S)
                lines = tail.read()
                if lines:
                    self.send_lines(lines)
                    last = time.monotonic()
                elif time.monotonic() - last > PING_S:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last = time.monotonic()
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7431)
    ap.add_argument("--open", action="store_true", help="open the page in the default browser")
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    srv.daemon_threads = True
    url = f"http://127.0.0.1:{args.port}/"
    print(f"HUD at {url}  (log: {LOG_PATH})", flush=True)
    if args.open:
        threading.Timer(0.3, webbrowser.open, [url]).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
