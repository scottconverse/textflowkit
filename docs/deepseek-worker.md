# Required DeepSeek coding harness

Owner directive, 2026-10-19: all DeepSeek coding work on TextFlowKit uses native headless Codex exec through Ollama. No custom agent loop or Claude Code. This remains binding after restart unless Scott changes it.

- Exact requested model: `deepseek-v4.1-flash:1017-cloud`. Provider-reported canonical ID: `deepseek-v4.1-flash:1017`.
- Thinking off: `model_reasoning_effort = "none"`; verify actual provider effort and reasoning-token receipts.
- No model/provider/harness fallback. Missing setup is a repair task or blocker, not permission to revert to `scripts/run_deepseek_worker.py`.
- Use dedicated worktree/branch, isolated CODEX_HOME/profile/temp, cleared-and-allowlisted process environment, no copied personal auth/settings/hooks/MCP configuration, and native Windows workspace-write sandbox. Do not bypass enforced controls. This is not a VM or full read-confidentiality boundary.
- Send a scoped brief on stdin. Preserve native JSONL events, stderr, session/metadata, PID ownership and terminal state. Workers do not publish or launch nested workers. Main assistant coordinates/audits; worker implements the assigned unit.

## Local installation on Scott's Windows machine

- Worktree: `C:\Users\scott\Desktop\Code\textflowkit-headless-worker`
- Branch: `worker/headless-codex-deepseek`
- Setup directory: `C:\Users\scott\Documents\Codex\2026-09-21\re\outputs\headless-codex-deepseek-20261019`
- Launcher: `run-codex.ps1` in that directory (Brief, Unit and optional Resume parameters).
- Configuration: `codex-home\config.toml`; model metadata: `codex-home\models.json`.
- Endpoint: `http://127.0.0.1:11434/v1`. IPv6 localhost refused the setup probe; no service configuration was changed.
- Tested Codex: `0.163.0-alpha.10.2`; launcher pins the tested executable path. If an app upgrade removes that path, verify the replacement executable/configuration and affected behavior, preserving the exact model and thinking-off setting. Do not silently revert to another harness.
- Evidence: `verification.json`, `runs\`, `README.md`, current `STATE.md` alongside the launcher.

## Restart checklist

1. Read repository AGENTS.md, this document and the setup STATE.md.
2. Inspect worktree status and the scoped brief; preserve unrelated changes.
3. Check the launcher/config still select exact model, none effort, isolation and sandbox. Reuse unaffected verification evidence; retest changed inputs or unresolved acceptance only.
4. Use native Codex, never the historical Python runner. Report actual provider/model, reasoning setting and tool results separately from configuration.

The old Python runner remains historical repository code; its presence is not authorization to use it. This instruction does not change Whistle, application cleanup models, normal non-DeepSeek preferences, or existing release authorization boundaries.
