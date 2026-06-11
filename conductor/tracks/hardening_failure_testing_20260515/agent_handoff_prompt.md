# Agent Handoff Prompt — Review and Create Kira Chaos Test

Copiá este prompt para otro agente executor/reviewer. Recomendado: `opencode-go/qwen3.6-plus`.

```text
You are a VoiceAI hardening executor/reviewer working inside the project directory.

MISSION
Review and implement the urgent Kira chaos stream test defined in:
conductor/tracks/hardening_failure_testing_20260515/urgent_kira_chaos_test.md

CONTEXT
VoiceAI is being prepared for installer readiness. Before packaging, we need proof that Kira survives chaotic live-stream conditions: 7+ agenda topics, extended agenda responses, repeated streamer/PTT interruptions, intense noisy chat, joyita comments, queue overflow, stale chat expiry, and recovery without invalid states.

MANDATORY ENGRAM RULES
- Before planning/coding, recover Engram context for project `voiceai` with mem_context first, then mem_search if needed.
- Treat memories as constraints, especially recent agenda prefetch fixes, queue/TTL policy, Windows Qwen cleanup, avatar config pollution, and music_library local-state caveat.
- Save meaningful discoveries, decisions, bug fixes, and completed summaries back to Engram using mem_save or mem_session_summary.
- Use project: `voiceai` for all Engram saves.

MANDATORY SDD / CONDUCTOR RULES
- This work belongs to track `hardening_failure_testing_20260515`.
- Read:
  - conductor/tracks/hardening_failure_testing_20260515/spec.md
  - conductor/tracks/hardening_failure_testing_20260515/plan.md
  - conductor/tracks/hardening_failure_testing_20260515/urgent_kira_chaos_test.md
- Follow the project's TDD workflow: write/adjust failing tests first where practical, then implement minimal code.
- Do not start packaging work.
- Do not integrate LiveAudio/Whisper.
- Do not commit unless the user explicitly asks.

WORKTREE SAFETY
- Preserve all existing uncommitted changes.
- Do NOT revert user changes.
- Do NOT touch `config/music_library.json`.
- Do NOT write pytest temp paths into `config/avatar.yaml`.
- Tests must use temp configs/mocks when config persistence is involved.

TECHNICAL POLICY TO PRESERVE
- Priority order: PTT/streamer = 0, chat = 1, agenda = 2.
- PTT must always outrank chat and agenda.
- PTT must not expire by chat TTL.
- Non-PTT queued items older than TTL must not cause stale reactions.
- Overflow must preserve higher-priority items first.
- Chat joyita selection must be deterministic and cheap; no LLM for selection.
- Agenda prefetched outputs must be validated before cache and recorded when actually spoken.
- UI Kira response should update at playback time, not background prefetch time.

TASK
1. Review urgent_kira_chaos_test.md for completeness and consistency with current code.
2. Create or adjust automated tests for the highest-value missing coverage:
   - 7-topic agenda stress without repetition/state stall.
   - repeated PTT interruptions during agenda/preload/chat conditions.
   - high-volume noisy chat/backpressure with joyita preservation.
   - queue overflow/TTL behavior under combined PTT + chat + agenda load.
3. Implement only minimal production changes required for those tests.
4. Run focused tests with:
   python -m pytest tests/test_llm_engine_timeouts.py tests/test_kira_agenda_controller.py tests/test_smart_aggregator.py tests/test_smart_aggregator_ui.py -q
5. If feasible, run the broader relevant suite:
   python -m pytest tests/test_llm_engine_timeouts.py tests/test_kira_agenda_controller.py tests/test_health_monitor.py tests/test_avatar_panel.py tests/test_music_library.py tests/test_smart_aggregator.py tests/test_smart_aggregator_ui.py -q

RETURN FORMAT
Return a concise report with:
- Files changed.
- Tests added/modified.
- Commands run and pass/fail counts.
- Any remaining gaps or risks.
- Whether config/avatar.yaml or config/music_library.json changed.
- Engram memories saved.

DO NOT COMMIT.
```
