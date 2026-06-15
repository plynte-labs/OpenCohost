# ADR-008: Main-Thread Freeze Elimination — Model-Install Cache, Async Agenda Load, Non-Blocking Prefetch, and StatusBar Marshaling

**Date**: 2026-06-14
**Status**: Implemented (committed `ada82d5`, awaiting owner runtime validation)
**Branch**: `feat/ui-polish-freeze-declutter-20260614`
**Author**: Claude Code orchestrator + dual adversarial review (Judgment Day)
**Scope**: `ui/model_panel.py`, `ui/app_shell.py` — eliminate blocking I/O on the Tk main thread. Phase 5 of the `ui_rendering_optimization_20260609` track.

---

## Context

The owner reported that on the **first startup after booting the PC**, entering the Co-host and Configuration panels froze the UI for 60+ seconds — "como si pulsaras un botón y tuviera falso" (controls behaving as if pressed, toggles reading stale/false values).

A read-only dual-angle audit (engram `ui/declutter-and-freeze-audit-20260614`, #1959) established a **correction to the mental model**: there is *no per-navigation prefetch that rebuilds the panel*. `_switch_product_tab` only toggles `grid()/grid_remove()` on already-built frames — cheap. The freeze is **blocking I/O executing on the single-threaded Tk event loop** that happens to fire around panel entry. CustomTkinter runs one event loop; any synchronous network/disk call on it freezes every panel.

Four independent defects were confirmed against the real code (the audit's main-thread-blocking angle was verdict NOT-refuted at 0.82 confidence; the phantom-input angle was narrowed by its verifier).

---

## Findings & Code Audit

### #A (P1, primary) — Unbounded `ollama.list()` on the Tk main thread

* **Location**: `ui/model_panel.py:533` (`_modelo_instalado`)
* **The Code (before)**:
  ```python
  def _modelo_instalado(self, model_tag: str) -> bool:
      try:
          import ollama
          for mod in ollama.list().models:        # NO timeout
              if mod.model == model_tag or mod.model == f"{model_tag}:latest":
                  return True
              ...
      except Exception:
          return False
  ```
* **The Issue**: `ollama.list()` uses the default client with no request timeout. It is reached **on the main thread** from `build()→update_model_info` at startup, the Configuration model combo `_on_model_changed`, the "Activar modelo" button, and `_safe_after` motor-event callbacks. On a cold Ollama after PC boot (service still starting, model registry/blobs not in OS file cache), a call that is normally milliseconds stalls for tens of seconds and freezes the whole UI.
* **Smoking gun**: the chat path *is* timeout-wrapped (`_create_ollama_chat_client(timeout=OLLAMA_CHAT_TIMEOUT)`) and the *other* `ollama.list()` site (`_discover_installed_model_tags`) *is* already off-loaded to a worker thread — the team knew this rule and missed this one site.

### #B — Blocking agenda prefetch wait

* **Location**: `ui/app_shell.py:1382 → core/llm_engine.py:423` (`wait_prefetched_agenda(timeout=0.35)`)
* **The Issue**: a 350 ms blocking wait fires per turn boundary on the Tk thread.

### #C — Synchronous SQLite agenda restore at startup

* **Location**: `ui/app_shell.py:211 → core/agenda_persistence.py:164` (`load_into`)
* **The Issue**: the agenda restore reads SQLite synchronously on the Tk thread before `mainloop`, up to ~500 ms.

### #D (defense-in-depth) — StatusBar constructed without the main-thread marshaler

* **Location**: `ui/app_shell.py:386`
* **The Issue**: `StatusBar` was built without `schedule_ui_update`, so its `UIState` observer callback ran `pill.configure()` inline on the `UIState-ObserverDispatcher` daemon thread (Tcl/Tk is not thread-safe). Every *other* panel passed `self._safe_after`. This is the leading candidate for the "phantom button / toggle reads false" symptom — narrowed by review to the `model_status` and `health_status` pills only (the other four pills are written via direct on-main calls).

---

## Decisions & Alternatives Considered

### #A: cache the installed-tag set (chosen)

Three options were weighed (engram design #1964):

| Option | Summary | Verdict |
|---|---|---|
| **1 — cache (chosen)** | `_installed_model_cache: set[str]` populated by the existing `_refresh_model_list_async` worker inside `_apply_model_catalog`; `_modelo_instalado` reads it O(1) | **Selected** — reuses the already-trusted async worker, zero new threads, tag-match logic stays in one place |
| 2 — async per-call | make `_modelo_instalado` fully async (worker + callback) | Rejected — 3 call-sites each flicker independently; `_descargar_o_activar_modelo` gates a download on the bool, making async structurally awkward |
| 3 — bounded timeout | add a short timeout to `ollama.list()` | Rejected — still blocks the main thread (just shorter); masks the architecture instead of fixing it |

**Critical correction from the design**: the cache write must happen in `_apply_model_catalog` **before** the `update_model_info` UI calls (which read it), guarded by `if installed_model_tags is not None` so an Ollama-DOWN refresh never wipes the cache.

* **#B**: call with `timeout=0` (non-blocking; `llm_engine` already supports it) and reschedule via a cancellable `after(50)` retry (`_prefetch_retry_id`).
* **#C**: move `load_into` into a daemon worker that posts `_on_agenda_loaded` via `after(0)`; exception-guarded so the topic-inbox bridge is always constructed.
* **#D**: pass `schedule_ui_update=self._safe_after` — a one-line correctness fix applied regardless of whether the phantom-input symptom is later reproduced.

---

## Edge Cases Considered

- **Cache cold-start**: empty cache returns a conservative `False` and schedules a refresh; the UI self-heals within ~250 ms.
- **Cache staleness**: invalidated on download-complete (`_on_motor_download_done → _invalidate_model_cache`). No model-remove UI exists today, so no remove hook is needed.
- **Thread safety**: the cache is written only via `_apply_model_catalog` (marshaled to main) and read only on main — GIL + main-thread-only access, no lock required.
- **Ollama DOWN vs slow**: a DOWN refresh passes `installed_model_tags=None`; the not-None guard preserves the last-good cache.
- **Tag matching**: `_canonical_model_tag` strips `:latest`, so the 3-way match collapses to exact + prefix (the `:latest` branch was dead and was removed).
- **#B teardown**: the retry is cancelled in `on_closing`, `_kira_agenda_emergency_stop`, and `_kira_agenda_clear_prefetch`, and guarded by `_closing`.
- **#C async load**: callers between init and the callback see an empty-but-valid agenda (idle tick handles it); `_on_agenda_loaded` guards teardown first.

---

## Adversarial Review — What It Caught (value of the process)

Two blind judges + two re-judge rounds (Judgment Day) found issues the 174-test green baseline and the first TDD pass missed:

1. **Real regression (both judges)**: the new `_topic_inbox_bridge is None` guard in `_kira_agenda_update_status` used `return`, exiting the whole method and **skipping the BUG-003 TTS pause-pill update** during the startup window. Fixed to skip only the suggestions call.
2. **New leak introduced by a fix (re-judge)**: the #A robustness re-probe added an un-cancelled `threading.Timer` — the same leak class just fixed for `_prefetch_retry_id`. Now deduped + cancelled in `cleanup()`; `on_closing` now calls `model_panel.cleanup()` (it never did before).
3. **Daemon-thread exception hole**: `load_into` raising would leave `_topic_inbox_bridge` `None` all session. Wrapped so `_on_agenda_loaded` always fires.
4. **Test-isolation leak**: new tests spawned real `ollama.list()` daemon threads; the `model_panel` fixture's `patch.dict("sys.modules", {customtkinter})` exit calls `sys.modules.clear()`, and if a background thread imported `ollama` inside that window it was absent from the snapshot → finalized → `ollama.chat` vanished for a later test. Fixed by pre-importing `ollama` at module top + a no-real-Timer autouse fixture. **Lesson recorded: UI test changes must be verified with the broad multi-file run, not only the focused suite.**

---

## Implementation Notes

- Files: `ui/model_panel.py` (+85), `ui/app_shell.py` (+112), and four test files. `llm_engine.py`/`agenda_persistence.py` were unchanged (their support already existed).
- Strict TDD throughout (test-first). Behavioral tests replaced earlier source-inspection tautologies.
- The `agenda_persistence` regression guards were updated to the async contract — they now assert `apply_session_settings` then `_kira_agenda_update_status` ordering **inside `_on_agenda_loaded`**, preserving the PR #41 "visible restore" intent.
- `app_shell.py` line-count guard raised 3100 → 3200 with documented debt; full decomposition stays owned by `ui_rendering_optimization_20260609`.
- Final: **526 passed, 2 skipped** across the model / agenda / health / integration suites.

---

## Consequences

- **Positive**: the unbounded cold-boot freeze is removed at its root; the bar can no longer corrupt Tk state off-thread.
- **Deferred**: `_detectar_estado_ollama`'s bounded 1 s `requests.get` stays on-main (marked TODO for a future Phase 6); the phantom-input *root cause* is only narrowed, not proven — the marshaler fix is applied as defense and a runtime repro is still owed.
- **Runtime validation pending (owner)**: a real cold boot to confirm the 60 s+ freeze is gone.
