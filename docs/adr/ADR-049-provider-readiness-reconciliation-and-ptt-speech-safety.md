# ADR-049: Provider Readiness Reconciliation, Atomic Model Switching, and PTT Speech Safety

- **Status:** Accepted & Implemented
- **Date:** 2026-08-25
- **Context:** `opencohost/core/llm_engine.py`, `opencohost/core/engine/llm_engine_models.py`, `opencohost/core/engine/llm_engine_cloud.py`, `opencohost/core/engine/llm_engine_speech.py`, `OpenCohost_UI/src/api/engineCommand.ts`

---

## 1. Context & Symptoms

During active multi-provider LLM usage (transitioning between Cloud providers like OpenAI/Nemotron and Local Ollama instances) and rapid model switching (`gemma4:e2b` to `gemma4:e4b`), the following regressions were observed:

1. **Spurious `ollama_unavailable` / `model_switch_failed`:** After transitioning from Cloud to Local, selecting any local model failed immediately with a rejection stating that Ollama was not available, despite the daemon running healthily on `127.0.0.1:11434`.
2. **UI Command Hangs:** The web UI remained disabled/pending for 15 seconds upon model switch failures due to missing action-level event discrimination.
3. **PTT Speech Cut Failures (`AttributeError`):** Pressing the PTT hotkey while Kira was speaking during agenda playback failed to interrupt audio due to an uninitialized attribute reference in `pause_speech_for_ptt`.
4. **Fictitious Tier Switches:** `switch_llm_tier` emitted successful switch events without preparing the target model when `is_ready` was false.

---

## 2. Hypotheses & Investigation

| Hypothesis | Verification | Conclusion |
| :--- | :--- | :--- |
| **H1: Ollama Daemon Crash / OOM** | Verified via direct HTTP ping to `/api/tags` and process inspection. Daemon was responsive. | **Rejected** |
| **H2: In-Memory `is_ready` State Drift** | Inspected `set_provider_config()`: unconditionally set `self.is_ready = False` on Cloud->Local and Local->Local without executing a live probe. | **Confirmed** |
| **H3: Uninitialized Mixin State in Speech Pipeline** | Inspected `pause_speech_for_ptt()`: accessed `self._promotion_state` directly without `getattr()`. | **Confirmed** |
| **H4: Stale Probe Epoch Races** | A slow asynchronous local probe started before a switch to Cloud could complete late and erroneously mark `self.is_ready = True` under Cloud posture. | **Confirmed** |

---

## 3. Root Cause Analysis

1. **Readiness Degeneration in `set_provider_config()`:**
   ```python
   # OLD DEFECTIVE CODE:
   self._provider_config = cfg
   self.is_ready = (incoming_provider != "local")  # Always set to False when switching to Local
   ```
2. **Missing Active Reprobe in `switch_model()`:**
   ```python
   # OLD DEFECTIVE CODE:
   if not self.is_ready:
       self._log(f"Switch a {modelo} rechazado: Ollama no esta listo.", tipo="warning")
       if self.ui_callback:
           self.ui_callback("model_switch_failed")
       return  # Bailed out without probing Ollama!
   ```
3. **Unsafe Attribute Access in `pause_speech_for_ptt()`:**
   ```python
   # OLD DEFECTIVE CODE:
   if self._promotion_state == _PromotionState.RUNNING:  # Threw AttributeError on standard instances!
   ```

---

## 4. Architectural Solutions & Code Patterns

### A. Centralized Bounded Probe & Epoch Reconciliation (`llm_engine_models.py`, `llm_engine_cloud.py`)
Introduced `_reconcile_local_readiness()` which triggers a bounded 3.0s timeout probe (`_select_ollama_probe_client`) and verifies that `provider_epoch` has not shifted before mutating `is_ready`:

```python
def _reconcile_local_readiness(self) -> bool:
    with self._lock:
        if not self._cfg_is_local(self._provider_config):
            return False
        epoch = self.provider_epoch

    success = self._probe_ollama_service()

    with self._lock:
        if self.provider_epoch == epoch and self._cfg_is_local(self._provider_config):
            self.is_ready = bool(success)
            return self.is_ready
        return False
```

### B. Preserving Readiness on `Local -> Local` PUTs (`llm_engine_cloud.py`)
```python
if previous_provider != incoming_provider:
    self.provider_epoch += 1
    self.is_ready = (incoming_provider != "local")
    provider_changed = True
```

### C. Safe PTT Interruption Seam (`llm_engine_speech.py`)
Guarded `_promotion_state` access with `getattr()` to ensure `SpeechRouter.hold_and_pause("ptt")` always executes reliably:

```python
promotion_state = getattr(self, "_promotion_state", None)
if promotion_state is not None:
    running_state = getattr(_eng._PromotionState, "RUNNING", None)
    if running_state is not None and promotion_state == running_state:
        self._promotion_ptt_held = True

return self._ensure_router().hold_and_pause("ptt")
```

### D. Protocol-Level Fast Failure in UI (`OpenCohost_UI/src/api/engineCommand.ts`)
Mapped specific action failure codes (`model_switch_failed`, `llm_tier_switch_failed`) to immediately clear pending states with `isFailed: true` without hitting the 15-second timeout.

---

## 5. Verification & Test Evidence

* **Backend Test Suite (`pytest`):**
  * `tests/test_provider_model_switch_reconciliation.py`: 15 dedicated regression tests for Cloud->Local, Local->Local, bounded probes, stale epoch rejection, and atomic model switches.
  * `tests/test_interruption_connector.py`: Verified `pause_speech_for_ptt` on uninitialized engine instances.
  * **Full Suite:** `5,784 passed, 15 skipped in 538.95s` (100% green).
* **Frontend Test Suite (`Vitest`):**
  * `OpenCohost_UI/src/api/engineCommand.test.ts`: 11 tests verifying fast failure resolution.
  * **Full Suite:** `90 test files passed, 1,206 tests passed in 98.53s` (100% green).
* **Live Runtime Validation:**
  * Verified seamless multi-step switching: `openai` -> `nvidia_nemotron` -> `Local` -> `gemma4:e4b` -> `PTT speech cut & Kira reply generation`.

---

## 6. Out of Scope / Delegated Proposals

The following tasks are preserved for subsequent tracks:
1. **Semantic & Hybrid Memory:** Qdrant local persistent vector sidecar + SQLite MemoriaStore canonical storage.
2. **Prototyping Dead Code Cleanup:** Removal of unreferenced `StreamingSpeechPipeline` and legacy test files.
3. **Background Deferred Promotion Scheduler:** Periodic 30s-idle trigger (`_tick_memoria_promotion`).
