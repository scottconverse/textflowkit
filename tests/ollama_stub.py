"""A minimal stand-in for the Ollama HTTP API.

Used by the test suite so the translation *transport* is exercised everywhere -
including CI, which has no Ollama. Only the model is fake; the request shape,
response parsing, batching, fallback, and caching all run for real.

This is the difference between "the live path is unverified" and "everything but
model quality is verified on every platform."
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Self

_NUMBERED = re.compile(r"^\s*(\d+)\s*[.):\-]\s*(.*)$")


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
        return

    def do_GET(self):
        if self.path == "/api/tags":
            self._json({"models": [{"name": "stub:latest"}]})
        else:
            self._json({"error": "not found"}, status=404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._json({"error": "bad json"}, status=400)
            return

        if self.path != "/api/generate":
            self._json({"error": "not found"}, status=404)
            return

        model = payload.get("model", "")
        prompt = payload.get("prompt", "")

        if "reject" in prompt:
            self._json({"error": "model not found"}, status=404)
            return

        server = self.server
        server.calls.append({"model": model, "prompt": prompt})  # type: ignore[attr-defined]

        if server.mode == "garbage":  # type: ignore[attr-defined]
            response = "this will not parse"
        else:
            # echo numbered lines back as "[<target-ish>]" translations
            lines = [ln for ln in prompt.splitlines() if _NUMBERED.match(ln)]
            if lines:
                out = []
                for ln in lines:
                    m = _NUMBERED.match(ln)
                    out.append(f"{m.group(1)}. x-{m.group(2).strip()}")
                response = "\n".join(out)
            else:
                tail = prompt.splitlines()[-1].strip()
                response = f"x-{tail}"

        self._json({"response": response, "done": True})

    def _json(self, obj, status: int = 200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class OllamaStub:
    """Context manager: a stub Ollama on an ephemeral port."""

    def __init__(self, mode: str = "echo") -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.mode = mode            # type: ignore[attr-defined]
        self.server.calls = []             # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def host(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    @property
    def calls(self) -> list[dict]:
        return self.server.calls  # type: ignore[attr-defined]

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
