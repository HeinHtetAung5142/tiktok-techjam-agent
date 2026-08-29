"""Standard-library HTTP server for the local web UI.

`http.server` + `json`, nothing else -- `requirements.txt` is untouched by this package,
so the agent's dependency footprint is exactly what it was.

    py -m webui.server                 # http://127.0.0.1:8000
    py -m webui.server --port 9000
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import random
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from webui import target
from webui.agent_bridge import DISPLAY_DEPTH, MAX_TURNS, TOP_K, AgentBridge


STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY_BYTES = 64 * 1024

# Shared by every request thread; all agent access inside it is already serialized.
BRIDGE: AgentBridge | None = None
RNG = random.Random()
_RNG_LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server_version = "ShoppingCopilotUI/1.0"

    # -- plumbing ----------------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:  # noqa: A002 - stdlib signature
        sys.stderr.write("  %s\n" % (fmt % args))

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Local single-user tool; never cache the API or the assets while iterating.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: dict, status: int = 200) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ValueError("request body too large")
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("request body is not valid JSON") from exc
        return body if isinstance(body, dict) else {}

    # -- routing -----------------------------------------------------------------

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        try:
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path == "/api/stats":
                return self._json(BRIDGE.stats())
            self._json({"error": "not found"}, status=404)
        except Exception as exc:  # never show a traceback page
            self._fail(exc)

    do_HEAD = do_GET

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        try:
            body = self._read_json()
            if path == "/api/session":
                return self._json(
                    {
                        "session_id": BRIDGE.open_session(),
                        "max_turns": MAX_TURNS,
                        "top_k": TOP_K,
                        "display_depth": DISPLAY_DEPTH,
                    }
                )
            if path == "/api/target":
                # Pure reroll. Reads the catalog, touches no session and no agent state,
                # and the drawn product is returned and then forgotten -- the server keeps
                # no record of what the user is hunting for.
                with _RNG_LOCK:
                    return self._json(target.pick(BRIDGE.reader, RNG))
            if path == "/api/message":
                return self._message(body)
            if path == "/api/end":
                BRIDGE.close_session(str(body.get("session_id") or ""))
                return self._json({"ok": True})
            self._json({"error": "not found"}, status=404)
        except ValueError as exc:
            self._json({"error": str(exc)}, status=400)
        except Exception as exc:
            self._fail(exc)

    def _message(self, body: dict) -> None:
        session_id = str(body.get("session_id") or "")
        # The single value from the browser that reaches the agent. Nothing else in
        # `body` is read here, by design -- see the isolation note in agent_bridge.py.
        message = str(body.get("message") or "").strip()
        if not message:
            return self._json({"error": "message is empty"}, status=400)
        try:
            payload = BRIDGE.turn(session_id, message)
        except KeyError:
            return self._json({"error": "unknown session", "expired": True}, status=409)
        self._json(payload)

    def _fail(self, exc: Exception) -> None:
        self.log_message("error: %s: %s", type(exc).__name__, exc)
        try:
            self._json({"error": f"{type(exc).__name__}: {exc}"}, status=500)
        except Exception:
            pass

    # -- static ------------------------------------------------------------------

    def _static(self, relative: str) -> None:
        candidate = (STATIC_DIR / relative).resolve()
        if not candidate.is_file() or STATIC_DIR not in candidate.parents:
            return self._json({"error": "not found"}, status=404)
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type == "application/javascript":
            content_type += "; charset=utf-8"
        self._send(200, candidate.read_bytes(), content_type)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", default="data/catalog.jsonl")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    global BRIDGE
    print("Building the agent index over 50k products (this takes ~15s)...", flush=True)
    BRIDGE = AgentBridge(args.catalog)
    print(
        f"Ready in {BRIDGE.agent.construction_seconds:.1f}s. "
        f"Serving http://{args.host}:{args.port}/  (Ctrl-C to stop)",
        flush=True,
    )

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
