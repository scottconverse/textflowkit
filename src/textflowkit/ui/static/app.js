"use strict";
/*
 * TextFlowKit local UI.
 *
 * Plain JavaScript, no framework, no build step, no external request. Talks to
 * the same server that served this page, under /api and /ui, sending the
 * capability header the shell embedded on every mutating call.
 *
 * Two rules this file holds to:
 *  - Untrusted text (transcript segments, error strings, source labels) is set
 *    with textContent, never innerHTML. A transcript is media-derived text and
 *    is treated as data throughout.
 *  - Polling is single-flight per job id. A poll loop is keyed to the job it is
 *    watching and stops when the rendered job changes, so switching jobs cannot
 *    render a stale status over the current one.
 */

(function () {
  const meta = (name) =>
    document.querySelector(`meta[name="textflowkit-${name}"]`)?.content || "";
  const CAPABILITY = meta("capability");
  const ORIGIN = meta("origin");

  const $ = (id) => document.getElementById(id);

  const state = {
    upload: null,      // { upload_id, path, bytes, name }
    source: "",        // a URL typed by the operator
    jobId: null,       // the job currently being watched/rendered
    pollTimer: null,
    pollJob: null,     // which job the active poll loop belongs to
    transcript: null,  // the loaded transcript for the current job
    capabilities: null,
    mediaUrl: null,    // a blob URL for local playback, if available
    mediaEl: null,
    mediaJobId: null,  // the job the current blob URL belongs to, if any
  };

  // --- fetch helpers -----------------------------------------------------

  async function api(path, options = {}) {
    const opts = { ...options };
    opts.headers = { ...(options.headers || {}) };
    if (opts.method && opts.method.toUpperCase() !== "GET") {
      opts.headers["x-textflowkit-ui-capability"] = CAPABILITY;
    }
    const res = await fetch(path, opts);
    return res;
  }

  async function apiJson(path, options) {
    const res = await api(path, options);
    let body = null;
    try { body = await res.json(); } catch (_) { /* non-JSON body */ }
    if (!res.ok) {
      const detail = body && (body.error || body.detail) ? (body.error || body.detail) : `HTTP ${res.status}`;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return body;
  }

  // --- status / messages -------------------------------------------------

  function setStatus(el, message, kind) {
    el.textContent = message || "";
    el.className = el.className.replace(/\b(error|warn)\b/g, "").trim();
    if (kind) el.classList.add(kind);
  }

  // --- capabilities ------------------------------------------------------

  async function loadCapabilities() {
    try {
      const caps = await apiJson("/ui/capabilities");
      state.capabilities = caps;
      $("version-line").textContent = `v${caps.version} · default engine: ${caps.default_engine}`;
      populateEngines(caps);
      const note = $("upload-note");
      note.textContent = `Up to ${formatBytes(caps.max_upload_bytes)} per file.`;
      const asset = caps.assets || {};
      const assetNotice = $("asset-notice");
      if (asset.download_required) {
        assetNotice.textContent =
          `The ${caps.default_engine} engine downloads ${asset.detail} on first use. ` +
          "You will be asked to confirm before the first job that needs it.";
      } else if (asset.downloaded) {
        assetNotice.textContent = `The ${caps.default_engine} engine is present (${asset.detail}).`;
      }
    } catch (err) {
      setStatus($("preflight"), `Could not read capabilities: ${err.message}`, "error");
    }
  }

  function populateEngines(caps) {
    const engineSel = $("opt-engine");
    engineSel.replaceChildren();
    for (const name of caps.engines || []) {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      if (name === caps.default_engine) opt.selected = true;
      engineSel.appendChild(opt);
    }
    refreshModels();
    engineSel.addEventListener("change", refreshModels);
  }

  function refreshModels() {
    const caps = state.capabilities;
    if (!caps) return;
    const engine = $("opt-engine").value;
    const names = (caps.engine_models || {})[engine];
    const modelSel = $("opt-model");
    modelSel.replaceChildren();
    const auto = document.createElement("option");
    auto.value = "";
    auto.textContent = "(engine default)";
    modelSel.appendChild(auto);
    if (Array.isArray(names)) {
      for (const n of names) {
        const opt = document.createElement("option");
        opt.value = n;
        opt.textContent = n;
        modelSel.appendChild(opt);
      }
    }
  }

  // --- source selection --------------------------------------------------

  // A local <audio>/<video> element playing the selected File through a blob
  // URL, so transcript timestamps can seek the *current local* file. It is shown
  // inside the visible #player container with native controls, so a long local
  // file is never playing unstoppably and invisibly: the operator can see it and
  // pause or stop it. The object URL is revoked and the element removed whenever
  // the selection changes, so we never leak one per file.
  function clearLocalMedia() {
    if (state.mediaEl) {
      try { state.mediaEl.pause(); } catch (_) { /* not playing */ }
      state.mediaEl.removeAttribute("src");
      try { state.mediaEl.load(); } catch (_) { /* detached */ }
      try { state.mediaEl.remove(); } catch (_) { /* already detached */ }
    }
    if (state.mediaUrl) {
      try { URL.revokeObjectURL(state.mediaUrl); } catch (_) { /* already gone */ }
    }
    state.mediaEl = null;
    state.mediaUrl = null;
    state.mediaJobId = null;
    const player = $("player");
    if (player) {
      player.replaceChildren();
      player.hidden = true;
    }
  }

  function setLocalMedia(file) {
    clearLocalMedia();
    if (!file) return;
    try {
      state.mediaUrl = URL.createObjectURL(file);
      const el = document.createElement(file.type && file.type.startsWith("video") ? "video" : "audio");
      el.preload = "metadata";
      el.src = state.mediaUrl;
      // Native controls so playback can be paused or stopped from the page, and
      // an accessible name so the control is identifiable to a screen reader.
      el.controls = true;
      el.setAttribute("aria-label", `Local media preview: ${file.name}`);
      const player = $("player");
      if (player) {
        player.replaceChildren(el);
        player.hidden = false;
      }
      state.mediaEl = el;
    } catch (_) {
      // No blob URL (rare): leave mediaEl null and the timestamps stay select-only.
      clearLocalMedia();
    }
  }

  function setUpload(file) {
    state.upload = null;
    state.source = "";
    $("source-input").value = "";
    setStatus($("preflight"), "");
    if (file) {
      $("upload-note").textContent =
        `${file.name} — ${formatBytes(file.size)}. Press Transcribe to upload.`;
      state.pendingFile = file;
      setLocalMedia(file);
    } else {
      clearLocalMedia();
    }
  }

  async function uploadFile(file) {
    const progress = $("upload-progress");
    const bar = $("upload-bar");
    const text = $("upload-progress-text");
    progress.hidden = false;
    bar.value = 0;
    text.textContent = "Uploading…";

    // fetch with the File as the body streams it; the browser sends
    // Content-Length, and the server caps the size while reading.
    const res = await fetch("/ui/uploads", {
      method: "POST",
      headers: {
        "x-textflowkit-ui-capability": CAPABILITY,
        // The filename goes in a header, and HTTP header values must be Latin-1:
        // a `fetch` Headers object throws on a non-Latin-1 name such as
        // "meeting-中文.wav". Encode it (percent-encoding is pure ASCII) and let
        // the server decode it back to the real name.
        "x-textflowkit-filename": encodeURIComponent(file.name),
        "content-type": "application/octet-stream",
      },
      body: file,
    });
    if (!res.ok) {
      let msg = `HTTP ${res.status}`;
      try { const b = await res.json(); if (b.error) msg = b.error; } catch (_) {}
      progress.hidden = true;
      throw new Error(msg);
    }
    const body = await res.json();
    bar.value = 100;
    text.textContent = "Uploaded.";
    // The file is staged now, so the note must stop telling the operator to press
    // Transcribe *to upload*. Show the staged name and size instead.
    $("upload-note").textContent =
      `${file.name} — ${formatBytes(file.size)} uploaded.`;
    setTimeout(() => { progress.hidden = true; }, 600);
    return body;
  }

  // --- submit ------------------------------------------------------------

  function selectedFormats() {
    return Array.from(document.querySelectorAll('input[name="format"]:checked'))
      .map((el) => el.value);
  }

  function buildSubmission() {
    const payload = {
      language: $("opt-language").value.trim() || null,
      formats: selectedFormats(),
      engine: $("opt-engine").value || undefined,
      model: $("opt-model").value || null,
      device: $("opt-device").value || null,
      diarize: $("opt-diarize").checked,
      translate_to: $("opt-translate").value.trim() || null,
    };
    const outDir = $("opt-output-dir").value.trim();
    if (outDir) payload.output_dir = outDir;
    return payload;
  }

  async function maybeConfirmAssetDownload() {
    const caps = state.capabilities;
    if (!caps || !caps.assets || !caps.assets.download_required) return true;
    const dialog = $("asset-dialog");
    $("asset-dialog-text").textContent =
      `This looks like the first run. The ${caps.default_engine} engine will ` +
      `download ${caps.assets.detail}. This can take a while and needs network ` +
      "access. Continue?";
    return await new Promise((resolve) => {
      dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), { once: true });
      dialog.showModal();
    });
  }

  async function submitJob() {
    const button = $("transcribe");
    setStatus($("preflight"), "");
    button.disabled = true;
    try {
      const payload = buildSubmission();
      let fromLocalFile = false;

      if (state.pendingFile && !state.source) {
        const confirmed = await maybeConfirmAssetDownload();
        if (!confirmed) return;
        const caps = state.capabilities;
        if (state.pendingFile.size > (caps?.max_upload_bytes || Infinity)) {
          throw new Error(`File is larger than the ${formatBytes(caps.max_upload_bytes)} limit.`);
        }
        const uploaded = await uploadFile(state.pendingFile);
        state.upload = uploaded;
        state.pendingFile = null;
        payload.mode = "upload";
        payload.upload_id = uploaded.upload_id;
        fromLocalFile = true;
      } else if (state.source) {
        const confirmed = await maybeConfirmAssetDownload();
        if (!confirmed) return;
        payload.mode = "source";
        payload.source = state.source;
      } else {
        throw new Error("Choose a file or enter a URL first.");
      }

      const job = await apiJson("/ui/jobs", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload),
      });
      // Tie the current blob URL to this job only when it came from the selected
      // local file, so timestamps keep seeking for that file; a URL job has none.
      state.mediaJobId = fromLocalFile && state.mediaEl ? job.id : null;
      watchJob(job.id);
      loadRecent();
    } catch (err) {
      setStatus($("preflight"), err.message, "error");
    } finally {
      button.disabled = false;
    }
  }

  // --- job watching ------------------------------------------------------

  function watchJob(jobId) {
    state.jobId = jobId;
    state.transcript = null;
    stopPolling();
    // The blob URL belongs to the file the operator just selected. Keep it only
    // while we are watching the job that file produced; watching any other job
    // (a recent job, a URL job) drops it, so a timestamp can never seek the wrong
    // media.
    if (state.mediaJobId !== jobId) clearLocalMedia();
    $("transcript").replaceChildren();
    // Stale search hits belong to the job they were searched against; clear and
    // hide them so matches from another job cannot linger over this one.
    const searchBox = $("search-results");
    searchBox.replaceChildren();
    searchBox.hidden = true;
    setStatus($("results-status"), "");
    pollJob(jobId);
  }

  function stopPolling() {
    if (state.pollTimer) {
      clearTimeout(state.pollTimer);
      state.pollTimer = null;
    }
    state.pollJob = null;
  }

  async function pollJob(jobId) {
    // Single-flight per job id: if the watched job changed while we were away,
    // stop rather than render a status for a job the workspace no longer shows.
    if (state.jobId !== jobId) return;
    state.pollJob = jobId;
    let job;
    try {
      job = await apiJson(`/api/jobs/${encodeURIComponent(jobId)}`);
    } catch (err) {
      if (state.jobId !== jobId) return;
      setStatus($("results-status"), `Lost contact with the server: ${err.message}`, "error");
      state.pollTimer = setTimeout(() => pollJob(jobId), 3000);
      return;
    }
    if (state.jobId !== jobId) return; // a newer job took over while fetching
    renderJob(job);

    if (job.state === "pending" || job.state === "running") {
      state.pollTimer = setTimeout(() => pollJob(jobId), 1500);
    } else {
      state.pollJob = null;
      if (job.state === "done") {
        await loadTranscript(jobId);
      }
      loadRecent();
    }
  }

  const STAGE_LABEL = {
    starting: "Starting",
    fetching: "Fetching media",
    downloading: "Downloading media",
    decoding: "Decoding audio",
    transcribing: "Transcribing",
    diarizing: "Identifying speakers",
    translating: "Translating",
    rendering: "Writing output files",
    cancelling: "Stopping…",
    cancelled: "Cancelled",
    complete: "Complete",
    failed: "Failed",
    interrupted: "Interrupted",
  };

  function renderJob(job) {
    const box = $("current-job");
    box.replaceChildren();
    const stage = STAGE_LABEL[job.state] || STAGE_LABEL[job.progress] || job.progress || job.state;
    const stageLine = document.createElement("p");
    stageLine.className = "stage-line";
    // textContent: progress strings are server text and treated as data.
    stageLine.textContent = job.state === "running" ? `${stage}…` : stage;
    box.appendChild(stageLine);

    const meta = document.createElement("p");
    meta.className = "stage-meta";
    meta.textContent = `job ${job.id} · ${job.state}`;
    box.appendChild(meta);

    if (job.error) {
      const err = document.createElement("p");
      err.className = "preflight error";
      err.textContent = job.error;
      box.appendChild(err);
    }

    const actions = $("current-actions");
    actions.hidden = false;
    const terminal = ["done", "error", "cancelled"].includes(job.state);
    $("cancel-job").disabled = terminal;
    $("cancel-job").textContent = job.cancel_requested ? "Stopping…" : "Cancel";
    // Resume is offered for interrupted/error/cancelled jobs that carry a saved
    // request; the server refuses it otherwise and we surface that message.
    $("resume-job").hidden = !(job.state === "error" || job.state === "cancelled");
  }

  async function cancelCurrent() {
    if (!state.jobId) return;
    try {
      await apiJson(`/api/jobs/${encodeURIComponent(state.jobId)}/cancel`, { method: "POST" });
    } catch (err) {
      setStatus($("results-status"), err.message, "error");
    }
  }

  // --- controlled shutdown ----------------------------------------------

  async function requestShutdown() {
    const dialog = $("stop-dialog");
    const confirmed = await new Promise((resolve) => {
      dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), { once: true });
      dialog.showModal();
    });
    if (!confirmed) return;
    const status = $("stop-status");
    setStatus(status, "Stopping… any running job is drained, not killed.");
    try {
      await apiJson("/ui/shutdown", { method: "POST" });
      setStatus(status, "Server is stopping. You can close this tab.");
      $("stop-server").disabled = true;
    } catch (err) {
      setStatus(status, err.message, "error");
    }
  }

  async function resumeCurrent() {
    if (!state.jobId) return;
    try {
      await apiJson(`/api/jobs/${encodeURIComponent(state.jobId)}/resume`, { method: "POST" });
      pollJob(state.jobId);
    } catch (err) {
      setStatus($("results-status"), err.message, "error");
    }
  }

  // --- transcript --------------------------------------------------------

  async function loadTranscript(jobId) {
    // Page if needed so an hour-long meeting is never truncated. The server
    // caps inline JSON pages under the production profile; read the first page,
    // then keep requesting while has_more is true.
    const segments = [];
    let offset = 0;
    const limit = 500;
    let meta = null;
    for (;;) {
      const page = await apiJson(
        `/api/jobs/${encodeURIComponent(jobId)}/transcript?format=json&offset=${offset}&limit=${limit}`
      );
      if (state.jobId !== jobId) return; // job changed mid-load
      meta = page;
      const tr = page.transcript || {};
      const batch = tr.segments || [];
      segments.push(...batch);
      if (!page.has_more || batch.length === 0) break;
      offset += batch.length;
    }
    state.transcript = { ...(meta?.transcript || {}), segments };
    renderTranscript();
    setStatus($("results-status"), `${segments.length} segments loaded.`);
  }

  function renderTranscript() {
    const container = $("transcript");
    container.replaceChildren();
    const segs = state.transcript?.segments || [];
    for (const seg of segs) {
      container.appendChild(segmentNode(seg));
    }
  }

  function segmentNode(seg) {
    const row = document.createElement("div");
    row.className = "segment";
    row.dataset.start = String(seg.start);

    const ts = document.createElement("button");
    ts.type = "button";
    ts.className = "ts" + (state.mediaEl ? "" : " static");
    ts.textContent = formatTimestamp(seg.start);
    if (state.mediaEl) {
      ts.title = "Seek to this point in the local media";
      ts.addEventListener("click", () => seekTo(seg.start));
    } else {
      // No local media: honest about it. The control selects the row rather
      // than pretending to play something it does not have.
      ts.title = "No local playback available for this source";
      ts.addEventListener("click", () => row.scrollIntoView({ block: "center" }));
    }
    row.appendChild(ts);

    const text = document.createElement("div");
    text.className = "text";
    if (seg.speaker) {
      const sp = document.createElement("span");
      sp.className = "speaker";
      sp.textContent = seg.speaker + ":";
      text.appendChild(sp);
    }
    const body = document.createElement("span");
    // textContent, always: transcript text is untrusted media-derived content.
    body.textContent = seg.translated_text || seg.text || "";
    text.appendChild(body);
    row.appendChild(text);
    return row;
  }

  function seekTo(seconds) {
    if (!state.mediaEl) return;
    try {
      state.mediaEl.currentTime = Math.max(0, Number(seconds) || 0);
      state.mediaEl.play().catch(() => {});
    } catch (_) { /* seeking a not-yet-loaded element is fine to ignore */ }
  }

  async function searchTranscript() {
    const q = $("search-input").value.trim();
    const box = $("search-results");
    box.replaceChildren();
    if (!q || !state.jobId) { box.hidden = true; return; }
    try {
      const res = await apiJson(
        `/api/jobs/${encodeURIComponent(state.jobId)}/search?q=${encodeURIComponent(q)}&limit=100&context=1`
      );
      box.hidden = false;
      const hits = res.matches || [];
      if (hits.length === 0) {
        const p = document.createElement("p");
        p.className = "hint";
        p.textContent = "No matches.";
        box.appendChild(p);
        return;
      }
      for (const hit of hits) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "search-hit";
        const ts = document.createElement("span");
        ts.className = "ts";
        ts.textContent = formatTimestamp(hit.start);
        b.appendChild(ts);
        const t = document.createElement("span");
        t.textContent = hit.text || "";
        b.appendChild(t);
        b.addEventListener("click", () => jumpToSegment(hit.start));
        box.appendChild(b);
      }
    } catch (err) {
      setStatus($("results-status"), err.message, "error");
    }
  }

  function jumpToSegment(start) {
    const rows = $("transcript").querySelectorAll(".segment");
    for (const row of rows) {
      if (Number(row.dataset.start) >= Number(start)) {
        row.scrollIntoView({ block: "center" });
        row.style.outline = "3px solid var(--focus)";
        setTimeout(() => { row.style.outline = ""; }, 1500);
        return;
      }
    }
  }

  async function copyText() {
    const segs = state.transcript?.segments || [];
    const text = segs.map((s) => s.translated_text || s.text || "").join("\n");
    if (!text) {
      setStatus($("results-status"), "Nothing to copy yet.", "warn");
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      setStatus($("results-status"), "Transcript copied to the clipboard.");
    } catch (_) {
      setStatus($("results-status"), "Clipboard was not available; select the text and copy manually.", "warn");
    }
  }

  function downloadTranscript() {
    if (!state.jobId) {
      setStatus($("results-status"), "No finished job to download.", "warn");
      return;
    }
    const fmt = $("download-format").value;
    // Navigate to the fixed-format endpoint; the browser handles the download.
    const url = `/ui/jobs/${encodeURIComponent(state.jobId)}/download?format=${encodeURIComponent(fmt)}`;
    const a = document.createElement("a");
    a.href = url;
    a.download = "";
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  // --- recent jobs -------------------------------------------------------

  async function loadRecent() {
    try {
      const res = await apiJson("/ui/jobs?limit=15");
      const list = $("recent-list");
      list.replaceChildren();
      for (const job of res.jobs || []) {
        const li = document.createElement("li");
        const b = document.createElement("button");
        b.type = "button";
        b.className = "recent-item";
        const label = document.createElement("span");
        // display_source is the original upload name (from the UI's sidecar), a
        // URL, or a basename - never the opaque staged path. Untrusted text:
        // textContent, always.
        label.textContent = shorten(job.display_source || job.source || job.id, 42);
        const st = document.createElement("span");
        st.className = "state " + (["done", "error", "cancelled"].includes(job.state) ? job.state : "");
        st.textContent = job.state;
        b.append(label, st);
        b.addEventListener("click", () => watchJob(job.id));
        li.appendChild(b);
        list.appendChild(li);
      }
      if (!(res.jobs || []).length) {
        const li = document.createElement("li");
        li.className = "hint";
        li.textContent = "No jobs yet.";
        list.appendChild(li);
      }
    } catch (_) { /* recent list is a convenience; a failure is not fatal */ }
  }

  // --- utilities ---------------------------------------------------------

  function formatBytes(n) {
    if (n === Infinity || n === null || n === undefined) return "unlimited";
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let v = Number(n), i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return `${v % 1 === 0 ? v : v.toFixed(1)} ${units[i]}`;
  }

  function formatTimestamp(seconds) {
    const s = Math.max(0, Math.floor(Number(seconds) || 0));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    const mm = String(m).padStart(2, "0");
    const ss = String(sec).padStart(2, "0");
    return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
  }

  function shorten(text, max) {
    const t = String(text || "");
    return t.length > max ? t.slice(0, max - 1) + "…" : t;
  }

  // --- wiring ------------------------------------------------------------

  function wire() {
    const drop = $("drop-zone");
    const fileInput = $("file-input");

    drop.addEventListener("click", () => fileInput.click());
    drop.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); }
    });
    fileInput.addEventListener("change", () => {
      // Copy the File out before clearing the input. A file input keeps its last
      // selection, so re-picking the *same* file after switching sources (to a
      // URL) fires no change at all and the source silently stays a URL — the
      // operator's chosen file is ignored. Resetting value="" after reading the
      // File makes the next pick of any file, same or different, raise change.
      const file = fileInput.files && fileInput.files[0];
      if (file) setUpload(file);
      fileInput.value = "";
    });

    ["dragenter", "dragover"].forEach((ev) =>
      drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("dragover"); })
    );
    ["dragleave", "drop"].forEach((ev) =>
      drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("dragover"); })
    );
    drop.addEventListener("drop", (e) => {
      const f = e.dataTransfer?.files?.[0];
      if (f) setUpload(f);
    });

    $("source-use").addEventListener("click", () => {
      const url = $("source-input").value.trim();
      if (url) {
        state.source = url;
        state.pendingFile = null;
        // A URL source has no local file to play, so drop the blob URL and any
        // seek target it provided.
        clearLocalMedia();
        renderTranscript();
        setStatus($("preflight"), `Using URL: ${shorten(url, 60)}`);
      }
    });
    $("source-input").addEventListener("input", () => {
      state.source = $("source-input").value.trim();
      if (state.source) {
        state.pendingFile = null;
        if (state.mediaEl) { clearLocalMedia(); renderTranscript(); }
      }
    });

    $("transcribe").addEventListener("click", submitJob);
    $("cancel-job").addEventListener("click", cancelCurrent);
    $("resume-job").addEventListener("click", resumeCurrent);
    $("search-button").addEventListener("click", searchTranscript);
    $("search-input").addEventListener("keydown", (e) => { if (e.key === "Enter") searchTranscript(); });
    $("copy-button").addEventListener("click", copyText);
    $("download-button").addEventListener("click", downloadTranscript);
    $("stop-server").addEventListener("click", requestShutdown);

    const advToggle = $("advanced-toggle");
    advToggle.addEventListener("click", () => {
      const expanded = advToggle.getAttribute("aria-expanded") === "true";
      advToggle.setAttribute("aria-expanded", String(!expanded));
      $("advanced-body").hidden = expanded;
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    wire();
    loadCapabilities();
    loadRecent();
  });
})();
