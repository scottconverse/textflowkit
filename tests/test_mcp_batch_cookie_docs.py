"""AL-003: the MCP batch cookie capability must be documented correctly.

Audit finding AL-003 — ``docs/adapters.md`` stated that ``submit_batch_media``
has no ``cookies_from_browser`` parameter, but the tool's real signature accepts
one, forwards it to every shared request, and the production refusal is enforced
by the same common guard as every other surface. The v0.1.7 changelog repeated
the incorrect "no parameter" claim.

These tests read the schema the **installed MCP SDK** actually generates for
``tools/list`` — not the function source — so a docstring or doc sentence that
drifts from the published contract fails here. They also pin the production
refusal so the documentation fix cannot be mistaken for a capability removal.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="the mcp extra is required for the MCP adapter")

from textflowkit.adapters.mcp_server import mcp  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


def _batch_schema() -> dict:
    """The real SDK-generated input schema for ``submit_batch_media``."""

    async def _go():
        for tool in await mcp.list_tools():
            if tool.name == "submit_batch_media":
                # The installed SDK publishes `input_schema`; serialization to
                # the wire uses `inputSchema`. Read the model field the SDK
                # actually populates rather than assuming a wire name.
                return tool.input_schema
        raise AssertionError("submit_batch_media not published")

    return asyncio.run(_go())


def test_sdk_schema_exposes_cookies_from_browser_on_batch():
    """The published batch schema really does accept the cookie parameter."""
    schema = _batch_schema()
    props = schema["properties"]
    assert "cookies_from_browser" in props, (
        "docs claim no batch cookie parameter; schema disagrees"
    )
    assert props["cookies_from_browser"]["anyOf"] == [
        {"type": "string"}, {"type": "null"}
    ]


def _adapters_doc() -> str:
    return (REPO_ROOT / "docs" / "adapters.md").read_text(encoding="utf-8")


def _changelog() -> str:
    return (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def test_adapters_doc_does_not_claim_batch_rejects_cookies():
    """The adapter matrix must agree with the schema it describes."""
    doc = _adapters_doc()
    assert "not a supported parameter" not in doc, (
        "adapters.md still says the MCP batch tool has no cookie parameter"
    )
    assert "signature has no such parameter" not in doc
    # The paragraph must positively state the batch tool accepts a shared
    # cookie value (and the production refusal that still applies).
    assert "submit_batch_media" in doc
    lowered = doc.lower()
    assert "cookies_from_browser" in doc
    assert "production" in lowered and "refus" in lowered


def test_changelog_corrects_the_historical_batch_cookie_claim():
    """The v0.1.7 text said "no cookies_from_browser parameter"; fix it."""
    log = _changelog()
    assert "cookies_from_browser` parameter" not in log or (
        "accepts" in log.lower()
    ), "changelog still repeats the incorrect no-parameter claim"


def test_production_still_refuses_cookie_submissions(tmp_path, monkeypatch):
    """The documentation fix must not weaken the production refusal."""
    from textflowkit.core.service import (
        ServiceConfigurationError,
        reject_browser_cookie_requests,
    )

    monkeypatch.setenv("TEXTFLOWKIT_PROFILE", "production")
    with pytest.raises(ServiceConfigurationError):
        reject_browser_cookie_requests("firefox")
    # An empty/None value is admissible (nothing was requested).
    reject_browser_cookie_requests(None)
