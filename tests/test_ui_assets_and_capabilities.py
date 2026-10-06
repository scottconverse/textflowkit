"""Packaged assets and the capability endpoint.

Two promises:

- The workspace shell and its CSS/JS are served **from the package**, so an
  installed wheel serves a working UI and the page references no external origin
  (no CDN, no remote script, no telemetry beacon).
- ``/ui/capabilities`` reports engines, models, assets, and extras **without
  loading a model or downloading anything**, so it is safe on every page load.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from textflowkit.ui.app import create_app

LOOPBACK_PEER = ("127.0.0.1", 50000)


@pytest.fixture
def client():
    app = create_app(host="127.0.0.1", port=8756)
    return TestClient(app, base_url="http://127.0.0.1:8756", client=LOOPBACK_PEER)


def test_shell_references_only_local_assets(client):
    html = client.get("/").text
    # Every <script src> and stylesheet <link href> is a package-served,
    # root-relative path. In-page anchor links (href="#workspace") are not
    # resource references and are excluded by looking only at scripts/links.
    refs = re.findall(r'<script[^>]+src="([^"]+)"', html)
    refs += re.findall(r'<link[^>]+href="([^"]+)"', html)
    assert refs, "expected at least one asset reference"
    for ref in refs:
        assert not ref.startswith(("http://", "https://", "//")), ref
        assert ref.startswith("/assets/"), ref


def test_css_and_js_are_served(client):
    css = client.get("/assets/app.css")
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]
    js = client.get("/assets/app.js")
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]


def test_static_path_traversal_refused(client):
    # A traversal in the asset name is refused (404), never resolved off-package.
    for bad in ("../app.py", "..%2fapp.py", "/etc/passwd"):
        resp = client.get(f"/assets/{bad}")
        assert resp.status_code in (404, 405)


def test_missing_asset_is_404(client):
    assert client.get("/assets/nope.css").status_code == 404


def test_no_external_reference_in_served_assets(client):
    """The shipped JS/CSS contains no absolute http(s) URL."""
    for asset in ("app.js", "app.css"):
        text = client.get(f"/assets/{asset}").text
        assert "http://" not in text
        assert "https://" not in text


def test_buttons_declare_their_own_text_colour(client):
    """Every button paints its own text colour, so it is readable in dark mode.

    A regression the coordinator caught in a real Chrome dark-theme session:
    buttons that set a dark background but left ``color`` unset inherited the
    user-agent default black, giving black-on-dark labels (transcript search
    hits and the recent-job buttons). Buttons must not rely on inheritance; each
    declares a colour that pairs with the theme. This checks the base ``button``
    rule, the two button classes that previously omitted a colour, and - as a
    guard against the same bug in a new rule - that no block which paints a
    themed background (``var(--bg)``/``var(--panel)``/``var(--accent)``) on a
    button-like selector lacks a ``color``.
    """
    import re

    css = client.get("/assets/app.css").text

    # The base button rule carries a colour, so ordinary buttons are never black
    # on a dark panel.
    base = re.search(r"(?m)^button\s*\{(.*?)\}", css, re.DOTALL)
    assert base is not None, "base button rule is missing"
    assert re.search(r"color:\s*var\(--ink\)", base.group(1)), (
        "the base button rule must set color: var(--ink)"
    )

    # The two classes that set a background and previously omitted a colour.
    for selector in (".search-hit", ".recent-item"):
        block = re.search(rf"(?m)^{re.escape(selector)}\s*\{{(.*?)\}}", css, re.DOTALL)
        assert block is not None, f"{selector} rule is missing"
        assert re.search(r"color:\s*var\(--ink\)", block.group(1)), (
            f"{selector} must set color: var(--ink) next to its background"
        )

    # Guard: any button-ish rule that paints a themed background must also set a
    # colour. (Buttons here are elements that render interactive labels.)
    offenders = []
    for match in re.finditer(r"(?m)^([^\n{@][^{]*?)\{([^{}]*)\}", css):
        selector, body = match.group(1).strip(), match.group(2)
        if not re.search(r"\bbutton\b|\.(search-hit|recent-item)\b", selector):
            continue
        paints_bg = re.search(r"background(?:-color)?:\s*var\(--(bg|panel|accent)\)", body)
        if paints_bg and not re.search(r"(?<!-)color:\s*var\(", body):
            offenders.append(selector)
    assert offenders == [], f"button rules paint a background with no colour: {offenders}"


def test_workspace_wires_local_media_playback_and_cleanup(client):
    """The workspace creates a blob URL for the selected local file so transcript
    timestamps can seek, and revokes it when the selection changes.

    No browser runs here, so this pins the wiring structurally: the blob URL is
    created, assigned to a media element, used by the seek path, and revoked in a
    cleanup function that the selection-change paths call.

    A local file can be an hours-long meeting, so playback must never be invisible
    and unstoppable: the element is shown in the visible ``#player`` container with
    native controls and an accessible name, and the container is emptied and hidden
    again on cleanup.
    """
    js = client.get("/assets/app.js").text
    assert "URL.createObjectURL(" in js, "the selected file must get a blob URL"
    assert "URL.revokeObjectURL(" in js, "the blob URL must be revoked to avoid a leak"
    # The media element is the seek target the transcript uses.
    assert "state.mediaEl" in js and "state.mediaUrl" in js
    # A visible player: the element is put in the #player container with native
    # controls and an accessible name, so playback is neither invisible nor
    # unstoppable.
    assert 'el.controls = true' in js, "the local player must expose native controls"
    assert 'setAttribute("aria-label"' in js, "the local player must be named for assistive tech"
    assert 'player.replaceChildren(el)' in js, "the player element must be shown in #player"
    # Cleanup exists and is called when the selection changes (new file, URL, job).
    assert "function clearLocalMedia(" in js
    assert js.count("clearLocalMedia()") >= 3
    # ... and it removes the element and hides the container, so a source switch
    # leaves no visible player behind.
    assert "player.replaceChildren()" in js, "cleanup must empty the player container"
    assert "player.hidden = true" in js, "cleanup must hide the player container"


def test_workspace_clears_search_results_when_the_watched_job_changes(client):
    """Search hits belong to the job they were searched against.

    Switching to another job (a recent job, a cancelled job) must not leave the
    previous job's matches rendered over the new one. ``watchJob`` clears and
    hides ``#search-results`` so a stale hit cannot persist. No browser runs here,
    so this pins the wiring structurally.
    """
    js = client.get("/assets/app.js").text
    assert 'const searchBox = $("search-results");' in js
    assert "searchBox.replaceChildren();" in js
    assert "searchBox.hidden = true;" in js


def test_uploaded_note_stops_saying_press_transcribe(client):
    """After a successful upload the note reports the staged file, not an
    instruction to press Transcribe to upload again."""
    js = client.get("/assets/app.js").text
    # The pre-upload hint may say "press Transcribe to upload" ...
    assert "Press Transcribe to upload" in js
    # ... but a successful upload rewrites the note to say it *is* uploaded.
    assert "uploaded." in js


def test_file_input_resets_so_reselecting_a_file_raises_change(client):
    """Re-selecting the *same* file after switching sources must raise change.

    A real-browser regression: the operator picked a local file (upload), then
    switched to a URL, then chose the *same* local file again. A file input keeps
    its last selection, so the second pick fired no ``change`` event, ``setUpload``
    never ran, the source stayed the URL, and Transcribe started another URL job
    instead of the chosen file.

    The handler must read the File, hand it to ``setUpload``, then clear the input
    value so any later pick — same file or different — raises ``change`` again.
    The order matters: clearing before reading would drop the File. No browser runs
    here, so this pins the handler body structurally.
    """
    js = client.get("/assets/app.js").text
    match = re.search(
        r'fileInput\.addEventListener\("change",\s*\(\)\s*=>\s*\{(.*?)\}\s*\);',
        js,
        re.DOTALL,
    )
    assert match is not None, "the file-input change handler is missing"
    body = match.group(1)

    # The File is read and handed to setUpload ...
    read = body.find("fileInput.files")
    feed = body.find("setUpload(file)")
    reset = body.find('fileInput.value = ""')
    assert read != -1, "the handler must read the chosen File"
    assert feed != -1, "the handler must hand the File to setUpload"
    # ... and the input is cleared so the next pick of the same file still fires.
    assert reset != -1, "the handler must reset fileInput.value so a re-pick raises change"
    # Clearing must come *after* reading the File, or the File is lost.
    assert read < reset, "the File must be read before the input value is cleared"
    assert feed < reset, "setUpload must receive the File before the input is cleared"


def test_drag_and_drop_flow_is_unchanged(client):
    """The drag-and-drop path still hands the dropped File straight to setUpload.

    The file-input fix must not touch the drop path: a dropped file has no input
    to reset, so it calls ``setUpload`` directly from ``dataTransfer.files``.
    """
    js = client.get("/assets/app.js").text
    assert "e.dataTransfer?.files?.[0]" in js
    assert "if (f) setUpload(f);" in js
    # The drop path must not reference the file input at all.
    drop = re.search(
        r'addEventListener\("drop",\s*\(e\)\s*=>\s*\{(.*?)\}\s*\);', js, re.DOTALL
    )
    assert drop is not None, "the drop handler is missing"
    assert "fileInput" not in drop.group(1), "the drop path must not touch the file input"


def test_capabilities_reports_defaults(client):
    caps = client.get("/ui/capabilities").json()
    assert caps["default_engine"] == "whistle"
    assert "whistle" in caps["engines"]
    assert caps["max_upload_bytes"] == 2 * 1024 * 1024 * 1024
    assert caps["download_formats"] == ["txt", "md", "srt", "vtt", "json", "docx", "pdf"]


def test_capabilities_lists_extras_without_importing_models(client):
    caps = client.get("/ui/capabilities").json()
    names = {e["name"] for e in caps["extras"]}
    assert {"export-docx", "export-pdf", "diarize", "faster-whisper"} <= names
    for e in caps["extras"]:
        assert isinstance(e["installed"], bool)


def test_capabilities_reports_whistle_asset_status(client):
    caps = client.get("/ui/capabilities").json()
    assets = caps["assets"]
    assert assets["managed"] is True
    assert "download_required" in assets
    # A status probe must carry no model data.
    assert "model" not in assets or isinstance(assets.get("model"), str)


def test_capabilities_does_not_load_a_model(client, monkeypatch):
    """Guard the promise that the endpoint is import-only and offline.

    If the endpoint downloaded the Whistle model, `_install_pinned` would be
    reached; failing that call proves nothing tried to fetch anything.
    """
    from textflowkit.core import whistle_assets

    def boom(*a, **k):  # pragma: no cover - only called on a regression
        raise AssertionError("capabilities must not download assets")

    monkeypatch.setattr(whistle_assets, "_install_pinned", boom)
    assert client.get("/ui/capabilities").status_code == 200


def test_preflight_refuses_missing_local_file(client):
    import re

    token = re.search(
        r'name="textflowkit-capability" content="([^"]+)"', client.get("/").text
    ).group(1)
    resp = client.post(
        "/ui/preflight",
        json={"source": "C:/no/such/file.wav", "formats": ["txt"]},
        headers={"x-textflowkit-ui-capability": token},
    )
    body = resp.json()
    assert body["ok"] is False
    assert any("no such file" in e for e in body["errors"])


def test_preflight_ok_for_url(client):
    import re

    token = re.search(
        r'name="textflowkit-capability" content="([^"]+)"', client.get("/").text
    ).group(1)
    resp = client.post(
        "/ui/preflight",
        json={"source": "https://example.com/a.mp3", "formats": ["txt"]},
        headers={"x-textflowkit-ui-capability": token},
    )
    assert resp.json()["ok"] is True
