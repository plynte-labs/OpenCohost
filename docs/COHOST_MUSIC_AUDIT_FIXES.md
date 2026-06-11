# Co-host Agenda and Music audit fixes

## Outcome

The high-risk dual-audit findings around Co-host Agenda prefetch/lifecycle, compact chat prompt persistence, destructive UI actions, and AudioBed concurrency were fixed in the production paths.

## Fixed behavior

- **Agenda lifecycle source-gating**: `mark_generation_accepted()`, `mark_speech_complete()`, and agenda prefetch now run only while the current speech source starts with `kira-agenda`.
- **Stale agenda prefetch invalidation**: if PTT/chat/direct speech interrupts or a higher-priority pending item would outrank the cached agenda continuation, the cached agenda action/text is cleared so the next agenda turn regenerates from fresh state.
- **Agenda output validation**: agenda LLM output now passes through `KiraAgendaController.accept_output()` before TTS/history commit. Rejected outputs record controller failure and do not speak.
- **Compact prompt history safety**: agenda prompts containing internal compact chat context are no longer persisted verbatim in `MotorVocalIA.historial`; agenda history stores a redacted user entry plus the safe assistant output.
- **Destructive UI confirmation**: queued Co-host topic deletion and Music `Limpiar faltantes` now require confirmation. Track deletion confirmation and app-managed-file-only delete constraints remain in place.
- **AudioBed race hardening**: `AudioBedEngine` state/channel mutations are protected with a small `RLock`.

## Remaining theoretical risks

- Source-gating depends on `MotorVocalIA.current_speech_source` staying accurate during `speaking_start/end` callbacks.
- Agenda guardrails are heuristic; novel prompt leaks or semantically repetitive outputs may still require more patterns.
- `AudioBedEngine` locking protects local state, but backend `pygame` mixer behavior is still platform/audio-driver dependent.

## Manual smoke-test checklist

1. Start Co-host Agenda with at least two queued topics; confirm agenda continues naturally.
2. While agenda is speaking, trigger PTT/chat; confirm PTT/chat wins and agenda does not mark a completed turn for that speech.
3. After interruption, confirm the next agenda line is regenerated, not stale cached text.
4. Force an agenda output with internal/repetitive phrasing; confirm it is rejected and no TTS plays.
5. Inspect/clear memory: raw `CHAT COMPACTO FILTRADO` prompt text should not appear in chat history.
6. Delete a queued agenda topic; confirm the dialog appears and cancel preserves it.
7. Use Music delete and `Limpiar faltantes`; confirm both ask before destructive metadata/file actions.
8. Play/duck/unduck/stop music while Kira speaks; confirm no visible race/crash.

## Focused verification

```powershell
python -m pytest tests/test_music_library.py tests/test_product_ui_refactor_safety.py tests/test_cohost_agenda_panel.py tests/test_kira_agenda_controller.py tests/test_llm_engine_timeouts.py
```

Latest result: `89 passed`.
