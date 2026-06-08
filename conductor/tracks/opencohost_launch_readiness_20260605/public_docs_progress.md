# OpenCohost Public Documentation Progress

Date: 2026-06-07

## Current Documentation Direction

OpenCohost public documentation is being produced as small, evidence-backed
documentation slices. The goal is to help human contributors and coding agents
understand the project without mixing current behavior, future plans, and agent
memory.

All public documentation artifacts are written in English.

## Completed in This Slice

| Output | Status | Notes |
|---|---|---|
| `docs/INDEX.md` | Drafted | Router for humans and agents. Marks historical docs as references, not final public truth. |
| `docs/METHODOLOGY.md` | Drafted | Defines controlled validation, evidence labels, Conductor usage, runtime/privacy boundaries. |
| `docs/TESTING.md` | Drafted and verified | Built from test discovery: 53 test files, 1,616 AST definitions, 1,736 collected pytest items in the project environment. |
| `docs/architecture.md` | Rewritten in English | Initial OpenCohost architecture map. Shallow by design; module docs carry deeper detail. |
| `docs/modules/ui-shell.md` | Drafted | Documents the UI shell composition root, Tk mainloop ownership, `_safe_after`, UI task queue, tests, limits, and deferred UI work. |
| `docs/modules/runtime-speech.md` | Drafted and verified | Documents MotorVocalIA, speech source ownership, priority arbitration, agenda prefetch, TTS modes, tests, and runtime limits. |

## Verification Performed

- `git diff --check`
- Test discovery for `docs/TESTING.md`
- Focused runtime-speech verification:

```powershell
E:\Miniconda\envs\flux_env\python.exe -m pytest tests/test_runtime_smoke_harness.py tests/test_kira_orchestration_gaps.py tests/test_llm_engine_timeouts.py tests/test_streaming_speech_pipeline.py tests/test_sentence_splitter.py -q -o cache_dir=E:\VoiceAI\temp\.pytest_cache_local --basetemp=E:\VoiceAI\temp\pytest-basetemp
```

Result:

```text
79 passed
```

## Important Notes

- The Python/system-environment collection issue was treated as an environment
  note, not as a launch blocker.
- README remains intentionally deferred. It should summarize validated docs
  after core architecture, testing, methodology, and module docs are complete.
- Future plans stay in Conductor tracks or explicitly labeled deferred sections.
- Real audio, OBS, OAuth, and long-running GUI behavior still require manual or
  opt-in runtime validation.

## Remaining Documentation Slices

- `docs/modules/tts-audio.md`
- `docs/modules/smart-aggregator.md`
- `docs/modules/stream-integrations.md`
- `docs/modules/runtime-safety.md`
- `docs/TRUST_MODEL.md` or `SECURITY.md`
- `CONTRIBUTING.md`
- `THIRD_PARTY_NOTICES.md`
- Public README refresh
- Final documentation audit
