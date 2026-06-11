# Test Suite Audit — Core VoiceAI Files

**Scope**: 7 core test files (195 tests, all GREEN)  
**Date**: 2026-06-01  
**Author**: Claude (Auditoría Técnica Sesión 0531)  
**Status**: Saved for implementation reference  

---

## Overall Verdict

The test suite is **solid for a live-streaming AI app**. No catastrophic false positives found — most tests exercise real production code. However, there are several patterns that weaken confidence:

---

## 🔴 False Positives (tests that pass but don't test what they claim)

### FP-1: `test_switch_while_speaking_is_pending` — [test_llm_engine_model_trace.py:154-172](tests/test_llm_engine_model_trace.py#L154-L172)

**Severity: HIGH** — Same pattern as the guardrails false-green we already fixed.

The test claims to verify "Switch while `_speaking=True` → pending state", but it **manually sets the motor attributes** instead of calling the real `switch_model` handler:

```python
# Test sets state directly:
motor._desired_model = new_model
motor._pending_model_switch = new_model  # ← manually set, not via handler
motor._pending_switch_retries = 3
```

**Problem**: If the real handler changes its queuing logic (e.g., adds a guard when `_speaking=True`), this test would still pass because it never calls the handler. It's testing a hypothesis about the code, not the code itself.

**Fix**: Call `motor.command_queue.put(("switch_model", "qwen3:4b"))` and run one iteration of the command loop, or extract the handler into a testable method.

---

### FP-2: `test_unavailable_when_pynvml_not_installed` — [test_health_monitor.py:35-39](tests/test_health_monitor.py#L35-L39)

**Severity: LOW** — Assertion is meaningless.

```python
assert guard.status in ("unavailable", "normal", "low", "critical")
```

This accepts ALL possible values. It will pass whether pynvml is installed or not, whether the GPU works or not. The test name says "unavailable when not installed" but it would pass with `"normal"` too.

**Fix**: Mock `pynvml` import to guarantee `ImportError`, then assert `status == "unavailable"`.

---

### FP-3: `test_is_port_in_use` — [test_health_monitor.py:398-401](tests/test_health_monitor.py#L398-L401)

**Severity: LOW** — Environment-dependent, no useful assertion.

```python
result = QwenProcessManager._is_port_in_use(5000)
assert isinstance(result, bool)
```

This passes regardless of whether port 5000 is in use. It only checks the return type.

**Fix**: Mock socket, test both True (port occupied) and False (port free) paths explicitly.

---

## 🟡 Weak/Insignificant Tests

### WK-1: `test_poll_does_not_crash` — [test_health_monitor.py:41-45](tests/test_health_monitor.py#L41-L45)

"Should not raise" tests are valid as smoke tests but add zero regression value. If poll() starts returning wrong results instead of crashing, this test won't catch it.

### WK-2: `test_free_mb_is_float` — [test_health_monitor.py:53-57](tests/test_health_monitor.py#L53-L57)

Checking that `free_mb` returns a float is a type-level assertion. Useful only if there was ever a risk of returning `None` or `int`. Low value.

### WK-3: `test_status_values` — [test_health_monitor.py:47-51](tests/test_health_monitor.py#L47-L51)

Same as FP-2 — accepts all valid values without asserting a specific state.

### WK-4: `test_idle_seconds_zero_initially` — [test_health_monitor.py:202-205](tests/test_health_monitor.py#L202-L205)

Checking a default value. Not wrong, just trivial. Combined with `test_is_running_false_initially` and `test_is_manual_false_initially`, these 3 tests could be a single `test_initial_state` parametrized test.

### WK-5: `test_custom_values` in TestMonitorState — [test_health_monitor.py:724-738](tests/test_health_monitor.py#L724-L738)

Tests that a dataclass accepts keyword arguments. Python guarantees this.

### WK-6: `test_all_fields_validation` — [test_validation.py:170-174](tests/test_validation.py#L170-L174)

Exact duplicate of `test_valid_default_config_passes` at line 68.

---

## 🟢 Healthy Tests (well-written, high-value)

These deserve callout for being well-structured — they test real behavior with real consequences:

| File | Test | Why it's good |
|------|------|---------------|
| `test_llm_engine_model_trace.py` | `test_run_loop_recovers_selected_model_after_external_ollama_start` | Actually starts the motor thread, tests real lifecycle |
| `test_llm_engine_model_trace.py` | `test_check_ollama_with_pending_model_does_not_warm_stale_current_model` | Catches a real performance trap — warming the wrong model |
| `test_llm_engine_tiers.py` | `test_generation_captures_model_at_request_start` | Concurrency-aware: mutates model mid-generation to prove snapshot |
| `test_llm_engine_tiers.py` | `test_failed_tier_switch_keeps_previous_active_model_and_tier` | Verifies full rollback including 7 state fields |
| `test_ollama_startup.py` | ALL (8 tests) | Excellent dependency injection via FakeClock/FakeProcess |
| `test_health_monitor.py` | `test_run_marks_state_unknown_after_poll_exception` | Prevents stale green exposure after crash |
| `test_health_monitor.py` | `test_qwen_alive_but_unhealthy_is_not_green` | Anti-false-positive: alive ≠ healthy |
| `test_llm_engine_timeouts.py` | `test_heavy_tts_continues_after_connection_error` | Full TTS retry integration with FakeMusic |
| `test_llm_engine_timeouts.py` | `test_accumulation_*_logs_count_without_raw_payload` | Privacy guard: raw chat never leaks to logs |
| `test_ui_state.py` | `test_update_is_atomic` | Multi-threaded snapshot consistency proof |
| `test_validation.py` | `test_negative_engagement_*` | 9 tests covering R11 rule in both languages |

---

## 🔵 Proposed Edge Cases (14)

### Priority 1 — Production-critical for live streaming

#### EC-A: Concurrent tier switch + model switch collision
*   **File**: `test_llm_engine_tiers.py`
*   **Scenario**: User clicks tier "fast" while a `switch_model` command is already in the queue.
*   **Risk**: Two prepare_model calls compete for VRAM, or the motor ends up on tier "fast" with the wrong model tag.
*   **Test**: Enqueue `switch_model` to gemma4, then immediately call `switch_llm_tier("fast")`. Assert only ONE model is loaded and tier/model are consistent.

#### EC-B: Tier switch during active inference
*   **File**: `test_llm_engine_tiers.py`
*   **Scenario**: `_generar_dialogo` is running when `switch_llm_tier` is called.
*   **Risk**: The generation uses model A but `current_model` has already changed to B, causing MODEL_MISMATCH_WARNING and potential wrong-model responses.
*   **Test**: Mock `ollama.chat` to be slow (sleep 0.1s), call `_generar_dialogo` in a thread, call `switch_llm_tier` concurrently. Assert the generation completes with the original model.

#### EC-C: OllamaWatchdog flap during active generation
*   **File**: `test_health_monitor.py`
*   **Scenario**: OllamaWatchdog transitions `healthy→down→healthy` while motor is generating.
*   **Risk**: HealthMonitor incorrectly blocks Vibe calls or switches TTS mode mid-inference.
*   **Test**: Set up green HealthMonitor, simulate rapid `down→healthy` polls, assert `can_vibe_call()` remains stable (no oscillation).

#### EC-D: Qwen process dies during TTS chunk
*   **File**: `test_health_monitor.py`
*   **Scenario**: QwenProcessManager reports `is_running=True` but the process has crashed (zombie).
*   **Risk**: Heavy TTS sends chunks to a dead server, user hears silence. `should_use_heavy_tts` returns True incorrectly.
*   **Test**: Set `_process.poll()` to return exit code, but `is_running` hasn't updated yet. Assert `should_use_heavy_tts` returns False after next poll.

---

### Priority 2 — Data integrity

#### EC-E: Persistence file corruption on model switch
*   **File**: `test_llm_engine_model_trace.py`
*   **Scenario**: `save_last_model` writes to disk, but the JSON is truncated (e.g., disk full, power loss).
*   **Risk**: Next startup crashes with `json.JSONDecodeError` instead of falling back to default.
*   **Test**: Write corrupted JSON to `last_model.json`, create motor. Assert `current_model == DEFAULT_MODEL` and no crash.

#### EC-F: Concurrent `save_last_model` calls
*   **File**: `test_llm_engine_model_trace.py`
*   **Scenario**: Two threads call `save_last_model` simultaneously (rapid tier switch + model switch).
*   **Risk**: File corruption from interleaved writes.
*   **Test**: Run 10 threads calling `save_last_model` with different models concurrently. Assert the final file is valid JSON with one of the expected models.

#### EC-G: History redaction bypass via XML encoding
*   **File**: `test_llm_engine_timeouts.py`
*   **Scenario**: LLM output contains `&#60;editorial_context&#62;` (HTML entity encoded XML tags).
*   **Risk**: Redaction regex doesn't catch encoded variants. Raw agenda prompts leak into conversation history.
*   **Test**: Pass entity-encoded `<editorial_context>` tags to `_commit_history`. Assert they're redacted.

---

### Priority 3 — UX robustness

#### EC-H: OllamaStartupManager with multiple bind failures
*   **File**: `test_ollama_startup.py`
*   **Scenario**: Ollama exits multiple times with "Only one usage of each socket address" before succeeding.
*   **Risk**: The UI gives up after first failure instead of retrying.
*   **Test**: Use FakeProcess that exits 3 times, then succeeds on 4th attempt. Currently only one attempt is tested.
    > *Real-world Ollama often needs 2-3 retries when Windows holds the port briefly after a crash.*

#### EC-I: Output guard with mixed-language text
*   **File**: `test_validation.py`
*   **Scenario**: LLM output mixes Spanish and English in the same sentence (common with bilingual models).
*   **Risk**: A Spanish pattern doesn't match because the trigger word is in English context, or vice versa.
*   **Test**: `"esta situación is so boring, nobody participates"` should trigger `no_negative_engagement`.

#### EC-J: Observer notification order during batch update
*   **File**: `test_ui_state.py`
*   **Scenario**: `state.update(ollama_state="ready", model_status="ready")` — does the observer receive `ollama_state` before or after `model_status`?
*   **Risk**: A `ModelPanel` observer that checks `ollama_state` when notified of `model_status` might see stale `ollama_state="checking"`.
*   **Test**: Subscribe observer, call `state.update()` with 3 keys. Assert all 3 notifications arrive with consistent snapshot values.

#### EC-K: Extremely long LLM output
*   **File**: `test_llm_engine_timeouts.py`
*   **Scenario**: Ollama returns 50KB+ of text (reasoning model with long thinking chain).
*   **Risk**: TTS chunking creates hundreds of chunks, exhausting memory or timing out.
*   **Test**: Mock `ollama.chat` to return a 10K-word response. Assert `_hablar` completes without OOM and within bounded time.

#### EC-L: RTFTracker with NaN from bad measurements
*   **File**: `test_health_monitor.py`
*   **Scenario**: `record(0.0, 1.0)` → RTF = 0.0 (valid), but `record(float('inf'), 1.0)` → RTF = inf.
*   **Risk**: Rolling average becomes NaN, propagates to `should_use_heavy_tts` decisions.
*   **Test**: Feed `inf` and `nan` wall_time values. Assert status never becomes NaN.

#### EC-M: Priority queue with identical timestamps
*   **File**: `test_llm_engine_timeouts.py`
*   **Scenario**: Two `enqueue` calls at the exact same `time.time()` (common on Windows with low-res timer).
*   **Risk**: Heap comparison falls through to comparing tuples where string comparison fails.
*   **Test**: Monkeypatch `time.time` to return constant. Enqueue 3 items with same priority. Assert no `TypeError` and correct FIFO order.

#### EC-N: Output guard sensitivity to Unicode homoglyphs
*   **File**: `test_validation.py`
*   **Scenario**: Malicious input uses Unicode lookalikes: `"como modelo de lenguаje"` (Cyrillic а instead of Latin a).
*   **Risk**: Regex-based guards miss the homoglyph, harmful output passes to TTS.
*   **Test**: Replace key characters with Cyrillic equivalents. Current guard likely passes this (expected RED).
