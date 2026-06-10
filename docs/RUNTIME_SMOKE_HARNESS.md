# Runtime Smoke Harness

The runtime smoke harness validates the risky OpenCohost cohost/audio path
without putting real audio-device checks into the normal unit test suite.

## Quick path

Run the deterministic smoke scenario:

```powershell
E:\Miniconda\envs\flux_env\python.exe tools\runtime_smoke_harness.py --mode deterministic --json temp\runtime-smoke-deterministic.json
```

Expected result:

- Exit code is `0`.
- JSON output includes `"passed": true`.
- `failures` is an empty list.
- `speaking_start` equals `speaking_end`.
- `no_agenda_direct_overlap` is `true`.
- `no_stale_speech_source` is `true`.

## What it proves

| Signal | Meaning |
| --- | --- |
| `process_survived` | The controlled runtime scenario did not crash. |
| `speaking_events_balanced` | Speech start/end lifecycle did not drift. |
| `no_agenda_direct_overlap` | Agenda prefetch did not play over direct/operator work. |
| `no_stale_speech_source` | Speech ownership was cleared after TTS completed. |
| `shutdown_completed` | The scenario reached the planned cleanup point. |

## When to run it

Run the deterministic harness after changes to:

- `core/llm_engine.py`
- cohost/agenda orchestration
- TTS playback lifecycle
- direct/operator interruption behavior
- smoke harness reporting

## Normal tests to pair with it

```powershell
E:\Miniconda\envs\flux_env\python.exe -m pytest tests/test_runtime_smoke_harness.py tests/test_kira_orchestration_gaps.py tests/test_llm_engine_timeouts.py -q -o cache_dir=E:\VoiceAI\temp\.pytest_cache_local --basetemp=E:\VoiceAI\temp\pytest-basetemp
```

These tests verify the deterministic harness contract plus the cohost and
LLM/TTS lifecycle contracts around it.

## What it intentionally does not do

- It does not use real audio devices.
- It does not start OBS, YouTube, Twitch, OAuth, or production chat.
- It does not replace manual release validation.
- It does not test native/fatal crash logging; that belongs to
  `crash_reporting_hardening_20260606`.

## Semi-real mode

Semi-real audio validation is intentionally not enabled yet. It should remain
opt-in because pygame/audio-driver behavior depends on the local machine.

Use the deterministic harness first. Only design and run semi-real validation
when a specific release-readiness question requires real audio/device evidence.
