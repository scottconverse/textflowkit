// TextFlowKit live-capture AudioWorklet.
//
// This runs on the audio rendering thread. It converts whatever the device
// hands it - any sample rate, any channel count - into the one shape the wire
// protocol accepts: mono signed-16 little-endian at 16 kHz, in frames of at
// most one second.
//
// It is served from this same process (no CDN, no bundler) and loaded with
// `audioWorklet.addModule`, so it is a real module the browser can cache and
// the project can test the same way it runs.
//
// The resampling is *continuous phase*: the fractional read position and the
// one carried input sample are kept across `process()` calls, so a tone that
// straddles a block boundary is neither dropped nor duplicated. A naive
// per-block resample would drift and click at every boundary.
//
// Stopping is explicit and atomic. On Stop the page sends a single
// "stop-and-flush" command. The worklet handles it in one step: stop producing
// audio *first*, then post the last partial PCM (if any), then post the
// acknowledgement `{ type: "flushed" }` - all on the same port, in that order.
// Because the stop and the flush are one command with no gap between them, no
// process() call can run in between and queue audio *after* the flushed PCM, so
// no audio frame can ever follow the page's subsequent `finish`.
//
// The separate "flush" and "stop" commands are kept for the standalone resampler
// harness and remain individually well-defined: "flush" emits the held PCM and
// acks without stopping production; "stop" stops production and drops held PCM.

class TextFlowKitCapture extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opts = (options && options.processorOptions) || {};
    this.targetRate = opts.targetRate || 16000;
    this.maxFrameBytes = opts.maxFrameBytes || 32000;
    // Input samples consumed per one output sample.
    this.ratio = sampleRate / this.targetRate;
    // Fractional position within the input stream, carried across calls.
    this.pos = 0;
    // The last input sample of the previous block, kept so interpolation can
    // reach across the boundary.
    this.tail = new Float32Array(0);
    // Resampled output awaiting a full frame.
    this.samples = [];
    // Once stopped, `process()` emits nothing more. The page sets this after it
    // has the flushed PCM and has sent `finish`, so no frame follows the finish.
    this.stopped = false;
    this.port.onmessage = (event) => {
      const data = event.data;
      if (data === "stop-and-flush") this.stopAndFlush();
      else if (data === "flush") this.flush();
      else if (data === "stop") this.stop();
    };
  }

  // The atomic Stop: stop production *and* emit the last partial PCM as one
  // indivisible step. Because `stopped` is set before the PCM is posted and the
  // ack follows the PCM on the same port, the page learns that no audio can
  // arrive after the frame it just received. There is no window in which a
  // process() call could post a frame between the flush and the ack.
  stopAndFlush() {
    this.stopped = true;
    if (this.samples.length) {
      this.port.postMessage(this.toBytes(this.samples));
    }
    this.samples = [];
    this.tail = new Float32Array(0);
    this.port.postMessage({ type: "flushed" });
  }

  // Emit any held PCM, then acknowledge on the same port. The ack is the page's
  // signal that the last audio for this session has been posted.
  flush() {
    if (this.samples.length) {
      this.port.postMessage(this.toBytes(this.samples));
      this.samples = [];
    }
    this.port.postMessage({ type: "flushed" });
  }

  // Stop producing audio. The tail and the held samples are dropped: they are
  // the worklet's own, not the device's, and the page has already flushed.
  stop() {
    this.stopped = true;
    this.samples = [];
    this.tail = new Float32Array(0);
  }

  // Float samples in [-1, 1] to signed 16-bit little-endian, clipped.
  //
  // The bytes are written with an explicit DataView and the little-endian flag,
  // not through an Int16Array's native byte order: the wire format is *defined*
  // as little-endian, so the guarantee must not depend on the host being
  // little-endian.
  toBytes(samples) {
    const view = new DataView(new ArrayBuffer(samples.length * 2));
    for (let i = 0; i < samples.length; i++) {
      let s = samples[i];
      if (s > 1) s = 1;
      else if (s < -1) s = -1;
      // -1 maps to -32768 and +1 to 32767, the standard asymmetry.
      const v = s < 0 ? Math.round(s * 32768) : Math.round(s * 32767);
      view.setInt16(i * 2, v, true /* little-endian */);
    }
    return view.buffer;
  }

  process(inputs) {
    if (this.stopped) return true;
    const input = inputs[0];
    if (!input || input.length === 0) return true;
    const channels = input.length;
    const frames = input[0].length;

    // Down-mix to mono by averaging the channels.
    const mono = new Float32Array(frames);
    for (let i = 0; i < frames; i++) {
      let acc = 0;
      for (let c = 0; c < channels; c++) acc += input[c][i];
      mono[i] = acc / channels;
    }

    // Prepend the carried sample, then interpolate to the target rate.
    const src = this.tail.length ? concat(this.tail, mono) : mono;
    const out = [];
    let pos = this.pos;
    while (pos < src.length - 1) {
      const i0 = Math.floor(pos);
      const frac = pos - i0;
      out.push(src[i0] * (1 - frac) + src[i0 + 1] * frac);
      pos += this.ratio;
    }
    // Carry the unconsumed tail and the fractional phase forward. `keep` is
    // the integer index the tail starts at; the fractional phase is measured
    // relative to it, so the next block resumes exactly where this one stopped.
    const keep = Math.min(Math.max(0, Math.floor(pos)), src.length - 1);
    this.tail = src.slice(keep);
    this.pos = pos - keep;
    for (let j = 0; j < out.length; j++) this.samples.push(out[j]);

    // Emit whole frames of at most `maxFrameBytes` (maxFrameBytes / 2 samples).
    const maxSamples = this.maxFrameBytes / 2;
    while (this.samples.length >= maxSamples) {
      this.port.postMessage(this.toBytes(this.samples.splice(0, maxSamples)));
    }
    return true;
  }
}

function concat(a, b) {
  const out = new Float32Array(a.length + b.length);
  out.set(a, 0);
  out.set(b, a.length);
  return out;
}

registerProcessor("textflowkit-capture", TextFlowKitCapture);
