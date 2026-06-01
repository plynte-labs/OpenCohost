# VoiceAI Agent Guide

## Memory First

- Before planning or coding, recover Engram context for project `voiceai` with `mem_context` first, then `mem_search` when needed.
- Treat recovered memories as constraints, especially offline TTS behavior, cached model paths, Hugging Face offline mode, Qwen3-TTS startup, and timeout decisions.
- Save meaningful decisions, discoveries, bug fixes, and completed-track summaries back to Engram with `mem_save` or `mem_session_summary`.

## Known Project Context

- VoiceAI has prior work to make TTS usable without internet after the first model download.
- Cached Qwen3-TTS models are under `E:\VoiceAI\modelos_f5\hub`.
- When the model is already cached, startup should prefer local resolution and force HF/Transformers offline mode to avoid unnecessary network access.
- Heavy TTS requests may need longer timeouts to avoid chunk failures.

## SDD / Skills Workflow

- Use Conductor skills in `.agents/skills/` for spec-driven work.
- For new features or ambiguous changes, start with `conductor-setup` if the Conductor structure is missing, then use `conductor-newTrack` before implementation.
- Use `conductor-implement` only after the track/spec/tasks are clear.
- Use `conductor-status` to inspect active tracks, `conductor-review` before closing work, and `conductor-revert` only when the user explicitly asks to undo a track.
- Keep tiny fixes direct when a full SDD track would add unnecessary process.

## Existing Project Skill

- Existing VoiceAI-specific skill remains available and should not be overwritten:
- `vocalai-ui-ux-architect`

## Worktree Safety

- The worktree may contain user changes. Do not revert or overwrite existing changes unless explicitly requested.
- Do not commit unless the user explicitly asks.

## Feature Preservation Rule

- Before modifying, extracting, or removing any existing feature, FIRST verify it works as intended in the current codebase.
- If code looks like a bug but might be intentional feature logic, INFORM or ASK the user before changing it.
- Never remove a feature gate, filter, or validation without confirming its purpose with the user.
- When refactoring, preserve exact behavior of existing features — extract first, then fix bugs separately with user confirmation.

## SmartAggregator Input Policy

- Never expose raw chat to the LLM, UI diagnostics, logs, or persistence. Raw chat may only feed in-memory counters.
- RF3/chat-interactive may use a relaxed filter policy for Twitch (`twitch_relaxed`) so short chat messages can reach compact aggregation.
- Cohost/Agenda mode must stay stricter: agenda topics are the priority and chat is secondary compact context only.
- LiveVoice continuous and PTT are separate voice pipelines; do not change them when tuning SmartAggregator chat filters.
- Keep diagnostics as counts/reasons only (`seen`, `accepted`, `rejected`, `by_reason`) and preserve compact context boundaries.
