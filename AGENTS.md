# TextFlowKit agent instructions

Scott's current bounded completion contract governs this work. Follow the active task and repository-required CI; preserve unrelated work and credentials. Keep one concise execution record, reuse unaffected evidence, and distinguish prepared, executed, verified and delivered work. A documented blocker remains incomplete. Do not invent adjacent features or weaken checks.

## Roles and delegation

The main assistant coordinates, plans and oversees. A worker invoked for an implementation unit performs that assigned unit directly; it is not another coordinator. Do not launch nested workers or automatic review/fan-out unless the owner or assignment explicitly authorizes it. Default to serial execution and one release candidate.

## Model selection

Follow Scott's active task selection; do not impose a project-wide Ollama default. His normal substantial-work/coordination preference is gpt-6.1-sol at medium, with gpt-6-luna at xhigh for small bounded work when available. Do not spawn a worker merely to switch models or add providers without authorization.

For the explicitly authorized current Whistle release and issue #31 source-recovery work, Scott requested Ollama `deepseek-v4.1-flash:1017-cloud`, reasoning OFF. `scripts/run_deepseek_worker.py` is an explicit opt-in runner for those assigned DeepSeek units: it sends `think: false`, allows no model fallback, and records the actual response model. Ollama currently returns canonical ID `deepseek-v4.1-flash:1017` without the `-cloud` transport suffix. Do not confuse a worker's self-description with the transport receipts. Preserve existing DeepSeek integrations with reasoning off, but do not turn this task-specific instruction into the default for unrelated future work.

This does not change the application's transcription or AI-cleanup defaults. Whistle remains the default transcription engine.

## Scope and delivery safeguards

Preserve unfinished work. Workers must not push, merge, tag, publish, release, stop unrelated processes, or change global configuration unless that unit is explicitly authorized to do so. The coordinating release task has Scott's push/merge/tag/publication authorization and separately his authorization to complete issue #31. Keep scratch, caches and build outputs outside the checkout. Verify process identity before acting on a PID; record ownership and terminal result for background jobs. Follow enforced controls rather than disguising a rejected action.
