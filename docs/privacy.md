# No-telemetry policy

TextFlowKit must not send analytics, usage statistics, crash reports, visitor
measurements or diagnostic telemetry. There is no TextFlowKit telemetry opt-in.
Local logs and job/progress data stay local unless the operator deliberately
exports or shares them. This is distinct from necessary functional traffic.

## Release boundary

The added pyannote/Hugging Face guard is unreleased after v0.1.15. Previously
published wheels and Windows installers are immutable and do not acquire this
source change automatically. Website settings take effect independently.

## Application controls

Before loading optional dependencies, TextFlowKit forces process-local opt-outs
for pyannote metrics and Hugging Face telemetry, overriding inherited opt-ins.
The policy does not edit a user's account settings or global machine environment.
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
