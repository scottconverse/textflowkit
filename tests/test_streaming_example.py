"""The runnable browser example and its AudioWorklet resampler.

Two things are checked here, both without ever touching a microphone:

* the packaged page and worklet are self-hosted, contain no CDN/analytics/external
  fetch, are served only when streaming is opted into, and the UI copy embeds the
  UI capability while the standalone copy does not; and
* the resampler in ``streaming-capture-worklet.js`` is *deterministic*: driven
  through a Node harness that stands in for the AudioWorkletGlobalScope it
  resamples 44.1 kHz and 48 kHz input to 16 kHz mono signed-16 little-endian,
  clips out-of-range samples, and emits frames of at most one second.

The Node harness is skipped when Node is absent, so the suite still runs on a
machine without it; the resampler is exercised whenever Node is present.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap

import pytest

from textflowkit.ui import app as ui_app

# --- assets -----------------------------------------------------------------


def _example_html() -> str:
    data = ui_app.read_static_asset("streaming-example.html")
    assert data is not None, "streaming-example.html must be packaged"
    return data.decode("utf-8")


def test_the_example_page_is_packaged() -> None:
    assert ui_app.read_static_asset("streaming-example.html") is not None


def test_the_worklet_is_packaged() -> None:
    data = ui_app.read_static_asset("streaming-capture-worklet.js")
    assert data is not None, "streaming-capture-worklet.js must be packaged"
    assert b"registerProcessor" in data


@pytest.mark.parametrize(
    "needle",
    ["cdn.", "googleapis", "google-analytics", "gtag(", "unpkg", "jsdelivr", "cdnjs"],
)
def test_the_example_has_no_cdn_or_analytics(needle: str) -> None:
    # A substring probe of the packaged page and worklet: none of the third-party
    # hosts or analytics entry points appear. It is a probe, not a proof of absence
    # of every conceivable fetcher - it guards against the obvious regressions.
    blob = _example_html()
    worklet = ui_app.read_static_asset("streaming-capture-worklet.js")
    assert worklet is not None
    blob += worklet.decode("utf-8")
    assert needle not in blob.lower()


def test_the_example_loads_no_external_script() -> None:
    html = _example_html()
    # No absolute http(s) script src anywhere in the page.
    assert "src=\"http" not in html
    assert "src='http" not in html
    assert "import(" not in html


def test_the_example_fetches_the_packaged_worklet() -> None:
    # It points at the same-origin packaged asset, not a blob URL.
    html = _example_html()
    assert "/assets/streaming-capture-worklet.js" in html
    assert "createObjectURL" not in html


def test_the_example_never_renders_transcript_as_html() -> None:
    html = _example_html()
    # The transcript is written with text nodes only; nothing is parsed as markup.
    assert "innerHTML" not in html
    assert "insertAdjacentHTML" not in html
    assert "textContent" in html


def test_standalone_example_carries_no_capability() -> None:
    html = ui_app.render_streaming_example(capability="", origin="")
    assert html is not None
    assert "__TFK_CAPABILITY__" not in html  # the placeholder was substituted
    # The meta carries an empty capability in standalone mode.
    assert 'name="textflowkit-capability" content=""' in html


def test_ui_example_embeds_the_ui_capability() -> None:
    html = ui_app.render_streaming_example(capability="cap-token-xyz", origin="http://127.0.0.1:8767")
    assert html is not None
    assert "cap-token-xyz" in html
    # The capability is placed in a meta attribute, never in a URL or an inline script.
    assert 'content="cap-token-xyz"' in html


def test_ui_example_escapes_the_capability() -> None:
    # A capability containing a double quote must not break out of the attribute.
    html = ui_app.render_streaming_example(capability='a"b<c>', origin="")
    assert html is not None
    assert 'a"b<c>' not in html
    assert "&quot;" in html or "&#" in html


# --- the resampler, driven through Node -------------------------------------

_NODE = shutil.which("node")

pytestmark_node = pytest.mark.skipif(_NODE is None, reason="Node is not installed")

# A harness that fabricates the AudioWorkletGlobalScope the module expects, loads
# the packaged resampler, and feeds it a pure tone at a chosen input rate. It
# reports the frame sizes, the sample rate actually achieved, the sign of the
# first sample of a sine, and clipping behaviour.
_HARNESS = textwrap.dedent(
    """
    const fs = require("fs");
    const vm = require("vm");

    const workletPath = process.argv[2];
    const inputRate = Number(process.argv[3]);
    const blockFrames = Number(process.argv[4]);
    const blocks = Number(process.argv[5]);
    const mode = process.argv[6] || "tone";

    const source = fs.readFileSync(workletPath, "utf8");
    const frames = [];             // byte lengths posted
    const posted = [];             // Int16 samples posted
    const acks = [];               // control messages posted (e.g. "flushed")
    let registered = null;

    const sandbox = {
      sampleRate: inputRate,
      AudioWorkletProcessor: class {
        constructor() { this.port = sandbox.__port; }
      },
      registerProcessor: (name, cls) => { registered = cls; },
      Float32Array, Int16Array, Math, Number, console,
      __port: {
        postMessage(msg) {
          // The worklet posts binary PCM (an ArrayBuffer) and, on flush, a
          // control object {type:"flushed"}. Only the binary is audio.
          if (msg && typeof msg === "object" && msg.type !== undefined) {
            acks.push(msg.type);
            return;
          }
          frames.push(msg.byteLength);
          posted.push(Array.from(new Int16Array(msg)));
        },
        onmessage: null,
      },
    };
    vm.createContext(sandbox);
    vm.runInContext(source, sandbox);

    const proc = new registered({ processorOptions: { targetRate: 16000, maxFrameBytes: 32000 } });

    // A 440 Hz sine at the input rate, generated continuously across blocks so a
    // boundary carries no discontinuity the resampler could exploit. A quarter
    // period of phase puts the first sample at the positive peak, so the sign of
    // the first output sample is a definite +1.
    let phase = Math.PI / 2;
    const step = (2 * Math.PI * 440) / inputRate;
    for (let b = 0; b < blocks; b++) {
      const ch = new Float32Array(blockFrames);
      for (let i = 0; i < blockFrames; i++) {
        ch[i] = 0.5 * Math.sin(phase);
        phase += step;
      }
      proc.process([[ch]]);
    }
    // Flush the partial tail, as the page does on stop.
    proc.flush();

    const totalSamples = posted.reduce((n, f) => n + f.length, 0);
    const maxByte = frames.length ? Math.max(...frames) : 0;
    // A 0.5-amplitude sine: the largest magnitude sample should be near 16384.
    let peak = 0;
    for (const f of posted) for (const s of f) peak = Math.max(peak, Math.abs(s));

    const out = {
      registered: registered !== null,
      frames: frames.length,
      maxByte,
      totalSamples,
      peak,
      // Sign of the very first sample: a sine starts positive.
      firstSign: posted.length && posted[0].length ? Math.sign(posted[0][0]) : 0,
      // The flush posts its ack on the same port, after any PCM.
      acks: acks,
    };

    if (mode === "stop_then_more") {
      // After a stop the worklet must emit nothing at all, however many more
      // process() calls come in.
      proc.stop();
      const beforeStop = { frames: frames.length, acks: acks.length };
      let phase2 = 0;
      for (let b = 0; b < blocks; b++) {
        const ch = new Float32Array(blockFrames);
        for (let i = 0; i < blockFrames; i++) { ch[i] = 0.5 * Math.sin(phase2); phase2 += step; }
        proc.process([[ch]]);
      }
      proc.flush();
      out.stoppedFramesAfter = frames.length - beforeStop.frames;
      out.stoppedAcksAfter = acks.length - beforeStop.acks;
    }

    if (mode === "byte_order") {
      // A single positive +0.5 and negative -0.5 sample: the little-endian byte
      // layout must be [0x00,0x40] for 16384 and [0x00,0xC0] for -16384.
      const bytes = new Uint8Array(proc.toBytes(new Float32Array([0.5, -0.5])));
      out.byteOrder = Array.from(bytes);
    }

    if (mode === "stop_and_flush") {
      // The atomic Stop command the page sends: it must stop production *and*
      // emit the held PCM followed by the ack, in that order, and emit nothing
      // afterwards even if more audio blocks arrive.
      frames.length = 0; posted.length = 0; acks.length = 0;
      // Feed a short block so the resampler holds partial PCM that must be flushed.
      const partial = new Float32Array(Math.max(1, blockFrames >> 1));
      for (let i = 0; i < partial.length; i++) { partial[i] = 0.25; }
      proc.process([[partial]]);
      const heldBefore = proc.samples.length;
      proc.port.onmessage({ data: "stop-and-flush" });
      out.stopHeldBefore = heldBefore;
      out.stopFrames = frames.length;
      out.stopAcks = acks.slice();
      out.stoppedAfter = proc.stopped === true;
      // Further audio must produce nothing.
      for (let b = 0; b < blocks; b++) {
        const ch = new Float32Array(blockFrames);
        for (let i = 0; i < blockFrames; i++) { ch[i] = 0.5 * Math.sin(phase); phase += step; }
        proc.process([[ch]]);
      }
      out.stopFramesAfterMore = frames.length;
    }

    console.log(JSON.stringify(out));
    """
)


def _run_harness(input_rate: int, block_frames: int, blocks: int, mode: str = "tone") -> dict:
    import tempfile
    from pathlib import Path

    worklet = ui_app.read_static_asset("streaming-capture-worklet.js")
    assert worklet is not None
    with tempfile.TemporaryDirectory() as tmp:
        wpath = Path(tmp) / "worklet.js"
        wpath.write_bytes(worklet)
        hpath = Path(tmp) / "harness.js"
        hpath.write_text(_HARNESS, encoding="utf-8")
        proc = subprocess.run(
            [_NODE, str(hpath), str(wpath), str(input_rate), str(block_frames), str(blocks), mode],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytestmark_node
@pytest.mark.parametrize("input_rate", [44100, 48000])
def test_resampler_registers_and_produces_frames(input_rate: int) -> None:
    out = _run_harness(input_rate=input_rate, block_frames=128, blocks=400)
    assert out["registered"] is True
    assert out["frames"] >= 1
    assert out["totalSamples"] > 0


@pytestmark_node
@pytest.mark.parametrize("input_rate", [44100, 48000])
def test_resampler_frames_are_at_most_one_second(input_rate: int) -> None:
    out = _run_harness(input_rate=input_rate, block_frames=128, blocks=400)
    # 32000 bytes = 16000 samples = one second of s16le mono at 16 kHz.
    assert out["maxByte"] <= 32000


@pytestmark_node
@pytest.mark.parametrize("input_rate", [44100, 48000])
def test_resampler_hits_sixteen_kilohertz(input_rate: int) -> None:
    # 400 blocks of 128 input frames = 51200 input samples. At 44.1 kHz that is
    # ~1.161 s -> ~18576 output samples; at 48 kHz, ~1.067 s -> ~17066.
    out = _run_harness(input_rate=input_rate, block_frames=128, blocks=400)
    seconds = (400 * 128) / input_rate
    expected = seconds * 16000
    # Allow a few percent for block-boundary carry and rounding.
    assert abs(out["totalSamples"] - expected) / expected < 0.03


@pytestmark_node
@pytest.mark.parametrize("input_rate", [44100, 48000])
def test_resampler_preserves_amplitude_and_sign(input_rate: int) -> None:
    out = _run_harness(input_rate=input_rate, block_frames=128, blocks=400)
    # A 0.5-amplitude sine -> peak near 16384; it must be a real signal, not silence
    # and not clipped to full scale.
    assert 15000 < out["peak"] <= 16384
    assert out["firstSign"] == 1


@pytestmark_node
def test_resampler_is_deterministic() -> None:
    a = _run_harness(input_rate=48000, block_frames=128, blocks=200)
    b = _run_harness(input_rate=48000, block_frames=128, blocks=200)
    assert a == b


# --- the stop/flush handshake and the byte order -----------------------------


@pytestmark_node
@pytest.mark.parametrize("input_rate", [44100, 48000])
def test_flush_acknowledges_on_the_same_port(input_rate: int) -> None:
    # Flush posts the final PCM (if any) and then the ack, on the same port.
    out = _run_harness(input_rate=input_rate, block_frames=128, blocks=400)
    assert out["acks"] == ["flushed"], (
        f"flush must acknowledge exactly once with 'flushed'; got {out['acks']}"
    )


@pytestmark_node
@pytest.mark.parametrize("input_rate", [44100, 48000])
def test_stopped_worklet_emits_no_further_data(input_rate: int) -> None:
    out = _run_harness(input_rate=input_rate, block_frames=128, blocks=50, mode="stop_then_more")
    assert out["stoppedFramesAfter"] == 0, "a stopped worklet posted PCM after stop"
    # It still answers a flush (with no PCM, just the ack), so the page never
    # waits for the timeout after a stop.
    assert out["stoppedAcksAfter"] == 1, "a stopped worklet did not acknowledge a flush"


@pytestmark_node
@pytest.mark.parametrize("input_rate", [44100, 48000])
def test_stop_and_flush_is_atomic_and_stops_production(input_rate: int) -> None:
    """The single Stop command: flush the held PCM, ack, then emit nothing.

    This is the command the page sends on Stop. It must (1) stop production,
    (2) post the held partial PCM, (3) post the ack - in that order - and emit
    no further audio however many blocks arrive afterwards.
    """
    out = _run_harness(input_rate=input_rate, block_frames=128, blocks=50, mode="stop_and_flush")
    assert out["stoppedAfter"] is True, "stop-and-flush did not stop the worklet"
    # Held partial PCM was flushed as one frame, then the ack followed.
    assert out["stopFrames"] == 1, (
        f"stop-and-flush did not post exactly the held PCM frame: {out['stopFrames']}"
    )
    assert out["stopAcks"] == ["flushed"], (
        f"stop-and-flush did not acknowledge exactly once: {out['stopAcks']}"
    )
    assert out["stopFramesAfterMore"] == 1, (
        "a stopped worklet posted PCM after stop-and-flush"
    )


@pytestmark_node
@pytest.mark.parametrize("input_rate", [44100, 48000])
def test_pcm_is_explicitly_little_endian(input_rate: int) -> None:
    out = _run_harness(input_rate=input_rate, block_frames=128, blocks=1, mode="byte_order")
    # +0.5 -> 16384 = 0x4000 -> little-endian bytes 00 40.
    # -0.5 -> -16384 = 0xC000 -> little-endian bytes 00 C0.
    assert out["byteOrder"] == [0x00, 0x40, 0x00, 0xC0], (
        f"PCM is not little-endian: {out['byteOrder']}"
    )
