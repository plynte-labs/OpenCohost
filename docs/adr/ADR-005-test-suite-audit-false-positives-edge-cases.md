# ADR-005: Test Suite Audit — False Positives, Weak Tests & Edge Case Gaps

**Date**: 2026-06-01  
**Status**: Accepted  
**Author**: Test Audit Session  
**Scope**: Core test files (195 tests across 7 files)

## Context

OpenCohost is a live-streaming AI assistant that manages LLM inference, TTS, model switching, and health monitoring in real-time. Test reliability is non-negotiable: a false-positive test that masks a model-switch bug can mean the streamer loses AI assistance mid-broadcast with no warning.

This audit was triggered by the discovery that `test_no_silent_deferred_model_switch_offline` in `tests/test_ollama_offline_ux_guardrails.py` was a **false green** — it passed by reimplementing production logic inline instead of calling the real handler. That pattern turned out to not be isolated.

### Files Audited

| File | Tests | Status |
|------|-------|--------|
| `tests/test_llm_engine_model_trace.py` | 21 | 21 GREEN |
| `tests/test_llm_engine_tiers.py` | 5 | 5 GREEN |
| `tests/test_ollama_startup.py` | 8 | 8 GREEN |
| `tests/test_health_monitor.py` | 48 | 48 GREEN |
| `tests/test_llm_engine_timeouts.py` | 30 | 30 GREEN |
| `tests/test_ui_state.py` | 44 | 44 GREEN |
| `tests/test_validation.py` | 39 | 39 GREEN |
| **Total** | **195** | **195 GREEN** |

## Decision

### 1. False Positives Identified (3)

#### FP-1: `test_switch_while_speaking_is_pending` — SEVERITY: HIGH

**File**: `tests/test_llm_engine_model_trace.py:154-172`

**What it claims**: "Switch while `_speaking=True` → pending state, model NOT changed yet."

**What it actually tests**: Nothing. It manually sets `motor._pending_model_switch = new_model` instead of calling the real `switch_model` command handler in `run()`.

```python
# Test sets state directly — never calls real handler:
motor._desired_model = new_model
motor._pending_model_switch = new_model          # ← manually set
motor._pending_switch_retries = 3
motor._pending_switch_next_at = time.monotonic() + 2.0
motor.ui_callback("model_switch_pending")
```

**Why this is dangerous**: If the real handler in `run()` changes its queuing logic (e.g., adds a `_speaking` guard, changes retry count, or stops emitting `model_switch_pending`), this test still passes because it never calls that code.

**Same pattern as**: The guardrails false-green we already fixed in `test_ollama_offline_ux_guardrails.py`.

**Recommended fix**: Extract the `switch_model` handler from `run()` into a testable method like `_handle_switch_model(payload)`, or process the command via `command_queue` with a short thread run.

---

#### FP-2: `test_unavailable_when_pynvml_not_installed` — SEVERITY: LOW

**File**: `tests/test_health_monitor.py:35-39`

```python
assert guard.status in ("unavailable", "normal", "low", "critical")
```

Assertion accepts ALL valid enum values. The test name says "unavailable when not installed" but would pass with `status == "normal"` on a machine with pynvml and a GPU.

**Recommended fix**: Mock `pynvml` import to guarantee `ImportError`, then assert `status == "unavailable"` exactly.

---

#### FP-3: `test_is_port_in_use` — SEVERITY: LOW

**File**: `tests/test_health_monitor.py:398-401`

```python
result = QwenProcessManager._is_port_in_use(5000)
assert isinstance(result, bool)
```

Environment-dependent — only checks return type. Passes whether port 5000 is occupied by another app or free. No useful regression value.

**Recommended fix**: Mock `socket.socket.connect_ex`, test both True (port occupied) and False (port free) explicitly.

---

### 2. Weak/Insignificant Tests (6)

These aren't false positives — they test what they claim — but they add near-zero regression value because their assertions are trivially satisfied:

| ID | Test | File | Issue |
|----|------|------|-------|
| WK-1 | `test_poll_does_not_crash` | `test_health_monitor.py:41` | No assertion beyond "no exception" |
| WK-2 | `test_free_mb_is_float` | `test_health_monitor.py:53` | Type check only |
| WK-3 | `test_status_values` | `test_health_monitor.py:47` | Same as FP-2 — accepts all valid values |
| WK-4 | `test_idle_seconds_zero_initially` | `test_health_monitor.py:202` | Default value check |
| WK-5 | `test_custom_values` (MonitorState) | `test_health_monitor.py:724` | Tests Python dataclass constructor |
| WK-6 | `test_all_fields_validation` | `test_validation.py:170` | Exact duplicate of `test_valid_default_config_passes` |

**Recommended action**: Don't delete them outright. WK-1 through WK-4 should be consolidated into a single `test_vram_guard_initial_state` parametrized test. WK-5 can be removed. WK-6 is a duplicate — remove.

### 3. Edge Case Gaps (14)

Grouped by production risk for a live-streaming application.

#### Priority 1 — Production-critical (live broadcast risk)

| ID | Scenario | File | Risk |
|----|----------|------|------|
| EC-A | Concurrent tier switch + model switch collision | `test_llm_engine_tiers.py` | Two `prepare_model` calls compete for VRAM → OOM crash |
| EC-B | Tier switch during active inference | `test_llm_engine_tiers.py` | Generation uses wrong model after mid-flight switch |
| EC-C | OllamaWatchdog flap during generation | `test_health_monitor.py` | `can_vibe_call()` oscillates, drops features mid-stream |
| EC-D | Qwen process dies → zombie detection | `test_health_monitor.py` | `should_use_heavy_tts` says True, TTS sends to dead server |

#### Priority 2 — Data integrity

| ID | Scenario | File | Risk |
|----|----------|------|------|
| EC-E | Corrupted `last_model.json` on startup | `test_llm_engine_model_trace.py` | `JSONDecodeError` crash instead of fallback |
| EC-F | Concurrent `save_last_model` writes | `test_llm_engine_model_trace.py` | Interleaved writes corrupt the file |
| EC-G | History redaction bypass via XML encoding | `test_llm_engine_timeouts.py` | Raw agenda prompts leak to conversation history |

#### Priority 3 — UX robustness

| ID | Scenario | File | Risk |
|----|----------|------|------|
| EC-H | Multiple Ollama bind failures before success | `test_ollama_startup.py` | UI gives up after first failure |
| EC-I | Output guard with mixed-language text | `test_validation.py` | Bilingual model output bypasses Spanish-only patterns |
| EC-J | Observer notification order during batch update | `test_ui_state.py` | `ModelPanel` sees stale `ollama_state` during `model_status` notify |
| EC-K | Extremely long LLM output (50KB+) | `test_llm_engine_timeouts.py` | TTS creates hundreds of chunks → memory/timeout |
| EC-L | RTFTracker with NaN/inf measurements | `test_health_monitor.py` | Rolling average becomes NaN → bad TTS decisions |
| EC-M | Priority queue with identical timestamps | `test_llm_engine_timeouts.py` | Heap comparison `TypeError` on Windows low-res timer |
| EC-N | Unicode homoglyphs bypass output guard | `test_validation.py` | Cyrillic lookalikes pass regex patterns → harmful TTS output |

## Consequences

### Positive

- **FP-1 fix** eliminates the most dangerous false-green pattern — the same pattern that already masked the guardrails bug.
- **EC-A through EC-D** cover the exact scenarios that would cause a streamer to lose AI mid-broadcast — the worst UX failure for this app.
- **EC-N** (homoglyph bypass) is a security gap in the output guard that could allow AI self-identification to reach TTS.

### Negative

- Fixing FP-1 properly requires extracting the `switch_model` handler from `run()` into a testable method — a production code change in `core/llm_engine.py`.
- Implementing all 14 edge cases adds ~200-300 lines of test code and potentially surfaces bugs that need fixing in the same release.

### Neutral

- Weak tests (WK-1 through WK-6) are harmless but add noise. Consolidation is a low-priority cleanup task.

## Notes

### Healthy Test Patterns Worth Preserving

The following tests demonstrate best practices that should be followed when writing new tests:

- **`test_ollama_startup.py`**: Exemplary dependency injection via `FakeClock`/`FakeProcess`. Every external dependency is injected through constructor. No mocking of internals needed.
- **`test_generation_captures_model_at_request_start`** (tiers): Concurrency-aware — mutates `current_model` inside the mock to prove the generation uses a snapshotted model.
- **`test_run_loop_recovers_selected_model_after_external_ollama_start`** (model_trace): Actually starts the motor thread and tests real lifecycle including command queue processing.
- **`test_update_is_atomic`** (ui_state): Multi-threaded snapshot consistency proof — verifies no torn reads.
- **`test_accumulation_*_logs_count_without_raw_payload`** (timeouts): Privacy guard tests — verify raw chat content never leaks to logs.

### Anti-Pattern: Manual State Setup

The anti-pattern found in FP-1 and the original guardrails false-green:

```python
# ❌ ANTI-PATTERN: Testing a hypothesis, not the code
motor._desired_model = new_model
motor._pending_model_switch = new_model
motor.ui_callback("model_switch_pending")
assert motor._pending_model_switch == new_model  # Always passes

# ✅ CORRECT: Testing the real code path
motor.command_queue.put(("switch_model", new_model))
# Process one command from the queue...
assert motor._pending_model_switch == new_model  # Tests real behavior
```

This anti-pattern should be flagged in code review for any future test that sets internal `_` attributes directly instead of calling through the public interface.
