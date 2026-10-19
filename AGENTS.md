# TextFlowKit delegated-worker policy

Scott's instruction, 2026-10-19:

- The coordinating assistant plans, delegates, reviews, and verifies. It does not write product code.
- All delegated work (coding, research, verification, documentation) uses Ollama model **`deepseek-v4.1-flash:1017-cloud`** with **thinking disabled** (`think: false`).
- Do not substitute `deepseek-v4.1-flash:cloud`, an older tag, Luna, Sol, or another provider. If the exact model is unavailable, report the failure rather than silently falling back.
- Use `scripts/run_deepseek_worker.py` for project delegated workers. It pins the requested model and checks the model identity returned by Ollama. Ollama Cloud currently reports the canonical response model as `deepseek-v4.1-flash:1017` (without the transport suffix `-cloud`). Record both identities.
- This is a development-worker policy, not a change to the application's transcription or AI-cleanup model. Whistle remains the default transcription engine.
- Preserve existing unfinished changes. Workers must not push, merge, tag, publish, release, delete unrelated files, or change global user/provider configuration. Keep task output and scratch outside the checkout. The coordinator independently audits each completed unit.
