"use strict";
/* Local proofreading. All model output is plain text; the original UI/export stays original. */
(function () {
  const $ = (id) => document.getElementById(id);
  const capability = document.querySelector('meta[name="textflowkit-capability"]').content;
  let jobId = null, original = null, variant = null, timer = null, generation = 0;
  async function request(path, options = {}) {
    const response = await fetch(path, { ...options, headers: {
      "Content-Type": "application/json", "x-textflowkit-ui-capability": capability,
      ...(options.headers || {}) } });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Cleanup request failed");
    return data;
  }
  const base = () => `/ui/jobs/${encodeURIComponent(jobId)}/cleanup`;
  function status(text) { $("cleanup-status").textContent = text; }
  function showVariant() {
    const box = $("cleanup-comparison");
    box.replaceChildren();
    box.hidden = !variant || $("cleanup-view").value !== "compare";
    if (box.hidden) return;
    let visibleIndex = 0;
    for (const seg of variant.segments) {
      if (seg.hidden) continue;
      const row = document.createElement("div");
      row.className = "cleanup-row";
      const timestamp = document.createElement("span");
      timestamp.textContent = `${seg.start.toFixed(2)}–${seg.end.toFixed(2)}s`;
      const source = document.createElement("div");
      // The shared retrieval API excludes hidden segments. Canonical IDs stay
      // on the variant; visible originals align by their preserved order.
      source.appendChild(document.createTextNode(original.segments[visibleIndex++].text));
      const corrected = document.createElement("div");
      corrected.textContent = seg.text;
      row.append(timestamp, source, corrected);
      box.appendChild(row);
    }
  }
  async function poll(expected, version) {
    try {
      const data = await request(base());
      if (jobId !== expected || version !== generation) return;
      variant = data.variant || null;
      const running = data.state === "running";
      $("cleanup-start").disabled = running || !$("cleanup-model").value;
      $("cleanup-model").disabled = running;
      $("cleanup-copy").disabled = !variant;
      $("cleanup-save").disabled = !variant;
      if (variant) {
        const p = variant.provenance;
        $("cleanup-provenance").textContent =
          `${p.provider} · requested ${p.model_requested} · returned ${p.model_returned} · ${p.created_at} · original SHA-256 ${p.original_sha256}`;
      } else $("cleanup-provenance").textContent = "";
      status(running ? "Proofreading locally… original remains available." :
        data.error || (variant ? "AI correction saved. Review against the original/audio before use." :
          "Optional: select a local model, then proofread."));
      showVariant();
      if (running) timer = setTimeout(() => poll(expected, version), 1500);
    } catch (error) {
      if (jobId === expected && version === generation) {
        status(error.message + " Original remains available. Reopen this job to retry polling.");
        $("cleanup-start").disabled = false;
      }
    }
  }
  async function models(expected, version) {
    try {
      const data = await request("/ui/cleanup/models");
      if (jobId !== expected || version !== generation) return;
      const select = $("cleanup-model");
      select.replaceChildren();
      const blank = document.createElement("option");
      blank.value = "";
      blank.textContent = "Choose an installed local model";
      select.appendChild(blank);
      for (const name of data.models) {
        const option = document.createElement("option");
        option.value = name;
        option.textContent = name;
        select.appendChild(option);
      }
      select.value = data.selected || "";
      $("cleanup-config").textContent = data.message;
      await poll(expected, version);
    } catch (error) {
      if (jobId === expected && version === generation) {
        $("cleanup-config").textContent = error.message;
        // Saved corrections remain readable even when Ollama is offline.
        await poll(expected, version);
      }
    }
  }
  window.addEventListener("textflowkit-transcript", (event) => {
    clearTimeout(timer);
    generation++;
    jobId = event.detail.jobId;
    original = event.detail.transcript;
    variant = null;
    $("cleanup-panel").hidden = !original;
    $("cleanup-comparison").replaceChildren();
    $("cleanup-provenance").textContent = "";
    $("cleanup-start").disabled = true;
    $("cleanup-copy").disabled = true;
    $("cleanup-save").disabled = true;
    $("cleanup-model").disabled = false;
    $("cleanup-model").replaceChildren();
    $("cleanup-view").value = "original";
    if (original) models(jobId, generation);
  });
  $("cleanup-start").addEventListener("click", async () => {
    if (!jobId || !original) return;
    const expected = jobId, version = generation;
    $("cleanup-start").disabled = true;
    status("Checking local model…");
    try {
      await request(base(), {method: "POST", body: JSON.stringify({model: $("cleanup-model").value})});
      if (expected === jobId && version === generation) await poll(expected, version);
    } catch (error) {
      if (expected === jobId && version === generation) {
        status(error.message);
        $("cleanup-start").disabled = !$("cleanup-model").value;
      }
    }
  });
  $("cleanup-model").addEventListener("change", () => {
    $("cleanup-start").disabled = !$("cleanup-model").value;
  });
  $("cleanup-view").addEventListener("change", showVariant);
  $("cleanup-copy").addEventListener("click", async () => {
    if (!variant) return;
    try {
      await navigator.clipboard.writeText(variant.segments.filter(s => !s.hidden).map(s => s.text).join("\n"));
      status("Copied AI-corrected text. Original copy/export is unchanged.");
    } catch (_) { status("Clipboard unavailable; use Save corrected TXT."); }
  });
  $("cleanup-save").addEventListener("click", async () => {
    const expected = jobId, version = generation;
    try {
      const response = await fetch(base() + "/download");
      if (!response.ok) throw new Error("No valid corrected transcript available");
      const text = await response.text();
      if (expected !== jobId || version !== generation) return;
      const url = URL.createObjectURL(new Blob([text], {type: "text/plain;charset=utf-8"}));
      const link = document.createElement("a");
      link.href = url;
      link.download = "transcript-ai-corrected.txt";
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) { status(error.message); }
  });
})();
