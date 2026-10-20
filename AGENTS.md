# TextFlowKit agent instructions

Scott's current bounded completion contract governs this work. Follow the active task and repository-required CI; preserve unrelated work and credentials. Keep one concise execution record, reuse unaffected evidence, and distinguish prepared, executed, verified and delivered work. A documented blocker remains incomplete. Do not invent adjacent features or weaken checks.

## Roles and delegation

The main assistant coordinates, plans and oversees. A worker invoked for an implementation unit performs that assigned unit directly; it is not another coordinator. Do not launch nested workers or automatic review/fan-out unless the owner or assignment explicitly authorizes it. Default to serial execution and one release candidate.

## Model selection

Follow Scott's active task selection; do not impose a project-wide Ollama default. His normal substantial-work/coordination preference is gpt-6.1-sol at medium, with gpt-6-luna at xhigh for small bounded work when available. Do not spawn a worker merely to switch models or add providers without authorization.

## Required harness for all TextFlowKit DeepSeek coding (owner directive, 2026-10-19)

Whenever DeepSeek is selected for coding work on TextFlowKit, use native **headless Codex exec through Ollama**, model **`deepseek-v4.1-flash:1017-cloud`**, LOW effort (`model_reasoning_effort = "low"`). This is a standing project instruction, not limited to one release. The observed provider response ID is `deepseek-v4.1-flash:1017`; verify it from transport receipts, not model self-description.

**Do not use `scripts/run_deepseek_worker.py`, another custom agent loop, or Claude Code for this work.** If the approved setup is missing, blocked or broken after restart, inspect/repair it within authorization or report the concrete blocker; never silently fall back, change the model, change effort, or bypass safeguards.

Use the dedicated worker worktree, separate CODEX_HOME/profile/temp directories, allowlisted environment and supported Windows workspace-write sandbox. No personal auth/settings/hooks/MCP configuration copied; no inherited provider/GitHub tokens; no nested workers or publishing. A worktree isolates changes, not all disk reads. Supply a scoped brief through stdin and retain native Codex events, provider model/reasoning evidence and actual tool results.

Read `docs/deepseek-worker.md` before the next DeepSeek unit and after restart. Verified local launcher: `C:\Users\scott\Documents\Codex\2026-09-21\re\outputs\headless-codex-deepseek-20261019\run-codex.ps1`; local state and evidence are alongside it. This selects the harness whenever DeepSeek is used; it does not make DeepSeek the default for unrelated work.

This does not change the application's transcription or AI-cleanup defaults. Whistle remains the default transcription engine.

## Scope and delivery safeguards

Preserve unfinished work. Workers must not push, merge, tag, publish, release, stop unrelated processes, or change global configuration unless that unit is explicitly authorized to do so. The coordinating release task has Scott's push/merge/tag/publication authorization and separately his authorization to complete issue #31. Keep scratch, caches and build outputs outside the checkout. Verify process identity before acting on a PID; record ownership and terminal result for background jobs. Follow enforced controls rather than disguising a rejected action.
