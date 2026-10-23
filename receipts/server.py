"""Stdlib web server: UI + JSON API + Server-Sent Events for live progress."""
from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import demo_cases
from .config import Settings
from .llm import LLM
from .pipeline import Pipeline
from .runner import NebiusRunner
from .search import Tavily

STATIC = Path(__file__).resolve().parent.parent / "static"
RUNS: dict[str, dict] = {}
LOCK = threading.Lock()


def _start(user_input: str, seeds: int, runner: str | None) -> str:
    s = Settings.load()
    store = {"events": [], "done": False}

    def emit(e):
        with LOCK:
            store["events"].append(e)

    p = Pipeline(s, emit=emit)
    with LOCK:
        RUNS[p.run_id] = store

    def work():
        try:
            p.run(user_input, seeds=max(1, min(seeds, 32)), runner=runner)
        except Exception as e:  # last-resort guard
            emit({"t": time.time(), "run_id": p.run_id, "stage": "error", "status": "error", "msg": f"{type(e).__name__}: {e}", "data": {}})
        finally:
            store["done"] = True

    threading.Thread(target=work, daemon=True).start()
    return p.run_id


class Handler(BaseHTTPRequestHandler):
    server_version = "receipts/0.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, default=str).encode())

    def do_GET(self):
        path = urlparse(self.path).path
        s = Settings.load()
        if path in ("/", "/index.html"):
            return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/api/config":
            return self._json({"llm": LLM(s).available, "tavily": Tavily(s).available, "nebius_cli": NebiusRunner.cli_available(),
                               "default_runner": s.default_runner})
        if path == "/api/cases":
            return self._json([{"input": c["id"], "title": c["title"], "blurb": c["blurb"], "expected": c["expected"]} for c in demo_cases.CASES.values()])
        if path.startswith("/api/runs/") and path.endswith("/events"):
            return self._sse(path.split("/")[3])
        if path.startswith("/receipts/"):
            name = os.path.basename(path)
            f = Path(s.out_dir) / name
            if f.is_file() and name.startswith("receipt-"):
                ctype = "text/html; charset=utf-8" if name.endswith(".html") else "application/json"
                return self._send(200, f.read_bytes(), ctype)
        self._send(404, b'{"error":"not found"}')

    def do_POST(self):
        if urlparse(self.path).path != "/api/runs":
            return self._send(404, b'{"error":"not found"}')
        n = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._json({"error": "bad json"}, 400)
        user_input = (body.get("input") or "").strip()
        if not user_input:
            return self._json({"error": "input required"}, 400)
        runner = body.get("runner") if body.get("runner") in ("local", "docker", "nebius") else None
        self._json({"id": _start(user_input, int(body.get("seeds", 8)), runner)})

    def _sse(self, run_id: str):
        store = RUNS.get(run_id)
        if not store:
            return self._send(404, b'{"error":"unknown run"}')
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        i, last = 0, time.time()
        try:
            while True:
                with LOCK:
                    batch = store["events"][i:]
                for e in batch:
                    self.wfile.write(f"data: {json.dumps(e, default=str)}\n\n".encode())
                    i += 1
                if batch:
                    self.wfile.flush()
                    last = time.time()
                if store["done"] and not batch:
                    self.wfile.write(b"event: end\ndata: {}\n\n")
                    self.wfile.flush()
                    return
                if time.time() - last > 15:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    last = time.time()
                time.sleep(0.15)
        except (BrokenPipeError, ConnectionResetError):
            return


def serve(host: str = "127.0.0.1", port: int = 8000):
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"Receipts UI on http://{host}:{port}  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
