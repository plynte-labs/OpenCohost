# OpenCohost Testing Guide

This guide documents the current test surface and the validation boundaries for
OpenCohost. It is evidence-backed: test counts and categories come from test
discovery on the current repository.

## Current Verified Snapshot

Date verified: 2026-06-07.

| Evidence | Result |
|---|---:|
| Test files matching `tests/test_*.py` | 53 |
| Test definitions discovered by AST | 1,616 |
| Pytest collected items in project environment | 1,736 |

The difference between AST definitions and pytest collected items is expected:
parametrized tests expand into multiple collected items.

## Verified Collection Command

The current project environment successfully collected the suite with:

```powershell
python -m pytest --collect-only -q
```

Result:

```text
1736 tests collected
```

Important local note: running collection with the system Python 3.13 failed
because that environment did not have `soundfile` installed, and it also tried
to write pytest cache under `.pytest_cache`. Use the project environment and a
local temp cache path when validating this workspace.

## Pytest Configuration

Source: `pytest.ini`.

| Setting | Current value |
|---|---|
| Test path | `tests` |
| Test files | `test_*.py` |
| Test functions | `test_*` |
| Test classes | `Test*` |
| Default addopts | `-v --tb=short` |
| Markers | `slow`, `integration`, `offline` |
| Coverage source | `smart_aggregator`, `stream_admin`, `core` |
| Coverage fail-under | `0` |

## Test Surface by Subsystem

These buckets are based on test filenames and should be treated as a navigation
map, not a perfect ownership model.

| Subsystem | Files | AST test definitions | What it mainly covers |
|---|---:|---:|---|
| UI and panels | 15 | 695 | Tk/panel behavior, UI state, voice controls, avatar panel, status bar, model/profile panels. |
| SmartAggregator, agenda, chat, orchestration | 9 | 325 | Chat event detection, agenda signals, Kira orchestration, topic suggestions, SmartAggregator rules. |
| Config, validation, utilities | 10 | 236 | Schema, presets, storage packaging, validation guards, translator, simulator, music/profile utilities. |
| LLM, TTS, Ollama, runtime speech | 9 | 112 | Model switching, LLM tiers, timeouts, Ollama startup/offline UX, sentence splitting, streaming speech, deterministic smoke harness. |
| Health, crash, cleanup, runtime safety | 4 | 94 | Health monitor, fallback gates, crash reporting, temp cleanup. |
| Stream integrations and OBS | 3 | 31 | OBS client, Stream Admin, URL parsing. |
| Other / integration | 3 | 123 | Structural integration and broad app import/wiring checks. |

## File Catalog

| File | Definitions | Primary area |
|---|---:|---|
| `tests/test_advanced_panel.py` | 56 | UI and panels |
| `tests/test_agenda_signal.py` | 37 | SmartAggregator, agenda, chat, orchestration |
| `tests/test_app_shell_motor_event_threading.py` | 4 | UI and panels |
| `tests/test_app_shell_obs_resilience.py` | 24 | UI and panels |
| `tests/test_avatar_config.py` | 17 | UI and panels |
| `tests/test_avatar_panel.py` | 21 | UI and panels |
| `tests/test_avatar_state.py` | 11 | UI and panels |
| `tests/test_chat_input_contract.py` | 21 | SmartAggregator, agenda, chat, orchestration |
| `tests/test_chat_source.py` | 32 | SmartAggregator, agenda, chat, orchestration |
| `tests/test_cohost_agenda_panel.py` | 13 | UI and panels |
| `tests/test_cohost_profiles.py` | 2 | Config, validation, utilities |
| `tests/test_crash_reporting.py` | 8 | Health, crash, cleanup, runtime safety |
| `tests/test_editorial_agenda_bridge.py` | 3 | SmartAggregator, agenda, chat, orchestration |
| `tests/test_editorial_cards.py` | 5 | SmartAggregator, agenda, chat, orchestration |
| `tests/test_health_integration.py` | 17 | Health, crash, cleanup, runtime safety |
| `tests/test_health_monitor.py` | 65 | Health, crash, cleanup, runtime safety |
| `tests/test_integration.py` | 46 | Other / integration |
| `tests/test_kira_agenda_controller.py` | 74 | SmartAggregator, agenda, chat, orchestration |
| `tests/test_kira_chaos_stream.py` | 27 | SmartAggregator, agenda, chat, orchestration |
| `tests/test_kira_orchestration_gaps.py` | 22 | SmartAggregator, agenda, chat, orchestration |
| `tests/test_llm_engine_model_trace.py` | 22 | LLM, TTS, Ollama, runtime speech |
| `tests/test_llm_engine_tiers.py` | 5 | LLM, TTS, Ollama, runtime speech |
| `tests/test_llm_engine_timeouts.py` | 40 | LLM, TTS, Ollama, runtime speech |
| `tests/test_llm_tiers.py` | 5 | LLM, TTS, Ollama, runtime speech |
| `tests/test_model_panel.py` | 55 | UI and panels |
| `tests/test_music_library.py` | 7 | Config, validation, utilities |
| `tests/test_obs_client.py` | 14 | Stream integrations and OBS |
| `tests/test_ollama_offline_ux_guardrails.py` | 15 | LLM, TTS, Ollama, runtime speech |
| `tests/test_ollama_startup.py` | 8 | LLM, TTS, Ollama, runtime speech |
| `tests/test_presets.py` | 23 | Config, validation, utilities |
| `tests/test_product_ui_refactor_safety.py` | 19 | UI and panels |
| `tests/test_profile_panel.py` | 33 | UI and panels |
| `tests/test_profile_tone.py` | 11 | Config, validation, utilities |
| `tests/test_protocols.py` | 35 | Config, validation, utilities |
| `tests/test_ptt_manager.py` | 72 | Other / integration |
| `tests/test_runtime_smoke_harness.py` | 5 | LLM, TTS, Ollama, runtime speech |
| `tests/test_schema.py` | 48 | Config, validation, utilities |
| `tests/test_sentence_splitter.py` | 8 | LLM, TTS, Ollama, runtime speech |
| `tests/test_simulator.py` | 35 | Config, validation, utilities |
| `tests/test_smart_aggregator.py` | 83 | SmartAggregator, agenda, chat, orchestration |
| `tests/test_smart_aggregator_ui.py` | 129 | UI and panels |
| `tests/test_status_bar.py` | 62 | UI and panels |
| `tests/test_storage_packaging.py` | 6 | Config, validation, utilities |
| `tests/test_stream_admin.py` | 9 | Stream integrations and OBS |
| `tests/test_stream_admin_ui.py` | 107 | UI and panels |
| `tests/test_streaming_speech_pipeline.py` | 4 | LLM, TTS, Ollama, runtime speech |
| `tests/test_temp_file_cleanup.py` | 4 | Health, crash, cleanup, runtime safety |
| `tests/test_topic_suggester.py` | 26 | SmartAggregator, agenda, chat, orchestration |
| `tests/test_translator.py` | 22 | Config, validation, utilities |
| `tests/test_ui_state.py` | 40 | UI and panels |
| `tests/test_url_parser.py` | 8 | Stream integrations and OBS |
| `tests/test_validation.py` | 47 | Config, validation, utilities |
| `tests/test_voice_control.py` | 104 | UI and panels |

## Recommended Test Commands

### Collect tests only

Use this before editing test docs or CI configuration:

```powershell
python -m pytest --collect-only -q
```

### Run focused tests

Prefer focused tests while working on a module:

```powershell
python -m pytest tests/test_<area>.py -q
```

### Known targeted health validation

Current handoff reference:

```powershell
python -m pytest tests/test_health_monitor.py tests/test_health_integration.py tests/test_app_shell_obs_resilience.py -q
```

## What Automated Tests Prove Well

Current automated tests provide meaningful coverage for:

- UI state and panel behavior under mocked/fake UI conditions.
- SmartAggregator and agenda signal rules.
- Cohost orchestration edge cases.
- Model switching, LLM tiers, timeout handling, and Ollama startup/offline guardrails.
- Health monitor transitions and fallback gates.
- Crash reporting hooks and fatal-log setup behavior.
- Validation guards for unsafe output, private data patterns, and negative engagement wording.
- Deterministic runtime smoke harness contracts.

## What Automated Tests Do Not Fully Prove

These areas require manual validation or opt-in runtime smoke tests:

- Real audio device behavior.
- Native `pygame` mixer behavior.
- Native `sounddevice` behavior.
- Tk mainloop behavior under full desktop interaction.
- Qwen subprocess lifecycle under real model load.
- Ollama service behavior on a clean external machine.
- OBS websocket behavior against a live OBS instance.
- YouTube OAuth and live chat behavior against real service accounts.
- Full process shutdown behavior after real audio/stream activity.

## Runtime Smoke Boundary

`tests/test_runtime_smoke_harness.py` verifies the deterministic smoke harness
contract. It does not run real audio or device-dependent smoke tests by default.

Current verified boundary:

- deterministic mode is testable in automation,
- real/semi-real audio smoke remains opt-in,
- runtime smoke should not become a mandatory unit-test dependency.

## Known Test Quality Notes

Existing audit reference: [`test_suite_audit_full.md`](test_suite_audit_full.md).

That audit records both healthy tests and weaker patterns. Treat it as a
reference for improving tests, not as proof that the entire test suite is
perfect.

Current known caution areas from that audit include:

- tests that manually set state instead of exercising the real handler,
- tests that only assert a return type or broad allowed status set,
- smoke-style "does not crash" tests that provide limited regression value,
- environment-dependent assertions.

## Adding New Tests

When adding tests:

1. Prefer testing production behavior over manually setting internal state.
2. Use fakes/mocks to isolate external services.
3. Keep raw chat and private data out of test logs and fixtures.
4. Mark true external-service or runtime tests separately.
5. Do not make real audio, OBS, OAuth, or local model availability mandatory for every contributor.
6. Pair runtime-facing changes with a note about what unit tests cannot prove.

## Documentation Maintenance Checklist

Update this guide when:

- new test files are added,
- pytest configuration changes,
- runtime smoke policy changes,
- a new required test environment is introduced,
- a module doc makes a new testing claim.
