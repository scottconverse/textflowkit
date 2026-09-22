"""The MCP server, exercised as an external harness would exercise it.

Every other adapter test imports the module and calls the tool functions
directly. That proves the *functions* work; it proves nothing about the server.
A harness never imports us - it spawns a process, writes JSON-RPC frames to
stdin, and reads frames back from stdout. Between those two worlds sit the
entry point, stdio framing, protocol-version negotiation, and the constant risk
that something prints to stdout and corrupts the stream.

This module closes that gap. It launches `textflowkit-mcp` as a real subprocess
and speaks the protocol over pipes. Nothing is mocked: the bytes on the wire are
the bytes a harness would see.

What this DOES prove: our server implements MCP over stdio correctly.
What this does NOT prove: that any specific harness connects. That needs the
harness itself.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="the mcp extra is required for the stdio server")

REPO_ROOT = Path(__file__).resolve().parents[1]
TIMEOUT = 30


class StdioClient:
    """A minimal MCP client: newline-delimited JSON-RPC over a child's pipes."""

    def __init__(self) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT / "src")
        env.setdefault("TEXTFLOWKIT_OUTPUT_ROOT", str(REPO_ROOT / "work"))
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "textflowkit.adapters.mcp_server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(REPO_ROOT),
            env=env,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._next_id = 0
        self._stderr: list[str] = []
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    def _drain_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self._stderr.append(line)

    def send(self, method: str, params: dict | None = None, *, notification: bool = False):
        frame: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            frame["params"] = params
        if not notification:
            self._next_id += 1
            frame["id"] = self._next_id
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(frame) + "\n")
        self.proc.stdin.flush()
        if notification:
            return None
        return self.read_response()

    def read_response(self) -> dict:
        """Read frames until one carrying an id arrives (skipping notifications)."""
        assert self.proc.stdout is not None
        deadline = threading.Event()
        while not deadline.is_set():
            line = self.proc.stdout.readline()
            if not line:
                raise AssertionError(
                    "server closed stdout unexpectedly; stderr was:\n"
                    + "".join(self._stderr[-20:])
                )
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AssertionError(
                    f"non-JSON on stdout breaks the MCP stream: {line[:200]!r}"
                ) from exc
            if "id" in payload:
                return payload
        raise AssertionError("unreachable")

    def initialize(self) -> dict:
        response = self.send(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "stdio-protocol-test", "version": "1.0"},
            },
        )
        # Standard MCP handshake: the client confirms with a notification.
        self.send("notifications/initialized", {}, notification=True)
        return response

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


@pytest.fixture
def client():
    c = StdioClient()
    try:
        yield c
    finally:
        c.close()


def test_server_starts_and_completes_handshake(client):
    """The entry point must run in a fresh process and answer initialize."""
    result = client.initialize()
    assert "result" in result, f"initialize failed: {result}"
    info = result["result"]
    assert "protocolVersion" in info
    assert "capabilities" in info
    assert info["serverInfo"]["name"]


def test_protocol_version_is_reported(client):
    """Record the version actually negotiated, so drift is visible."""
    result = client.initialize()
    version = result["result"]["protocolVersion"]
    assert isinstance(version, str) and version
    print(f"negotiated protocolVersion: {version}")


def test_tools_list_over_the_wire(client):
    """tools/list must return the real catalogue, not an empty list."""
    client.initialize()
    result = client.send("tools/list")
    tools = result["result"]["tools"]
    names = {t["name"] for t in tools}

    assert len(tools) >= 8, f"expected the full tool set, got {names}"
    for expected in (
        "transcribe_media",
        "get_transcript",
        "get_job_status",
        "list_jobs",
        "cancel_job",
        "export_transcript",
        "list_sources",
        "search_transcript",
    ):
        assert expected in names, f"{expected} missing from tools/list"


def test_read_only_tool_calls_over_the_wire(client):
    """A real call must return real data through the pipe."""
    client.initialize()
    result = client.send("tools/call", {"name": "list_sources", "arguments": {}})
    payload = result["result"]
    assert "content" in payload
    text = payload["content"][0]["text"]
    data = json.loads(text)
    assert data  # non-empty source catalogue


def test_unknown_job_is_a_clean_error_not_a_crash(client):
    """Errors must come back as protocol responses, not kill the server."""
    client.initialize()
    result = client.send(
        "tools/call", {"name": "get_job_status", "arguments": {"job_id": "nope"}}
    )
    assert "result" in result or "error" in result

    # The server must still be alive and answering afterwards.
    follow_up = client.send("tools/list")
    assert follow_up["result"]["tools"]


def test_stdout_carries_only_json(client):
    """Any stray print to stdout corrupts the protocol stream.

    We send several frames and require every line read back to parse as JSON.
    This is the failure mode that makes a server work when imported and fail as
    soon as a harness spawns it.
    """
    client.initialize()
    for _ in range(3):
        client.send("tools/list")
    # If we got here, every response parsed. Now assert stderr was not used for
    # protocol traffic.
    assert not any(line.strip().startswith("{") for line in client._stderr), (
        "server wrote JSON to stderr instead of stdout"
    )
