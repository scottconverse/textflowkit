"""Behavioural tests for the browser example's session lifecycle.

These run the *packaged page's own inline script* under Node, inside a fake DOM
and a fake audio/WebSocket environment, and assert how the page behaves when
things happen out of order: a slow microphone permission grant, a second Start
during the wait, a Cancel while the grant is still pending, a socket that closes
before the server is ready, a worklet that loads after the session was already
cancelled, and a stale socket's callbacks firing after a newer session began.

The point is the stop path in particular. The page must, on "Stop and finish":
stop the capture *immediately*, hand the worklet one atomic ``stop-and-flush``
command that stops production and posts the last partial PCM followed by its
acknowledgement on the same port, send that final PCM, and only then send
``finish`` - with no audio frame sent after ``finish``. An unacknowledged flush
is a failure: the session is cancelled and cleaned up, never finished as if it
had succeeded. The socket is retained after ``finish`` to receive the server's
final. These tests pin the corrected behaviour.

No microphone, no real audio device, no network, and no real time are involved:
``getUserMedia`` is a deferred fake, the socket is an in-process fake, the
worklet port is a scripted fake, and timers are the test's to fire.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

from textflowkit.ui import app as ui_app

_NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(_NODE is None, reason="Node is not installed")


# The harness. It extracts the page's inline <script> verbatim (so the code under
# test is exactly the code the browser runs), evaluates it against a fabricated
# global environment, and drives a named scenario, printing a JSON result the
# Python side asserts on.
_HARNESS = textwrap.dedent(
    r"""
    "use strict";
    const fs = require("fs");
    const vm = require("vm");

    const htmlPath = process.argv[2];
    const scenario = process.argv[3];
    // Optional 4th arg: JSON meta overrides applied *before* the page script is
    // evaluated, because the page reads its config metas once at load.
    const metaOverride = process.argv[4] ? JSON.parse(process.argv[4]) : {};
    // The packaged worklet asset, so the fake addModule can register the same
    // processor name a real context would.
    const WORKLET_PATH = process.argv[5] || "";
    const html = fs.readFileSync(htmlPath, "utf8");

    // --- extract the inline page script verbatim ------------------------------
    const m = html.match(/<script>([\s\S]*?)<\/script>/);
    if (!m) { console.log(JSON.stringify({ error: "no inline script" })); process.exit(0); }
    const pageSource = m[1];

    // --- a tiny deterministic event loop --------------------------------------
    // The page posts a "ready" event whenever a timer is scheduled or a promise
    // settles; the driver loops on it and fires timers. Time is virtual: timers
    // fire only when the scenario advances the clock past their due time, so a
    // long "ready" timeout does not fire unless the scenario wants it to.
    const timers = [];
    let timerId = 0;
    let virtualNow = 0;
    function setTimeout(fn, ms) {
      const id = ++timerId;
      timers.push({ id, due: virtualNow + (ms || 0), fn });
      return id;
    }
    function clearTimeout(id) { for (let i = 0; i < timers.length; i++) if (timers[i].id === id) timers.splice(i, 1); }
    // Advance virtual time to `ms` (absolute) and fire every timer now due.
    function advanceTo(ms) {
      virtualNow = ms;
      let fired = true;
      while (fired) {
        fired = false;
        timers.sort((a, b) => a.due - b.due);
        while (timers.length && timers[0].due <= virtualNow) {
          const t = timers.shift();
          t.fn();
          fired = true;
        }
      }
    }

    // --- fake DOM -------------------------------------------------------------
    function makeElement(tag) {
      return {
        tagName: tag, textContent: "", className: "", hidden: false, disabled: false,
        value: "", style: {}, children: [],
        _ev: {},
        addEventListener(type, fn) { (this._ev[type] = this._ev[type] || []).push(fn); },
        dispatch(type) {
          // A browser does not deliver a click to a disabled control.
          if (type === "click" && this.disabled) { rec("click_on_disabled", { id: this.id }); return; }
          (this._ev[type] || []).forEach((fn) => fn({ type }));
        },
        appendChild(c) { this.children.push(c); return c; },
        setAttribute(k, v) { this[k] = v; },
        getAttribute(k) { return k in this ? this[k] : null; },
      };
    }
    const elements = {};
    ["api-token", "token-block", "capability-note", "language", "start", "stop",
     "cancel", "status", "transcript", "level"].forEach((id) => {
      elements[id] = makeElement("div");
      elements[id].id = id;
    });
    elements.start.tagName = "button";
    elements.stop.tagName = "button";
    elements.cancel.tagName = "button";
    elements.language.value = "en";
    // Standalone mode (empty capability meta): the page requires an API token,
    // so the scenario supplies one. This mirrors the operator pasting it in.
    elements["api-token"].value = "test-api-token";

    // The capability is empty for these scenarios (standalone page, no UI). The
    // require-token meta mirrors what the server renders: "1" when the API token
    // is required (standalone, and the production UI).
    const metas = {
      "textflowkit-capability": "",
      "textflowkit-origin": "",
      "textflowkit-require-token": "1",
    };
    Object.assign(metas, metaOverride);

    const log = [];
    function rec(kind, data) { log.push(Object.assign({ k: kind }, data || {})); }

    // --- fakes the scenarios reach into ---------------------------------------
    let pendingGUM = [];      // resolvers for getUserMedia
    let gumCalls = 0;
    const createdStreams = []; // { tracks: [{stop(){}}] , stopped }

    const sockets = [];        // every WebSocket the page created, in order

    function FakeWebSocket(url) {
      this.url = url;
      this.readyState = 0; // CONNECTING
      this.binaryType = "";
      this.bufferedAmount = 0;
      this.sent = [];       // strings/JSON parsed, and binary frames tagged
      this._onopen = null; this._onclose = null; this._onerror = null; this._onmessage = null;
      sockets.push(this);
      rec("socket_new", { url: url });
    }
    FakeWebSocket.CONNECTING = 0; FakeWebSocket.OPEN = 1; FakeWebSocket.CLOSING = 2; FakeWebSocket.CLOSED = 3;
    FakeWebSocket.prototype.send = function (data) {
      if (this.readyState !== 1) { rec("send_when_closed", {}); throw new Error("send on non-open socket"); }
      let tag;
      if (typeof data === "string") {
        let parsed = null; try { parsed = JSON.parse(data); } catch (e) {}
        tag = parsed ? parsed.type : "string";
        this.sent.push({ type: parsed ? parsed.type : "string", msg: parsed });
        rec("ws_send", { socket: this._id, type: parsed ? parsed.type : "string" });
      } else {
        this.sent.push({ type: "binary", bytes: data.byteLength });
        rec("ws_binary", { socket: this._id, bytes: data.byteLength });
      }
    };
    FakeWebSocket.prototype.close = function () {
      this.readyState = 3;
      rec("socket_close_called", { socket: this._id });
      const self = this;
      // A real close fires onclose asynchronously; model it as a timer.
      setTimeout(function () { if (self._onclose) self._onclose({}); }, 0);
    };
    Object.defineProperty(FakeWebSocket.prototype, "onopen", { set(v){ this._onopen = v; }, get(){ return this._onopen; } });
    Object.defineProperty(FakeWebSocket.prototype, "onclose", { set(v){ this._onclose = v; }, get(){ return this._onclose; } });
    Object.defineProperty(FakeWebSocket.prototype, "onerror", { set(v){ this._onerror = v; }, get(){ return this._onerror; } });
    Object.defineProperty(FakeWebSocket.prototype, "onmessage", { set(v){ this._onmessage = v; }, get(){ return this._onmessage; } });

    // Server-side helpers the scenario calls directly.
    function wsOpen(ws) { ws.readyState = 1; if (ws._onopen) ws._onopen({}); }
    function wsMsg(ws, obj) { if (ws._onmessage) ws._onmessage({ data: JSON.stringify(obj) }); }
    function wsClose(ws) { ws.readyState = 3; if (ws._onclose) ws._onclose({}); }

    // --- fake Web Audio -------------------------------------------------------
    const worklets = [];  // created AudioWorkletNodes
    let addModuleResolvers = []; // deferred addModule
    let addModuleCalls = 0;

    function FakeAudioContext() {
      this.state = "suspended";
      this.resumeCount = 0;
      this.closed = false;
      const self = this;
      this.audioWorklet = {
        addModule(url) {
          addModuleCalls++;
          // Actually load the packaged worklet module and record the processor
          // name it registers. A real addModule evaluates the module in the
          // context, making registerProcessor names valid for that context; the
          // harness reads them from the worklet source so the fake can then
          // refuse unregistered names exactly as the browser does.
          self._registered = new Set();
          try {
            const src = fs.readFileSync(WORKLET_PATH, "utf8");
            const re = /registerProcessor\(\s*["']([^"']+)["']/g;
            let m;
            while ((m = re.exec(src))) self._registered.add(m[1]);
          } catch (e) {
            rec("worklet_read_error", { error: String(e) });
          }
          rec("addmodule", { url: url, ctx: self._id, registered: Array.from(self._registered) });
          return new Promise(function (resolve, reject) { addModuleResolvers.push({ resolve, reject, ctx: self }); });
        },
      };
      rec("audioctx_new", { ctx: self._id });
    }
    FakeAudioContext.prototype.resume = function () { this.resumeCount++; return Promise.resolve(); };
    FakeAudioContext.prototype.close = function () { this.closed = true; rec("audioctx_close", {}); return Promise.resolve(); };
    FakeAudioContext.prototype.createMediaStreamSource = function (stream) {
      const src = { _stream: stream, disconnected: false,
        disconnect() { this.disconnected = true; rec("source_disconnect", {}); },
        connect() { rec("source_connect", {}); } };
      rec("source_create", {});
      return src;
    };

    function FakeAudioWorkletNode(ctx, name, opts) {
      // A real browser's AudioWorkletNode constructor refuses a processor name
      // that was never registerProcessor()'d in that context. The harness must
      // model that refusal, otherwise a page/worklet name mismatch (the very
      // defect this test pins) would pass silently in the fake.
      ctx._registered = ctx._registered || new Set();
      if (!ctx._registered.has(name)) {
        rec("worklet_unregistered", { name: name });
        throw new Error(
          "AudioWorkletNode: no processor named '" + name +
          "' is registered in this context"
        );
      }
      this.ctx = ctx; this.name = name; this.opts = opts;
      this.disconnected = false;
      this.connectedTo = null;
      const self = this;
      this.port = {
        onmessage: null,
        posted: [],   // things the page sent to the worklet ("flush"/"stop")
        // the worklet replies by calling this:
        postMessage(msg) {
          self.port.posted.push(msg);
          rec("worklet_send", { msg: msg });
          // Deliver to the page's onmessage handler synchronously is what a port
          // does; the scenario drives explicit replies through workletReply().
        },
      };
      worklets.push(this);
      rec("worklet_new", { name: name });
    }
    FakeAudioWorkletNode.prototype.connect = function (t) { this.connectedTo = t; rec("worklet_connect", {}); };
    FakeAudioWorkletNode.prototype.disconnect = function () { this.disconnected = true; rec("worklet_disconnect", {}); };

    // The page delivers a worklet port message; the scenario calls this.
    function workletReply(node, data, bytes) {
      if (node && node.port.onmessage) node.port.onmessage({ data: bytes !== undefined ? bytes : data });
    }

    // --- build the sandbox -----------------------------------------------------
    const sandbox = {
      console: console,
      Promise: Promise, JSON: JSON, Object: Object, Array: Array, Math: Math,
      Number: Number, Boolean: Boolean, String: String, Error: Error, Date: Date,
      isNaN: isNaN, parseInt: parseInt, setTimeout: setTimeout, clearTimeout: clearTimeout,
      Int16Array: Int16Array, Float32Array: Float32Array, Uint8Array: Uint8Array,
      DataView: DataView, ArrayBuffer: ArrayBuffer,
      WebSocket: FakeWebSocket,
      AudioContext: FakeAudioContext,
      AudioWorkletNode: FakeAudioWorkletNode,
      location: { protocol: "http:", host: "127.0.0.1:8767", pathname: "/streaming-example" },
      navigator: {
        mediaDevices: {
          getUserMedia(constraints) {
            gumCalls++;
            rec("gum_call", { n: gumCalls });
            return new Promise(function (resolve, reject) { pendingGUM.push({ resolve, reject }); });
          },
        },
      },
      document: {
        querySelector(sel) {
          const mm = sel.match(/meta\[name="([^"]+)"\]/);
          if (mm) { const name = mm[1];
            return { getAttribute: () => (name in metas ? metas[name] : null) }; }
          return null;
        },
        getElementById(id) { return elements[id] || null; },
        createElement(tag) { return makeElement(tag); },
        createTextNode(t) { return { nodeType: 3, textContent: t, _text: t }; },
      },
    };
    sandbox.window = sandbox;
    sandbox.window.addEventListener = function (type, fn) { (sandbox.__win_ev = sandbox.__win_ev || {})[type] = fn; };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(pageSource, sandbox);

    // --- drive the event loop until idle --------------------------------------
    // Drain microtasks and fire only timers already due at the current virtual
    // time. Never advances the clock by itself, so a scenario step lands before
    // any long timeout the page armed.
    async function settle(maxSteps) {
      maxSteps = maxSteps || 5000;
      let steps = 0;
      while (steps++ < maxSteps) {
        await Promise.resolve(); await Promise.resolve();
        timers.sort((a, b) => a.due - b.due);
        if (timers.length && timers[0].due <= virtualNow) {
          timers.shift().fn();
          continue;
        }
        break;
      }
      return steps;
    }
    // Advance virtual time by `ms` and fire whatever becomes due (e.g. a
    // ready-timeout or flush-ack timeout the page armed).
    async function advance(ms) {
      advanceTo(virtualNow + (ms || 0));
      await settle();
    }

    function grantMic(stream) {
      const p = pendingGUM.shift();
      if (!p) throw new Error("no pending getUserMedia to grant");
      p.resolve(stream);
    }
    function denyMic(err) {
      const p = pendingGUM.shift();
      if (!p) throw new Error("no pending getUserMedia to deny");
      p.reject(err || new Error("denied"));
    }
    function makeStream() {
      const tracks = [{ stopped: false, stop() { this.stopped = true; rec("track_stop", {}); } }];
      const s = { tracks: tracks, getTracks() { return tracks; } };
      createdStreams.push(s);
      return s;
    }
    function runningTracks() { return createdStreams.reduce((n, s) => n + s.tracks.filter((t) => !t.stopped).length, 0); }

    // Invoke the element's registered click handlers directly, bypassing the
    // disabled gate a real browser applies. This is how we prove the handler
    // *itself* refuses a second Start, rather than relying on the disabled
    // attribute - a programmatic call or a click racing the disable reaches it.
    function invokeHandler(id, type) {
      (elements[id]._ev[type || "click"] || []).forEach((fn) => fn({ type: type || "click" }));
    }

    const result = {};
    function emit(extra) {
      Object.assign(result, extra || {});
      result.log = log;
      result.sockets = sockets.length;
      result.worklets = worklets.length;
      result.workletNames = worklets.map((w) => w.name);
      result.workletNewName = worklets.length ? worklets[worklets.length - 1].name : null;
      result.gumCalls = gumCalls;
      result.addModuleCalls = addModuleCalls;
      result.timersPending = timers.length;
      console.log(JSON.stringify(result));
    }

    async function main() {
      function resolveWorkletModules() {
        while (addModuleResolvers.length) addModuleResolvers.shift().resolve();
      }

      // Common bootstrapping: press Start, grant a mic, open the socket, send
      // ready, let the worklet module load, let capture install. Returns the
      // socket and worklet node. `deferWorklet` leaves addModule pending so a
      // scenario can cancel before capture is installed.
      async function startStreaming(deferWorklet) {
        elements.start.dispatch("click");
        await settle();
        const stream = makeStream();
        grantMic(stream);
        await settle();
        const ws = sockets[sockets.length - 1];
        wsOpen(ws);
        await settle();
        wsMsg(ws, { type: "ready", session: "sess-1" });
        await settle();
        if (!deferWorklet) {
          resolveWorkletModules();
          await settle();
        }
        const node = worklets[worklets.length - 1];
        return { ws, node, stream };
      }

      if (scenario === "stop_flush_ack") {
        const ctx = await startStreaming();
        const ws = ctx.ws, node = ctx.node;
        // Audio is flowing: one whole frame posted and sent.
        workletReply(node, null, new ArrayBuffer(2000));
        await settle();

        // Press Stop. The corrected page must post "flush" to the worklet and
        // NOT send finish yet - it waits for the worklet's acknowledgement.
        elements.stop.dispatch("click");
        await settle();
        const finishBeforeAck = ws.sent.some((s) => s.type === "finish");

        // The worklet posts the last partial PCM, then acknowledges on the same
        // port. The page must send that PCM and then finish.
        workletReply(node, null, new ArrayBuffer(800));
        workletReply(node, { type: "flushed" });
        await settle();

        const sendKinds = ws.sent.map((s) => s.type);
        const finishIdx = sendKinds.indexOf("finish");
        const lastBinary = (() => { let i = -1; sendKinds.forEach((k, idx) => { if (k === "binary") i = idx; }); return i; })();
        const tracksBeforeFinal = runningTracks();
        // The server drains its tail and sends the final transcript + count. Only
        // then must the capture be released.
        wsMsg(ws, { type: "final", event: { committed_delta: "done", pending: "", seq: 3 } });
        await settle();
        emit({
          phase: "after_ack",
          finishSentBeforeAck: finishBeforeAck,
          sendKinds: sendKinds,
          finishIdx: finishIdx,
          lastBinaryIdx: lastBinary,
          binaryAfterFinish: sendKinds.slice(finishIdx + 1).filter((k) => k === "binary").length,
          binaryBytes: ws.sent.filter((s) => s.type === "binary").map((s) => s.bytes),
          workletPosted: (node.port.posted || []).map((x) => (typeof x === "string" ? x : "binary")),
          tracksRunningBeforeFinal: tracksBeforeFinal,
          tracksRunning: runningTracks(),
          status: elements.status.textContent,
        });
        return;
      }

      if (scenario === "no_binary_after_finish_wait") {
        // Even if the worklet keeps posting whole frames before its ack, once
        // finish is sent no binary frame may follow it, and capture must be torn
        // down. After the ack the page also tells the worklet to stop.
        const ctx = await startStreaming();
        const ws = ctx.ws, node = ctx.node;
        workletReply(node, null, new ArrayBuffer(2000));
        await settle();
        elements.stop.dispatch("click");
        await settle();
        workletReply(node, null, new ArrayBuffer(2000));  // a late whole frame
        await settle();
        workletReply(node, null, new ArrayBuffer(400));   // final partial PCM
        workletReply(node, { type: "flushed" });          // then the ack
        await settle();
        // The worklet, buggy or not, tries to keep streaming:
        workletReply(node, null, new ArrayBuffer(2000));
        await settle();
        wsMsg(ws, { type: "final", event: { committed_delta: "done", pending: "", seq: 5 } });
        await settle();
        const sendKinds = ws.sent.map((s) => s.type);
        const finishIdx = sendKinds.indexOf("finish");
        emit({
          sendKinds: sendKinds,
          binaryAfterFinish: sendKinds.slice(finishIdx + 1).filter((k) => k === "binary").length,
          tracksRunning: runningTracks(),
          stopSentToWorklet: (node.port.posted || []).indexOf("stop") !== -1,
        });
        return;
      }

      if (scenario === "double_start") {
        elements.start.dispatch("click");
        await settle();
        // Start must already be disabled synchronously after the first click.
        const disabledRightAfterFirst = elements.start.disabled === true;
        elements.start.dispatch("click"); // a second click while permission pending
        await settle();
        emit({ disabledRightAfterFirst: disabledRightAfterFirst, gumCalls: gumCalls });
        return;
      }

      if (scenario === "cancel_while_pending") {
        elements.start.dispatch("click");
        await settle();
        // Cancel while the permission prompt is still open.
        elements.cancel.dispatch("click");
        await settle();
        // Now the user finally grants; the late stream must be stopped, no socket
        // opened, nothing running.
        grantMic(makeStream());
        await settle();
        emit({
          sockets: sockets.length,
          runningTracks: runningTracks(),
          status: elements.status.textContent,
        });
        return;
      }

      if (scenario === "early_close_rejects_ready") {
        elements.start.dispatch("click");
        await settle();
        grantMic(makeStream());
        await settle();
        const ws = sockets[sockets.length - 1];
        wsOpen(ws);
        await settle();
        // The socket dies before the server ever says "ready".
        wsClose(ws);
        await settle();
        emit({
          status: elements.status.textContent,
          statusError: elements.status.className === "error",
          workletsCreated: worklets.length,
          tracksRunning: runningTracks(),
          startEnabled: elements.start.disabled === false,
        });
        return;
      }

      if (scenario === "late_worklet_after_cancel") {
        const ctx = await startStreaming(true);  // addModule stays pending
        elements.cancel.dispatch("click");
        await settle();
        // The worklet module now finishes loading, after cancel.
        resolveWorkletModules();
        await settle();
        emit({
          workletsCreated: worklets.length,
          workletsConnected: worklets.filter((w) => w.connectedTo).length,
          tracksRunning: runningTracks(),
        });
        return;
      }

      if (scenario === "stale_socket_callbacks") {
        // Session A is running. It is cancelled, then a fresh session B starts.
        // Late callbacks from A's socket must not tear down B.
        const a = await startStreaming();
        elements.cancel.dispatch("click");
        await settle();
        // Session B, fully installed.
        const b = await startStreaming();
        const wsB = b.ws, nodeB = b.node;
        // Now A's old socket fires late callbacks.
        if (a.ws._onclose) a.ws._onclose({});
        if (a.ws._onerror) a.ws._onerror({});
        await settle();
        workletReply(nodeB, null, new ArrayBuffer(2000));
        await settle();
        emit({
          status: elements.status.textContent,
          statusError: elements.status.className === "error",
          stopEnabled: elements.stop.disabled === false,
          tracksRunning: runningTracks(),
          binarySent: wsB.sent.filter((s) => s.type === "binary").length,
        });
        return;
      }

      if (scenario === "happy_path") {
        const ctx = await startStreaming();
        const ws = ctx.ws, node = ctx.node;
        workletReply(node, null, new ArrayBuffer(2000));
        wsMsg(ws, { type: "event", event: { committed_delta: "hello", pending: "wor", seq: 0 } });
        wsMsg(ws, { type: "event", event: { committed_delta: "world", pending: "", seq: 1 } });
        await settle();
        const transcript = elements.transcript.children.map((c) => c.textContent).join("|");
        wsMsg(ws, { type: "final", event: { committed_delta: "done", pending: "", seq: 2 } });
        await settle();
        emit({
          status: elements.status.textContent,
          transcriptChildren: elements.transcript.children.length,
          finalStatus: elements.status.textContent,
        });
        return;
      }

      if (scenario === "token_meta") {
        // Report how the page presents and sends credentials, for the meta config it
        // was loaded with. Drives both the token-required and the token-not-required
        // cases via the meta override passed on argv.
        const tokenBlockHiddenBefore = elements["token-block"].hidden;
        const ctx = await startStreaming();
        const ws = ctx.ws;
        const startMsg = ws.sent.find((s) => s.type === "start");
        emit({
          tokenBlockHiddenBefore: tokenBlockHiddenBefore,
          startIncludesToken: Boolean(startMsg && startMsg.msg && startMsg.msg.token),
          startIncludesCapability: Boolean(startMsg && startMsg.msg && startMsg.msg.capability),
        });
        return;
      }

      if (scenario === "stop_releases_capture_immediately") {
        // On Stop the microphone must be released at once, not held until the
        // server's final. The socket is retained to receive that final.
        const ctx = await startStreaming();
        const ws = ctx.ws;
        workletReply(ctx.node, null, new ArrayBuffer(2000));
        await settle();
        const tracksBefore = runningTracks();
        elements.stop.dispatch("click");
        await settle();
        emit({
          tracksBefore: tracksBefore,
          tracksAfterStop: runningTracks(),
          socketOpenAfterStop: ws.readyState === WebSocket.OPEN,
        });
        return;
      }

      if (scenario === "stop_atomic_command_retains_socket") {
        // One atomic command to the worklet; finish only after the ack; the audio
        // context and worklet port closed then, the socket retained; finish on
        // the same captured socket this generation owns.
        const ctx = await startStreaming();
        const ws = ctx.ws, node = ctx.node;
        workletReply(node, null, new ArrayBuffer(2000));
        await settle();
        elements.stop.dispatch("click");
        await settle();
        const postedAfterStop = (node.port.posted || []).slice();
        const finishBeforeAck = ws.sent.some((s) => s.type === "finish");
        const ctxClosedBeforeAck = node.ctx.closed;
        // The worklet posts the final PCM then the ack (one command, in order).
        workletReply(node, null, new ArrayBuffer(800));
        workletReply(node, { type: "flushed" });
        await settle();
        const sendKinds = ws.sent.map((s) => s.type);
        const finishIdx = sendKinds.indexOf("finish");
        const lastBinary = (() => { let i = -1; sendKinds.forEach((k, idx) => { if (k === "binary") i = idx; }); return i; })();
        emit({
          workletPosted: postedAfterStop.map((x) => (typeof x === "string" ? x : "binary")),
          stopCommandCount: postedAfterStop.filter((x) => x === "stop-and-flush").length,
          finishBeforeAck: finishBeforeAck,
          ctxClosedBeforeAck: ctxClosedBeforeAck,
          finishSent: finishIdx !== -1,
          lastBinaryIdx: lastBinary,
          finishIdx: finishIdx,
          binaryAfterFinish: sendKinds.slice(finishIdx + 1).filter((k) => k === "binary").length,
          ctxClosedAfterAck: node.ctx.closed,
          socketOpenAfterAck: ws.readyState === WebSocket.OPEN,
        });
        return;
      }

      if (scenario === "flush_ack_timeout_is_error") {
        // If the worklet never acknowledges, the stop must NOT be finished as a
        // success: it is an explicit error, the session is cancelled, and the
        // resources are released. No `finish` may be sent.
        const ctx = await startStreaming();
        const ws = ctx.ws, node = ctx.node;
        workletReply(node, null, new ArrayBuffer(2000));
        await settle();
        elements.stop.dispatch("click");
        await settle();
        await advance(3000);  // past the flush-ack timeout
        const sendKinds = ws.sent.map((s) => s.type);
        emit({
          sendKinds: sendKinds,
          finishSent: sendKinds.indexOf("finish") !== -1,
          cancelSent: sendKinds.indexOf("cancel") !== -1,
          status: elements.status.textContent,
          statusError: elements.status.className === "error",
          tracksRunning: runningTracks(),
          timersPending: timers.length,
        });
        return;
      }

      if (scenario === "cancel_clears_generation_timers") {
        // Cancel while a Stop flush is in flight must clear the stop timer (and
        // any other generation-owned timer), leaving none armed.
        const ctx = await startStreaming();
        const ws = ctx.ws, node = ctx.node;
        workletReply(node, null, new ArrayBuffer(2000));
        await settle();
        elements.stop.dispatch("click");   // arms the flush-ack timer
        await settle();
        const timersAfterStop = timers.length;
        elements.cancel.dispatch("click");
        await settle();
        emit({
          timersAfterStop: timersAfterStop,
          timersAfterCancel: timers.length,
        });
        return;
      }

      if (scenario === "stale_stop_timer_cannot_finish_new_session") {
        // Session A: Stop arms a timer. A is then cancelled and a fresh session B
        // is started. A's stale timer/ack must not finish B.
        const a = await startStreaming();
        workletReply(a.node, null, new ArrayBuffer(2000));
        await settle();
        elements.stop.dispatch("click");   // A's flush-ack timer armed
        await settle();
        const wsA = a.ws, nodeA = a.node;
        elements.cancel.dispatch("click");
        await settle();
        // Session B, fully installed.
        const b = await startStreaming();
        const wsB = b.ws;
        // Now A's stale timer fires and A's stale worklet acks, after B is live.
        await advance(5000);               // fire any stale timer
        workletReply(nodeA, { type: "flushed" });
        await settle();
        emit({
          aFinishSent: wsA.sent.some((s) => s.type === "finish"),
          bFinishSent: wsB.sent.some((s) => s.type === "finish"),
          statusError: elements.status.className === "error",
          stopEnabled: elements.stop.disabled === false,
          tracksRunning: runningTracks(),
        });
        return;
      }

      if (scenario === "post_ready_server_error_is_handled") {
        // A server error arriving AFTER "ready" must be displayed and release
        // the session, not be dropped because the handshake already settled.
        const ctx = await startStreaming();
        const ws = ctx.ws;
        wsMsg(ws, { type: "error", code: "STREAMING_OVERLOAD", message: "queue overflow" });
        await settle();
        emit({
          status: elements.status.textContent,
          statusError: elements.status.className === "error",
          tracksRunning: runningTracks(),
          startEnabled: elements.start.disabled === false,
        });
        return;
      }

      if (scenario === "post_ready_socket_error_is_handled") {
        // A socket-level error after ready must also reach the error path.
        const ctx = await startStreaming();
        const ws = ctx.ws;
        if (ws._onerror) ws._onerror({});
        await settle();
        emit({
          status: elements.status.textContent,
          statusError: elements.status.className === "error",
          tracksRunning: runningTracks(),
        });
        return;
      }

      if (scenario === "audio_send_failure_is_not_swallowed") {
        // A send failure while streaming must surface and tear down, not be
        // swallowed while the page still claims to stream.
        const ctx = await startStreaming();
        const ws = ctx.ws, node = ctx.node;
        ws.send = function () { throw new Error("socket write failed"); };
        workletReply(node, null, new ArrayBuffer(2000));
        await settle();
        emit({
          status: elements.status.textContent,
          statusError: elements.status.className === "error",
          tracksRunning: runningTracks(),
        });
        return;
      }

      if (scenario === "double_invoke_start_handler") {
        // Invoke the actual Start handler twice, directly (not by clicking a
        // disabled button): only one microphone must be requested.
        invokeHandler("start");
        await settle();
        const gumAfterFirst = gumCalls;
        invokeHandler("start");   // second direct invocation while permission pending
        await settle();
        emit({
          gumAfterFirst: gumAfterFirst,
          gumCalls: gumCalls,
        });
        return;
      }

      if (scenario === "ready_timeout_is_generous") {
        // The ready wait must tolerate a slow native start. Advancing to 30 s
        // must not time out; only a bound >= 35 s is acceptable.
        elements.start.dispatch("click");
        await settle();
        grantMic(makeStream());
        await settle();
        const ws = sockets[sockets.length - 1];
        wsOpen(ws);
        await settle();
        // No "ready" yet. 30 s later must still be waiting.
        await advance(30000);
        const stillWaiting = elements.status.className !== "error" && ws.readyState === 1;
        // Beyond the bound it must finally time out.
        await advance(20000);
        const timedOut = elements.status.className === "error";
        emit({
          stillWaitingAt30s: stillWaiting,
          timedOutEventually: timedOut,
          status: elements.status.textContent,
        });
        return;
      }

      if (scenario === "cancel_settles_ready_no_timer") {
        // Cancel before "ready" must clear the ready timer and settle the
        // pending promise - no timer armed, nothing left pending.
        elements.start.dispatch("click");
        await settle();
        grantMic(makeStream());
        await settle();
        const ws = sockets[sockets.length - 1];
        wsOpen(ws);
        await settle();
        const timersBefore = timers.length;
        elements.cancel.dispatch("click");
        await settle();
        emit({
          timersBeforeCancel: timersBefore,
          timersAfterCancel: timers.length,
          startEnabled: elements.start.disabled === false,
        });
        return;
      }

      if (scenario === "finish_send_failure_is_error") {
        // The worklet acknowledges the flush, but the socket has already closed
        // (the server/connection vanished) so `finish` cannot be sent. The page
        // must surface an explicit error and release everything - never settle
        // on "Waiting for the final transcript" for a finish that never left.
        const ctx = await startStreaming();
        const ws = ctx.ws, node = ctx.node;
        workletReply(node, null, new ArrayBuffer(2000));
        await settle();
        elements.stop.dispatch("click");
        await settle();
        wsClose(ws);   // the connection is lost before the flush is acknowledged
        await settle();
        workletReply(node, null, new ArrayBuffer(800));
        workletReply(node, { type: "flushed" });
        await settle();
        const sendKinds = ws.sent.map((s) => s.type);
        emit({
          sendKinds: sendKinds,
          finishSent: sendKinds.indexOf("finish") !== -1,
          status: elements.status.textContent,
          statusError: elements.status.className === "error",
          tracksRunning: runningTracks(),
          timersPending: timers.length,
          // The worklet port is detached and the audio context closed on the
          // failure cleanup (recorded by the fakes).
          workletPortDetached: log.some((e) => e.k === "audioctx_close"),
          audioCtxClosed: node.ctx.closed,
          startEnabled: elements.start.disabled === false,
        });
        return;
      }

      if (scenario === "partial_frame_send_on_closed_fails_explicitly") {
        // The Stop click stops capture; the socket then closes; the worklet posts
        // its last partial PCM. The page must NOT silently skip that audio and
        // then send `finish` as if all was well - it must fail explicitly.
        const ctx = await startStreaming();
        const ws = ctx.ws, node = ctx.node;
        workletReply(node, null, new ArrayBuffer(2000));
        await settle();
        elements.stop.dispatch("click");
        await settle();
        wsClose(ws);   // socket gone, no client-side close recorded
        await settle();
        workletReply(node, null, new ArrayBuffer(800));   // last partial PCM, undeliverable
        await settle();
        const sendKinds = ws.sent.map((s) => s.type);
        emit({
          sendKinds: sendKinds,
          finishSent: sendKinds.indexOf("finish") !== -1,
          cancelSent: sendKinds.indexOf("cancel") !== -1,
          status: elements.status.textContent,
          statusError: elements.status.className === "error",
          tracksRunning: runningTracks(),
          timersPending: timers.length,
        });
        return;
      }

      if (scenario === "worklet_processor_name_registered") {
        // End-to-end proof the page asks for a *registered* processor name: the
        // fake AudioWorkletNode refuses any name the packaged worklet did not
        // registerProcessor(), so a page that starts capture at all proves the
        // names match. (This is the defect the earlier fake hid by ignoring the
        // name.)
        const ctx = await startStreaming();
        emit({
          workletsCreated: worklets.length,
          workletName: ctx.node ? ctx.node.name : null,
          registered: Array.from((ctx.node && ctx.node.ctx._registered) || []),
          workletError: log.some((e) => e.k === "worklet_unregistered"),
        });
        return;
      }

      emit({ error: "unknown scenario " + scenario });
    }

    main().then(emit).catch((e) => { emit({ error: String(e && e.stack || e) }); });
    """
)


def _run(scenario: str, metas: dict | None = None) -> dict:
    html = ui_app.read_static_asset("streaming-example.html")
    assert html is not None
    worklet = ui_app.read_static_asset("streaming-capture-worklet.js")
    assert worklet is not None
    with tempfile.TemporaryDirectory() as tmp:
        hpath = Path(tmp) / "page.html"
        hpath.write_bytes(html)
        wpath = Path(tmp) / "worklet.js"
        wpath.write_bytes(worklet)
        spath = Path(tmp) / "harness.js"
        spath.write_text(_HARNESS, encoding="utf-8")
        argv = [_NODE, str(spath), str(hpath), scenario, "{}", str(wpath)]
        if metas is not None:
            argv[4] = json.dumps(metas)
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    assert proc.returncode == 0, f"harness crashed:\n{proc.stderr}\n{proc.stdout}"
    line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "{}"
    out = json.loads(line)
    assert "error" not in out, out.get("error")
    return out


# --- RED: the stop / flush-ack ordering --------------------------------------


def test_stop_does_not_send_finish_before_the_worklet_acknowledges() -> None:
    out = _run("stop_flush_ack")
    assert out["finishSentBeforeAck"] is False, (
        "finish was sent before the worklet acknowledged the flushed PCM; "
        f"send sequence was {out['sendKinds']}"
    )


def test_finish_follows_the_flushed_pcm_and_no_binary_after() -> None:
    out = _run("stop_flush_ack")
    assert out["finishIdx"] != -1, "finish was never sent"
    assert out["binaryAfterFinish"] == 0, (
        f"binary frames were sent after finish: {out['sendKinds']}"
    )
    # The last binary must precede finish (the flushed partial PCM is delivered
    # before the server is told to finish).
    assert out["lastBinaryIdx"] < out["finishIdx"]


def test_stop_tells_the_worklet_to_stop_and_releases_on_final() -> None:
    out = _run("stop_flush_ack")
    # After the ack the last command the page gave the worklet stops it. The
    # command is atomic ("stop-and-flush"), so it both stops production and
    # flushes; there is no separate later "stop" that could race a frame.
    assert out["workletPosted"][-1] == "stop-and-flush", (
        f"the worklet was never handed the atomic stop-and-flush: {out['workletPosted']}"
    )
    # The capture is released by `final`.
    assert out["tracksRunning"] == 0, "the microphone was not released after final"
    assert "Finished" in out["status"], f"unexpected final status: {out['status']!r}"


def test_flush_ack_timeout_is_an_error_never_a_success() -> None:
    """An unacknowledged flush must never be finished as if it had succeeded.

    The old page, on ack timeout, sent ``finish`` and quietly noted the worklet
    "did not confirm". That reports an incomplete stop as a clean one. The page
    must instead surface an explicit error, cancel the session, and release.
    """
    out = _run("flush_ack_timeout_is_error")
    assert out["finishSent"] is False, (
        f"an unacknowledged flush sent finish as if it had succeeded: {out['sendKinds']}"
    )
    assert out["cancelSent"] is True, (
        f"an unacknowledged flush did not cancel the session: {out['sendKinds']}"
    )
    assert out["statusError"] is True, f"the failure was not an error status: {out['status']!r}"
    assert "did not confirm" in out["status"], f"unexpected status: {out['status']!r}"
    assert out["tracksRunning"] == 0, "resources were not released after a failed stop"
    assert out["timersPending"] == 0, "a timer was left armed after a failed stop"


def test_no_binary_after_finish_even_if_the_worklet_keeps_posting() -> None:
    out = _run("no_binary_after_finish_wait")
    assert out["binaryAfterFinish"] == 0, (
        f"binary frames after finish: {out['sendKinds']}"
    )
    assert out["tracksRunning"] == 0


# --- RED: controls and concurrency -------------------------------------------


def test_start_is_disabled_immediately_and_not_twice_requested() -> None:
    out = _run("double_start")
    assert out["disabledRightAfterFirst"] is True, (
        "Start stayed enabled until getUserMedia resolved, so a second click "
        "could request a second microphone"
    )
    assert out["gumCalls"] == 1, f"getUserMedia was called {out['gumCalls']} times"


def test_cancel_during_permission_stops_the_late_stream() -> None:
    out = _run("cancel_while_pending")
    assert out["sockets"] == 0, "a socket was opened after Cancel"
    assert out["runningTracks"] == 0, (
        "Cancel during the permission wait left a late stream's track running"
    )


def test_happy_path_reports_a_numeric_event_count() -> None:
    # The old page concatenated seq with 1 ("Finished. 21" instead of "3"). The
    # count must be a plain number of the events this client received.
    out = _run("happy_path")
    assert out["status"].startswith("Finished. 3 event(s)"), (
        f"event count is not the received count: {out['status']!r}"
    )


# --- RED: an early socket close and a bounded ready wait ---------------------


def test_socket_closed_before_ready_recovers_to_idle() -> None:
    out = _run("early_close_rejects_ready")
    assert out["statusError"] is True, "an early close was not surfaced as an error"
    assert out["tracksRunning"] == 0, "an early close left the microphone running"
    assert out["startEnabled"] is True, "Start was not restored after an early close"


def test_worklet_that_loads_after_cancel_is_not_attached() -> None:
    out = _run("late_worklet_after_cancel")
    assert out["workletsConnected"] == 0, "a worklet loaded after Cancel was connected"
    assert out["tracksRunning"] == 0


def test_stale_socket_callbacks_cannot_tear_down_a_newer_session() -> None:
    out = _run("stale_socket_callbacks")
    assert out["statusError"] is False, (
        f"a stale socket's callback put the new session into an error state: {out['status']!r}"
    )
    assert out["stopEnabled"] is True, "the newer session was torn down by a stale callback"
    assert out["tracksRunning"] == 1, "the newer session's microphone was stopped by a stale callback"
    assert out["binarySent"] >= 1, "the newer session stopped sending audio"


# --- D: the API token is usable when the server requires it ------------------


def test_a_token_requiring_page_shows_the_field_and_sends_the_token() -> None:
    """When the server requires a token, the page must collect and send it.

    The production UI requires *both* the capability and the API token. The old page
    hid the token field whenever it was served by the UI and never put a token in the
    ``start`` message, so the production UI example could never authenticate. The page
    must show the field and carry the token whenever ``textflowkit-require-token`` is
    "1", including inside the UI.
    """
    out = _run(
        "token_meta",
        metas={
            "textflowkit-capability": "ui-capability",
            "textflowkit-require-token": "1",
        },
    )
    assert out["tokenBlockHiddenBefore"] is False, (
        "the token field was hidden even though the server requires a token"
    )
    assert out["startIncludesToken"] is True, (
        "the production UI example never sends the API token it requires"
    )
    assert out["startIncludesCapability"] is True, (
        "the UI capability must still be sent alongside the token"
    )


def test_a_page_that_needs_no_token_hides_the_field_and_sends_none() -> None:
    """With no token configured, the field is hidden and no token is sent.

    A developer UI with no API token authenticates by capability alone; the page must
    not ask for a token it will not send, and must not send an empty one.
    """
    out = _run(
        "token_meta",
        metas={
            "textflowkit-capability": "ui-capability",
            "textflowkit-require-token": "0",
        },
    )
    assert out["tokenBlockHiddenBefore"] is True, (
        "the token field was shown even though the server needs no token"
    )
    assert out["startIncludesToken"] is False, "a token was sent though none is required"
    assert out["startIncludesCapability"] is True


def test_the_example_never_embeds_a_token_value() -> None:
    """No token *value* is ever injected into the page or its asset.

    The page is told *whether* a token is required, never what it is; the operator
    types the value and it lives only in the page's memory.
    """
    html = ui_app.read_static_asset("streaming-example.html")
    assert html is not None
    text = html.decode("utf-8")
    # The page renders a "require" flag, never a token placeholder to be substituted.
    assert "__TFK_REQUIRE_TOKEN__" in text  # the placeholder exists ...
    assert "__TFK_TOKEN__" not in text  # ... but there is no token-value placeholder
    # It reads the flag from a meta attribute, not from a URL query the server fills.
    assert 'name="textflowkit-require-token"' in text


# --- U6: the corrected Stop semantics ----------------------------------------


def test_stop_releases_the_microphone_immediately() -> None:
    """Stop must stop the capture at the click, not hold it until `final`."""
    out = _run("stop_releases_capture_immediately")
    assert out["tracksBefore"] == 1, "the microphone was not running before Stop"
    assert out["tracksAfterStop"] == 0, (
        "Stop did not release the microphone at the click; capture was held"
    )
    assert out["socketOpenAfterStop"] is True, (
        "Stop closed the socket, so the final transcript could never arrive"
    )


def test_stop_uses_one_atomic_command_then_finishes_only_after_the_ack() -> None:
    out = _run("stop_atomic_command_retains_socket")
    # Exactly one command to the worklet, and it is the atomic stop-and-flush.
    assert out["stopCommandCount"] == 1, (
        f"the worklet was not given exactly one stop command: {out['workletPosted']}"
    )
    assert out["finishBeforeAck"] is False, (
        "finish was sent before the worklet acknowledged the flushed PCM"
    )
    assert out["finishSent"] is True, "finish was never sent after the ack"
    assert out["binaryAfterFinish"] == 0, (
        "an audio frame followed finish; the atomic stop did not prevent it"
    )
    assert out["lastBinaryIdx"] < out["finishIdx"], (
        "the flushed PCM did not precede finish"
    )
    # The audio context/worklet port are closed after the ack, the socket retained.
    assert out["ctxClosedBeforeAck"] is False, "the audio context was closed before the flush"
    assert out["ctxClosedAfterAck"] is True, "the audio context stayed open after the flush"
    assert out["socketOpenAfterAck"] is True, "the socket was closed instead of retained for final"


def test_cancel_clears_the_generation_owned_stop_timer() -> None:
    out = _run("cancel_clears_generation_timers")
    assert out["timersAfterStop"] >= 1, "Stop did not arm its flush-ack timer"
    assert out["timersAfterCancel"] == 0, (
        f"Cancel left {out['timersAfterCancel']} timer(s) armed"
    )


def test_a_stale_stop_timer_or_ack_cannot_finish_a_new_session() -> None:
    out = _run("stale_stop_timer_cannot_finish_new_session")
    assert out["aFinishSent"] is False, "a cancelled session's stale stop still sent finish"
    assert out["bFinishSent"] is False, (
        "a stale stop timer/ack sent finish on the NEW session"
    )
    assert out["statusError"] is False, "the new session was put into an error state"
    assert out["stopEnabled"] is True, "the new session was torn down by a stale ack/timer"
    assert out["tracksRunning"] == 1, "the new session's microphone was stopped by a stale ack"


# --- U6: the handshake settlement is separate from lifecycle errors ----------


def test_a_server_error_after_ready_is_displayed_and_releases() -> None:
    """A runtime error message must not be dropped because the handshake settled."""
    out = _run("post_ready_server_error_is_handled")
    assert out["statusError"] is True, (
        f"a post-ready server error was not surfaced: {out['status']!r}"
    )
    assert "STREAMING_OVERLOAD" in out["status"], f"the error code was lost: {out['status']!r}"
    assert out["tracksRunning"] == 0, "a post-ready server error did not release the session"
    assert out["startEnabled"] is True, "Start was not restored after a post-ready error"


def test_a_socket_error_after_ready_is_displayed_and_releases() -> None:
    out = _run("post_ready_socket_error_is_handled")
    assert out["statusError"] is True, (
        f"a post-ready socket error was swallowed: {out['status']!r}"
    )
    assert out["tracksRunning"] == 0, "a post-ready socket error did not release the session"


def test_an_audio_send_failure_is_not_swallowed() -> None:
    out = _run("audio_send_failure_is_not_swallowed")
    assert out["statusError"] is True, (
        f"a failed audio send was swallowed as success: {out['status']!r}"
    )
    assert out["tracksRunning"] == 0, "a failed audio send did not release the session"


# --- U6: independent start guard and a generous ready bound ------------------


def test_the_start_handler_refuses_a_second_invocation() -> None:
    """The handler itself must refuse a second call, not merely a disabled button."""
    out = _run("double_invoke_start_handler")
    assert out["gumAfterFirst"] == 1, "the first Start invocation did not request a microphone"
    assert out["gumCalls"] == 1, (
        f"a second direct Start invocation requested a second microphone "
        f"({out['gumCalls']} microphone requests)"
    )


def test_the_ready_wait_tolerates_a_slow_native_start() -> None:
    out = _run("ready_timeout_is_generous")
    assert out["stillWaitingAt30s"] is True, (
        "the ready wait gave up before 30 s, too tight for a slow native start"
    )
    assert out["timedOutEventually"] is True, (
        "the ready wait never bounds out; it would hang forever"
    )


def test_cancel_before_ready_settles_the_wait_and_clears_its_timer() -> None:
    out = _run("cancel_settles_ready_no_timer")
    assert out["timersBeforeCancel"] >= 1, "no ready timer was armed"
    assert out["timersAfterCancel"] == 0, (
        f"Cancel left {out['timersAfterCancel']} ready timer(s) armed"
    )
    assert out["startEnabled"] is True, "Start was not restored after Cancel"


# --- U7: the worklet processor name is real and shared -----------------------


def test_the_page_instantiates_a_registered_worklet_processor() -> None:
    """The page must ask for the *registered* processor name, not a stale one.

    The fake ``AudioWorkletNode`` in the harness now refuses any name the packaged
    worklet did not ``registerProcessor`` - as a real browser does. The page used
    to pass ``"tfk-capture"`` while the worklet registered
    ``"textflowkit-capture"``; the old, name-agnostic fake hid it, and the example
    would have thrown in a real browser. Capture must install, with the matching
    name, and no ``worklet_unregistered`` error may be raised.
    """
    out = _run("worklet_processor_name_registered")
    assert out["workletError"] is False, (
        "the page instantiated an AudioWorkletNode with an unregistered processor "
        "name; a real browser would reject it"
    )
    assert out["workletsCreated"] == 1, "capture did not install a worklet node"
    assert out["workletName"] == "textflowkit-capture", (
        f"the page used the wrong processor name: {out['workletName']!r}"
    )
    assert "textflowkit-capture" in out["registered"], (
        f"the worklet did not register the name the page used: {out['registered']}"
    )


def test_the_page_and_worklet_agree_on_the_processor_name() -> None:
    """A source-level guard: the name the page passes to ``AudioWorkletNode``
    equals the one the worklet registers, so a future edit to either cannot
    silently diverge. It matches the *constructor argument* specifically, not any
    mention of the string, so a comment or doc line cannot satisfy it.
    """
    import re

    html = ui_app.read_static_asset("streaming-example.html")
    assert html is not None
    worklet = ui_app.read_static_asset("streaming-capture-worklet.js")
    assert worklet is not None
    registered = re.search(r"registerProcessor\(\s*[\"']([^\"']+)[\"']", worklet.decode())
    assert registered is not None, "the worklet never registers a processor"
    page = html.decode("utf-8")
    # The name the page actually hands the constructor: an identifier or literal
    # in the AudioWorkletNode(...) argument. Resolve a bare identifier against its
    # single `var NAME = "..."` assignment so a named constant is followed.
    call = re.search(r"new\s+AudioWorkletNode\(\s*[^,]+,\s*([^,]+),", page)
    assert call is not None, "the page never constructs an AudioWorkletNode"
    arg = call.group(1).strip()
    lit = re.fullmatch(r"[\"']([^\"']+)[\"']", arg)
    if lit:
        used = lit.group(1)
    else:
        const = re.search(rf"var\s+{re.escape(arg)}\s*=\s*[\"']([^\"']+)[\"']", page)
        assert const is not None, f"cannot resolve the processor-name argument {arg!r}"
        used = const.group(1)
    assert used == registered.group(1), (
        "the page constructs an AudioWorkletNode with a different processor name "
        f"than the worklet registers: page {used!r}, worklet {registered.group(1)!r}"
    )


# --- U7: a finish send that cannot happen is an error, not a false wait ------


def test_finish_send_failure_is_an_error_not_a_false_wait() -> None:
    """When ``finish`` cannot be sent, the page must not claim to be waiting.

    The worklet acks the flush, but the socket is already closed, so there is no
    server to send ``finish`` to. The old ``sendFinishOnce`` silently did nothing
    on a closed socket and the page still said "Waiting for the final
    transcript…". That is a false success. The page must surface an explicit
    error, release every owned resource, and restore Start.
    """
    out = _run("finish_send_failure_is_error")
    assert out["finishSent"] is False, "a finish left on a closed socket"
    assert "Waiting" not in out["status"], (
        f"the page claimed to be waiting after a finish that never left: {out['status']!r}"
    )
    assert out["statusError"] is True, (
        f"an unsendable finish was not surfaced as an error: {out['status']!r}"
    )
    assert out["tracksRunning"] == 0, "resources were not released after a failed finish"
    assert out["audioCtxClosed"] is True, "the audio context was left open"
    assert out["timersPending"] == 0, "a timer was left armed after a failed finish"
    assert out["startEnabled"] is True, "Start was not restored after a failed finish"


def test_a_partial_frame_on_a_closed_socket_fails_explicitly() -> None:
    """The last partial PCM must not be silently dropped in favour of ``finish``.

    Stop stops capture; the socket then closes; the worklet posts the final
    partial PCM on the same port. The page cannot deliver that audio. It must fail
    explicitly (error status, cancel where possible, resources released) - never
    skip the audio and send ``finish`` as if the stop had succeeded.
    """
    out = _run("partial_frame_send_on_closed_fails_explicitly")
    assert out["finishSent"] is False, (
        f"the page sent finish after dropping the last partial audio: {out['sendKinds']}"
    )
    assert out["statusError"] is True, (
        f"a dropped final frame was not surfaced as an error: {out['status']!r}"
    )
    assert out["tracksRunning"] == 0, "resources were not released after the failed stop"
    assert out["timersPending"] == 0, "a timer was left armed after the failed stop"
