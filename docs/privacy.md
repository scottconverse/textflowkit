# No-telemetry policy

TextFlowKit must not send analytics, usage statistics, crash reports, visitor
measurements or diagnostic telemetry. There is no TextFlowKit telemetry opt-in.
Local logs and job/progress data stay local unless the operator deliberately
exports or shares them. This is distinct from necessary functional traffic.

## Release boundary

The pyannote/Hugging Face opt-outs ship in v0.1.16. They are applied before
optional dependencies are imported. v0.1.15 and earlier wheels and Windows
installers are immutable, have no automatic update mechanism, and do not acquire
this source change; install v0.1.16 or later to receive these controls. This says
nothing about older downloads changing themselves. Website settings take effect
independently.

## Application controls

Before loading optional dependencies, TextFlowKit forces process-local opt-outs
for pyannote metrics and Hugging Face telemetry, overriding inherited opt-ins.
The policy does not edit a user's account settings or global machine environment.
These opt-outs do not change Whistle as the default engine, block necessary model or
runtime downloads, or disable Hugging Face authentication for gated models.
Whistle's standalone and streaming child environments already force
`NEEDLE_TELEMETRY=0`, `DO_NOT_TRACK=1` and `CI=1`; keep those overrides.
The upstream Python Needle telemetry SDK is not imported or bundled.

Third-party native code is not physically stripped merely by setting an opt-out.
The checked property is the enforced launch/import policy and relevant dependency
behavior, not a universal proof about every native binary or future dependency.
Optional dependencies must be rechecked when upgraded.

## Traffic that remains necessary

- Downloading the explicitly selected pinned runtime/model assets and optional
  Hugging Face models, including authentication for gated diarization models.
- Acquiring a user-requested media URL through yt-dlp.
- An explicitly requested translation/model call. Remote or cloud backends can
  receive transcript text; choosing them is not an anonymous telemetry channel.
- Local UI/API/MCP requests and explicitly requested local Ollama cleanup.

Opting out of telemetry must not block those functions. Offline mode, source
restrictions, authentication, proxy checks and existing security limits still
apply. No blanket socket block or disable-all-network shortcut implements this
policy.

## Public website

The canonical `www.textflowkit.org` site must serve no analytics beacon, third-party
tracking script or active browser network-error reporting policy. Configure
Cloudflare's RUM and NEL separately, following [site deployment](site-deployment.md).
The local browser UI is a separate app and serves only its packaged assets.
Hosting providers still process requests and may keep operational/security logs;
this project cannot truthfully promise that an external host keeps no logs.

## Unreleased local proofreading uncertainty annotations

Explicit cleanup sends requested source segments and up to 6,000 characters of
read-only neighboring speech to the configured local Ollama service, once per
chunk. It adds no cloud call, automatic second review, telemetry or new provider.
The canonical transcript is unchanged. The separate local cleanup sidecar stores
corrected text, exact uncertainty spans/reasons and provenance; include it in
backups and handle it as transcript data. Reasons use bounded categories rather
than model-written replacement suggestions. Comparison markup uses text nodes;
corrected clipboard and TXT carry review markers and exclude hidden segments.
Ordinary source context may include hidden segments as before; hidden is a display/
export control, not a promise to withhold speech from explicitly requested cleanup.
Older saved variants carry no uncertainty assessment. Local schema checks reject
malformed metadata but cannot guarantee detection of every ambiguity or hallucination.

Reported uncertainty is protected in software: a local deterministic token diff
suppresses edits overlapping valid source flags, including adjacent insertion
boundaries, or conservatively keeps the original segment if mapping is unclear.
This uses the existing response only; it adds no model call or remote processing.
Malformed or ambiguous source flags still reject the attempt. Unflagged model
errors remain possible and require audio review.
