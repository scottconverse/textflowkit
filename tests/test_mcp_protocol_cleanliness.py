"""G5A — the MCP protocol stream stays clean across the G5A guidance change.

The audit's UX-002 fix changes the wording MCP returns for terminal states. That
change touches the one surface where a stray print is fatal: the stdio protocol
stream. ``tests/test_stdio_protocol.py`` proves the server frames correctly for
``list_sources`` and ``list_jobs``; this module narrows the same guarantee onto
the *status/transcript guidance* tools the fix edits, over a real spawned
subprocess.

It is deliberately a **guard**, not RED: it passes today (an unknown job is a
clean error) and must keep passing after the guidance rewrite. If the fix prints
a human-readable line instead of returning it in the payload, or writes guidance
to stderr where it collides with protocol traffic, this test catches it.

Nothing is imported: the bytes on the wire are what a harness sees. The child is
spawned as ``test_stdio_protocol.StdioClient`` spawns it, so the two modules
exercise the same entry point - with one deliberate hardening: the child gets an
**isolated** environment, never the owner's.

The MCP child inherits the parent's environment by default, and an owner machine
or CI may export ``TEXTFLOWKIT_DB`` (a real jobs database) or a
``TEXTFLOWKIT_OUTPUT_ROOT``/``TEXTFLOWKIT_INPUT_ROOT`` pointing at real
directories. The server's ``_lifespan`` runs ``recover_startup()`` the instant it
starts, which reads and may *write* the store - so an inherited ``TEXTFLOWKIT_DB``
would let this spawned process reap rows in the owner's real database, and an
inherited output root would let a later tool call write real files. Every
``TEXTFLOWKIT_*`` variable is therefore dropped and the two roots the server reads
at startup are pinned under a per-client temp directory that ``close()`` removes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="the mcp extra is required for the stdio server")

REPO_ROOT = Path(__file__).resolve().parents[1]

# The environment variables the server reads to choose a store or a filesystem
# root: `TEXTFLOWKIT_DB` (durable jobs database, read at startup by
# `recover_startup`), `TEXTFLOWKIT_OUTPUT_ROOT`/`TEXTFLOWKIT_INPUT_ROOT` (the
# confinement roots a tool call reads and writes under), `TEXTFLOWKIT_WORK_ROOT`
# (the scratch/service root `service_work_root` returns), and
# `TEXTFLOWKIT_PROFILE` (the service profile, which reads the DB and root vars).
# All are dropped from the child's environment so it can only ever touch this
# test's temp tree - never the owner's real database or media/output directories.
_OWNER_ONLY_ENV = (
    "TEXTFLOWKIT_DB",
    "TEXTFLOWKIT_OUTPUT_ROOT",
    "TEXTFLOWKIT_INPUT_ROOT",
    "TEXTFLOWKIT_WORK_ROOT",
    "TEXTFLOWKIT_PROFILE",
)


class _Client:
    """Minimal newline-delimited JSON-RPC client over a spawned server."""

    def __init__(self) -> None:
        # A private scratch tree for the child: it is the only place the server
        # may read or write, and the only root it is told about below.
        self.root = Path(tempfile.mkdtemp(prefix="g5a-mcp-"))
        env = dict(os.environ)
        for name in _OWNER_ONLY_ENV:
            env.pop(name, None)
        env["PYTHONPATH"] = str(REPO_ROOT / "src")
        env["TEXTFLOWKIT_OUTPUT_ROOT"] = str(self.root)
        env["TEXTFLOWKIT_INPUT_ROOT"] = str(self.root)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "textflowkit.adapters.mcp_server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(REPO_ROOT), env=env, text=True, encoding="utf-8", bufsize=1,
        )
        self._id = 0
        self.stderr: list[str] = []
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self.stderr.append(line)

    def call(self, method: str, params: dict | None = None, *, notify: bool = False):
        frame: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            frame["params"] = params
        if not notify:
            self._id += 1
            frame["id"] = self._id
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(frame) + "\n")
        self.proc.stdin.flush()
        if notify:
            return None
        return self._read()

    def _read(self) -> dict:
        assert self.proc.stdout is not None
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise AssertionError(
                    "server closed stdout; stderr:\n" + "".join(self.stderr[-20:])
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

    def initialize(self) -> None:
        self.call("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "g5a-cleanliness", "version": "1.0"},
        })
        self.call("notifications/initialized", {}, notify=True)

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
        # Remove the private scratch tree so a run leaves nothing behind.
        shutil.rmtree(self.root, ignore_errors=True)


@pytest.fixture
def client():
    c = _Client()
    try:
        yield c
    finally:
        c.close()


def test_status_and_transcript_calls_return_clean_json_frames(client):
    """Every guidance call must be a protocol frame, never a stray print.

    An unknown job exercises the same code path the terminal-state branch sits
    in; the fix must keep returning a payload object rather than printing.

    Each tool is called with *its own required arguments*: ``search_transcript``
    takes a required ``query`` alongside ``job_id``, and the MCP SDK validates
    arguments against the tool schema *before* the function runs. A call missing
    ``query`` is refused by the SDK with a plain-text validation message in the
    content block - legitimate protocol behaviour, but not the tool's own JSON
    payload - so it is not what this guard is measuring.
    """
    client.initialize()
    calls = {
        "get_job_status": {"job_id": "nope"},
        "get_transcript": {"job_id": "nope"},
        # `query` is required; supply it so the call reaches the tool and returns
        # the tool's own JSON payload, as the other two do.
        "search_transcript": {"job_id": "nope", "query": "anything"},
    }
    for tool, arguments in calls.items():
        result = client.call("tools/call", {"name": tool, "arguments": arguments})
        assert "result" in result or "error" in result, (tool, result)
        payload = result.get("result") or {}
        if "content" in payload:
            # The tool ran and framed its answer as JSON text inside the content
            # block. A schema-validation refusal is a protocol-level error, not
            # this payload; it is asserted separately below.
            json.loads(payload["content"][0]["text"])
    # The server is still alive and answering.
    assert client.call("tools/list")["result"]["tools"]


def test_missing_required_argument_is_a_clean_protocol_error(client):
    """A schema-invalid call is a protocol error, not a crash or a stray print.

    ``query`` is required by ``search_transcript``. The SDK refuses the call
    before the function runs and returns its validation text in the content
    block; that text is not the tool's JSON payload, so only the *framing* is
    asserted here - the response is a well-formed protocol frame and the server
    survives to answer the next call. This is the shape the fix must not disturb.
    """
    client.initialize()
    result = client.call(
        "tools/call", {"name": "search_transcript", "arguments": {"job_id": "nope"}}
    )
    assert "result" in result or "error" in result, result
    # Whatever the SDK returns, it is a framed protocol response, not a crash.
    assert client.call("tools/list")["result"]["tools"]


def test_server_never_writes_protocol_json_to_stderr(client):
    """Guidance must go through the payload, not the stderr channel."""
    client.initialize()
    client.call("tools/call", {"name": "get_job_status", "arguments": {"job_id": "nope"}})
    client.call("tools/list")
    assert not any(line.strip().startswith("{") for line in client.stderr), (
        "server wrote JSON to stderr instead of stdout"
    )
