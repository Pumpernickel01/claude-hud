#!/usr/bin/env python3
"""HUD server: serves index.html and streams ~/.claude/agent-monitor/events.jsonl as
Server-Sent Events. Localhost only: the log carries paths and prompt snippets.

  python3 serve.py [--port 7431] [--open]

Jarvis: POST /ask {"text": ..., "speak": bool} runs `claude -p` (one at a time, one continuing
conversation per server run, cwd $HOME) and broadcasts the exchange to every open page as `chat`
events on the same stream; POST /state {"name": listening|thinking|speaking|idle} appends a `state`
record to the log for the status strip. CLAUDE_HUD_MODEL picks the model, CLAUDE_HUD_CLAUDE the
binary (tests point it at a stub).
"""
import argparse
import json
import os
import queue
import secrets
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))
from events import emit  # noqa: E402
LOG_PATH = os.environ.get("CLAUDE_MODS_EVENT_LOG",
                          os.path.join(os.path.expanduser("~"), ".claude", "agent-monitor", "events.jsonl"))
FILES = {"/": ("index.html", "text/html; charset=utf-8"),
         "/model.mjs": ("model.mjs", "text/javascript; charset=utf-8")}  # hardcoded: mimetypes lacks .mjs
POLL_S, PING_S = 0.25, 15.0
CLAUDE = os.environ.get("CLAUDE_HUD_CLAUDE", "claude")
TOKEN_FILE = os.environ.get("CLAUDE_HUD_TOKEN_FILE", os.path.expanduser("~/.cache/claude-hud/token"))
# Per run; the page gets it inline, the listener reads the 0600 file. A cross-site request forgery guard,
# not local authentication: only a loopback Host can fetch the page and other sites cannot read it, while
# any local program can run `claude` itself, so there is no local boundary to enforce here.
TOKEN = secrets.token_hex(16)


def write_token():
    os.makedirs(os.path.dirname(TOKEN_FILE), mode=0o700, exist_ok=True)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(TOKEN)
MODEL = os.environ.get("CLAUDE_HUD_MODEL")
SAY_CHARS = 600


class Jarvis:
    """One conversation with `claude -p`, one ask at a time, its exchanges fanned out to every open page."""

    def __init__(self):
        self.session = str(uuid.uuid4())
        self.started = False
        self.lock = threading.Lock()
        self.busy = False
        self.current = "idle"  # the last state set, for GET /jarvis
        self.proc = None  # the running claude or say, for /cancel
        self.clients = set()  # one queue per open /events stream

    def listen(self):
        q = queue.Queue()
        with self.lock:
            self.clients.add(q)
        return q

    def drop(self, q):
        with self.lock:
            self.clients.discard(q)

    def broadcast(self, msg):
        with self.lock:
            for q in self.clients:
                q.put(msg)

    def state(self, name):
        self.current = name
        emit("state", "running", {}, state=name)  # no session: the strip reads it, no session is switched

    def ask(self, text, speak):
        with self.lock:
            if self.busy:
                return False
            self.busy = True
        threading.Thread(target=self.run, args=(text, speak), daemon=True).start()
        return True

    def cancel(self):
        """Stops the answer being made or spoken. True when there was one."""
        with self.lock:
            p, busy = self.proc, self.busy
        if not busy:
            return False
        self.cancelled = True
        if p and p.poll() is None:
            p.terminate()
            threading.Timer(3.0, lambda: p.poll() is None and p.kill()).start()  # a call that ignores the request is killed
        return True

    def run(self, text, speak):
        try:
            self.broadcast({"kind": "ask", "text": text})
            self.state("thinking")
            self.cancelled = False
            for attempt in (1, 2):
                cmd = [CLAUDE, "-p", "--output-format", "json"]
                cmd += ["--resume", self.session] if self.started else ["--session-id", self.session]
                if MODEL:
                    cmd += ["--model", MODEL]
                try:
                    self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=os.path.expanduser("~"))
                    stdout, stderr = self.proc.communicate(text, timeout=600)
                    out = json.loads(stdout) if stdout.strip() else {}
                    answer = out.get("result") or (stderr.strip() or "no answer")[-800:]
                    error = bool(out.get("is_error")) or self.proc.returncode != 0
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.communicate()  # reaped: no orphan keeps running after the timeout
                    answer, error, out = "claude took longer than ten minutes and was stopped", True, {}
                except (OSError, ValueError, subprocess.SubprocessError) as e:
                    answer, error, out = f"claude failed: {e}", True, {}
                if error and attempt == 1 and "already in use" in answer and not self.cancelled:
                    # a cancelled call can leave the session marked in use: go on in a fresh conversation
                    print("jarvis: session in use, starting a new conversation", file=sys.stderr, flush=True)
                    self.session, self.started = str(uuid.uuid4()), False
                    continue
                break
            if self.cancelled:
                answer, error = "cancelled", True
            self.started = self.started or not error
            print("jarvis: " + ("error: " + answer[:300] if error else f"answer, {len(answer)} chars, ${out.get('total_cost_usd', 0)}"), file=sys.stderr, flush=True)  # errors in full, answers by length
            self.broadcast({"kind": "error" if error else "answer", "text": answer, "cost": out.get("total_cost_usd"), "ms": out.get("duration_ms")})
            if speak and not error and sys.platform == "darwin":
                self.state("speaking")
                self.proc = subprocess.Popen(["say"], stdin=subprocess.PIPE, text=True)
                try:
                    self.proc.communicate(answer[:SAY_CHARS], timeout=120)  # on stdin: an answer starting with - is not an option
                except subprocess.SubprocessError:
                    self.proc.kill()
            self.proc = None
            self.state("idle")
        finally:
            with self.lock:
                self.busy = False


JARVIS = Jarvis()
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
                body = f.read().replace(b"__HUD_TOKEN__", TOKEN.encode()) if name == "index.html" else f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src data:; connect-src 'self'")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/events":
            self.stream()
        elif path == "/jarvis":  # for the listener: when the answer has been spoken, a follow-up may begin
            body = json.dumps({"busy": JARVIS.busy, "state": JARVIS.current}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n <= 65536 else {}
        except (ValueError, OSError):
            return {}

    def do_POST(self):
        if not self.local():
            self.send_error(403)
            return
        if self.path in ("/ask", "/state", "/cancel") and not secrets.compare_digest(self.headers.get("X-HUD-Token") or "", TOKEN):
            self.send_error(403, "token")
            return
        if self.path == "/ask":
            b = self.body()
            text = b.get("text") if isinstance(b.get("text"), str) else ""
            if not text.strip():
                self.send_error(400)
            elif JARVIS.ask(text.strip()[:4000], bool(b.get("speak"))):
                self.send_response(202)
                self.end_headers()
            else:
                self.send_error(409, "an ask is already running")
            return
        if self.path == "/cancel":
            self.send_response(204 if JARVIS.cancel() else 200)
            self.end_headers()
            return
        if self.path == "/state":
            b = self.body()
            name, text = b.get("name"), b.get("text")
            if name not in ("listening", "thinking", "speaking", "idle"):
                self.send_error(400)
                return
            if isinstance(text, str):  # words heard so far: shown live on the page, never logged
                JARVIS.broadcast({"kind": "hearing", "text": text[:1000], "phase": b.get("phase") if b.get("phase") in ("listening", "sending") else "listening"})
                if JARVIS.current == name:
                    self.send_response(204)
                    self.end_headers()
                    return
            JARVIS.state(name)
            self.send_response(204)
            self.end_headers()
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
        chat = JARVIS.listen()
        try:
            self.send_lines(tail.read())
            self.wfile.write(b"event: synced\ndata: 1\n\n")  # replay done: the page may start animating
            self.wfile.flush()
            last = time.monotonic()
            while True:
                time.sleep(POLL_S)
                lines = tail.read()
                sent = False
                if lines:
                    self.send_lines(lines)
                    sent = True
                while not chat.empty():
                    self.wfile.write(b"event: chat\ndata: " + json.dumps(chat.get()).encode() + b"\n\n")
                    sent = True
                if sent:
                    self.wfile.flush()
                    last = time.monotonic()
                elif time.monotonic() - last > PING_S:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last = time.monotonic()
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass
        finally:
            JARVIS.drop(chat)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7431)
    ap.add_argument("--open", action="store_true", help="open the page in the default browser")
    args = ap.parse_args()
    write_token()
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
