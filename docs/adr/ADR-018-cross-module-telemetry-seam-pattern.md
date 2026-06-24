# ADR-018: Cross-Module Telemetry Seams — Optional Callbacks, Chat-Only Gating, and RECORD-ONLY Discipline

**Date**: 2026-06-23
**Status**: Implemented (committed) — awaiting owner RF3 runtime validation
**Branch**: `feat/akira-voseo-fix-and-cohost-adr`
**Author**: Claude Code orchestrator + dual blind adversarial review (Judgment Day, 2 sonnet judges)
**Scope**: `opencohost/core/llm_engine.py`, `opencohost/smart_aggregator/aggregator.py`, `opencohost/smart_aggregator/filter_telemetry.py`, `opencohost/ui/app_shell.py`, `opencohost/config/smart_aggregator.yaml` — the chat-activation diagnostics instrumentation under the OpenCohost launch-readiness measure-first effort. Commits `a8b8067` (in-aggregator seams + shadow-log deletion) and `0edbdd7` (cross-module seams + thread-safety hardening).

---

## Context

The chat-activation pipeline has six sequential gates (message filter, quality gate, context sampling, activity trigger, should_call, queue TTL) and the owner needs to answer one operational question during a real runtime: **why is Kira under-fed at high traffic?** Answering it requires METADATA about every filter decision — counts, thresholds, gap percentiles — but never the chat text or usernames, which the project prohibits from logs, prompts, and persistence (`opencohost/config/smart_aggregator.yaml:86`, "Raw chat persistence is prohibited").

ADR-017 is the umbrella for that measure-first telemetry. The collector itself — `FilterTelemetry` in `opencohost/smart_aggregator/filter_telemetry.py` — is metadata-only by construction: `FilterDecision` (`filter_telemetry.py:99-115`) physically has no text/user field, so a decision record cannot carry chat content. The five **in-aggregator** seams (covered by ADR-017) all fire from the chat-source thread inside the aggregator, where the collector lives — same module, same object.

This ADR documents the harder problem: **two of the six gates live in a different module on a different thread.** The queue-TTL gate runs inside `MotorVocalIA._process_priority_queue` (`opencohost/core/llm_engine.py:604`), and the "Kira actually finished speaking" event happens inside `MotorVocalIA._ejecutar_inferencia` (`llm_engine.py:1552`). Both run on the **motor worker thread**. The collector lives in the aggregator. We must feed the collector from the motor without (a) coupling the motor to the aggregator, (b) changing any motor behavior when diagnostics are off, or (c) ever firing for non-chat sources.

The prior mental model was a privacy hazard. Before `a8b8067`, the should_call path used a `_log_input_contract_shadow` log that captured author + text — a raw-chat leak. That shadow log was **deleted** in `a8b8067` and replaced by a metadata-only `should_call` record that reads packet metadata only (`aggregator.py:368-385`, "never to_dict()/to_prompt_context()"). This ADR's cross-module seams extend that same metadata-only discipline across the module boundary.

---

## Findings & Code Audit

### #A — The motor exposes two OPTIONAL callbacks, default `None`

* **Location**: `opencohost/core/llm_engine.py:199-200` (declared in `MotorVocalIA.__init__`)
* **The Code**:
  ```python
  self.on_chat_item_expired = None   # (info: dict) — a chat queue item expired (TTL)
  self.on_chat_turn_spoken = None    # () — a chat turn finished speaking
  ```
* **The Decision**: the motor knows nothing about the aggregator or the collector. It exposes two seams as plain optional callbacks. The in-module comment (`llm_engine.py:193-198`) states the contract: app_shell sets these ONLY when chat diagnostics are enabled; in production they stay `None` so the guards skip and behavior is **byte-identical**; they fire from the worker thread into the aggregator's lock-guarded collector; **RECORD-ONLY** — neither changes queue lifetime or speech behavior; gated strictly on `source == "chat"`.

### #B — Seam 1 (QUEUE_TTL): collect inside the lock, fire OUTSIDE the lock

* **Location**: `opencohost/core/llm_engine.py:616-641` (`_process_priority_queue`)
* **The Code** (expiry branch, inside `_pq_lock`):
  ```python
  if prio > 0 and (now - ts) > self._pq_ttl_seconds:
      self._log(f"Item expirado y omitido (TTL {self._pq_ttl_seconds:.0f}s): {source}")
      # Measure-first telemetry seam: record (never alter) chat expiries.
      # Captured here, emitted below OUTSIDE _pq_lock.
      if source == "chat":
          expired_chat_infos.append({"age_sec": now - ts, "ttl_sec": self._pq_ttl_seconds})
  else:
      kept.append(item)
  ```
  And the emit, **after** the lock is released (`llm_engine.py:636-641`):
  ```python
  if expired_chat_infos and self.on_chat_item_expired is not None:
      for info in expired_chat_infos:
          try:
              self.on_chat_item_expired(info)
          except Exception:
              pass
  ```
* **The Issue / The Decision**: the original placement fired the callback while `_pq_lock` was held — that would have held the queue lock across the aggregator collector's lock, an order-dependent two-lock pattern (the deadlock/teardown hazard ADR-019 dissects). The fix is to **collect** the expired-chat infos into a local list inside the lock, then **fire** outside it (`llm_engine.py:633-635`: "so the queue lock is never held across the aggregator collector's lock — the two locks stay order-independent"). The seam is RECORD-ONLY: it never touches `self._pq_ttl_seconds` (`llm_engine.py:169`) and never changes which items land in `kept` — the `else: kept.append(item)` branch is unaffected by the `if source == "chat"` capture. Expiry happens identically whether or not the seam is wired.

### #C — Seam 2 (spoken clock): fire only after a chat turn actually played, NOT in a finally

* **Location**: `opencohost/core/llm_engine.py:1552-1567` (`_ejecutar_inferencia`)
* **The Code**:
  ```python
  if dialogo:
      self._hablar(dialogo, source=source)
      ...
      if source == "chat" and self.on_chat_turn_spoken is not None:
          try:
              self.on_chat_turn_spoken()
          except Exception:
              pass
  ```
* **The Decision**: the spoken-clock seam fires AFTER `_hablar` returns, so it advances the spoken clock only when Kira truly finished speaking a chat turn. The comment (`llm_engine.py:1560-1562`) is load-bearing: it is **intentionally NOT in a `finally`** — if `_hablar` raises (a TTS failure), Kira did NOT speak, so the spoken gap should keep growing. That growing `secs_since_last_spoken` gap is the very signal that surfaces silent TTS failures to the operator. Wrapping it in `finally` would falsely advance the clock on a failed turn and hide the bug the telemetry exists to catch.

### #D — The aggregator owns the handlers AND a `diagnostics_enabled` property

* **Location**: `opencohost/smart_aggregator/aggregator.py:166-200`
* **The Code** (the gating property + the QUEUE_TTL handler):
  ```python
  @property
  def diagnostics_enabled(self) -> bool:
      return bool(self._telemetry.enabled)

  def on_chat_item_expired(self, info: dict) -> None:
      if not self._telemetry.enabled:
          return
      ttl = info.get("ttl_sec")
      self._telemetry.record(
          stage=FilterStage.QUEUE_TTL.value,
          accepted=False,
          reason=ReasonCode.TTL_EXPIRED.value,
          score=float(info.get("age_sec", 0.0)),
          threshold=float(ttl) if ttl is not None else None,
          msg_len=0,
          chat_rate=0.0,
          msg_category=MsgCategory.NA.value,
      )
  ```
* **The Decision**: the aggregator (which owns the collector) owns the handler logic. `on_chat_item_expired` maps the motor's raw `{age_sec, ttl_sec}` onto a `QUEUE_TTL` / `TTL_EXPIRED` record where **age is carried in `score` and the TTL limit in `threshold`**. Note `chat_rate=0.0` is deliberate (`aggregator.py:178-180`): reading the live rate here would touch the activity window from the wrong thread, and the TTL signal is age-vs-ttl, not rate. `on_chat_turn_spoken` (`aggregator.py:195-200`) simply forwards to `self._telemetry.mark_spoken()` after a disabled fast-exit. Both handlers fast-exit when disabled, so they are no-ops even if a callback were somehow attached while off.

### #E — Wiring is encapsulated in `attach_motor_telemetry_seams`, gated on `diagnostics_enabled`

* **Location**: `opencohost/smart_aggregator/aggregator.py:202-209`
* **The Code**:
  ```python
  def attach_motor_telemetry_seams(self, motor) -> None:
      if not self.diagnostics_enabled:
          return
      motor.on_chat_item_expired = self.on_chat_item_expired
      motor.on_chat_turn_spoken = self.on_chat_turn_spoken
  ```
* **The Decision**: the aggregator attaches its own seams to the motor, and only when diagnostics are enabled. In PRODUCTION (disabled, `smart_aggregator.yaml:55-57` `enabled: false`) the early `return` means the motor's callbacks **stay `None`** — the motor's behavior is byte-identical, no callback ever fires, no guard ever evaluates true. This is a single gate, in the module that owns the collector, instead of a conditional block in the UI shell.

### #F — The composition root calls it in ONE line

* **Location**: `opencohost/ui/app_shell.py:2183` (in `_init_smart_aggregator`, declared `app_shell.py:2142`, called from `__init__` at `app_shell.py:231`)
* **The Code**:
  ```python
  self.smart_agg.attach_motor_telemetry_seams(self.motor_ia)  # measure-first; no-op unless enabled
  ```
* **The Decision**: the composition root wires the two modules together with exactly one line, alongside the other `smart_agg.on_*` assignments (`app_shell.py:2175-2182`). No gating logic, no callback bodies, no TTL knowledge in the shell. The shell only knows "introduce the motor to the aggregator's seams"; the aggregator decides whether to accept the introduction.

### #G — Boundary safety: both seams gated STRICTLY on `source == "chat"`

* **Location**: `opencohost/core/llm_engine.py:627` (TTL) and `opencohost/core/llm_engine.py:1563` (spoken)
* **The Issue / The Decision**: the project rule "keep LiveVoice and PTT separate — do not merge" means telemetry meant for chat starvation must NEVER fire for `ptt`, `direct`, `accumulated`, or `kira-agenda` sources. Both motor sites carry an explicit `source == "chat"` guard. The TTL site captures the expiry info only when `source == "chat"` (`llm_engine.py:627`) even though a non-chat item (agenda, prio 2) expires through the same loop; the spoken site fires only when `source == "chat"` (`llm_engine.py:1563`). The cross-module tests pin this: `test_non_chat_ttl_expiry_does_not_fire_chat_seam` (`tests/test_chat_activation_telemetry_cross_module.py:80-92`) expires a `kira-agenda` item and asserts the chat seam stays silent while the item is still dropped (lifetime unchanged); `test_non_chat_turn_does_not_fire_spoken_seam` (`tests/test_chat_activation_telemetry_cross_module.py:122-132`) plays a `direct` turn and asserts `spoken == []`.

---

## Decisions & Alternatives Considered

### Fork 1 — How does the motor feed the aggregator's collector?

| Option | Summary | Verdict |
|---|---|---|
| **1 — optional callbacks on the motor (chosen)** | Motor exposes `on_chat_item_expired` / `on_chat_turn_spoken`, default `None`, guarded by `is not None` + `try/except`; aggregator owns handlers and attaches them | **Selected** — zero coupling (motor never imports the aggregator), byte-identical when off, RECORD-ONLY, and a failing callback cannot disturb queue processing or speech |
| 2 — motor imports/holds an aggregator reference | Give `MotorVocalIA` a `telemetry` collaborator it calls directly | Rejected — couples a core engine module to the diagnostics subsystem; the motor would carry collector knowledge into production even when disabled |
| 3 — shared global/singleton collector | Both modules reach a module-level telemetry singleton | Rejected — hidden global state, hard to test, and no clean off-switch; production would still import the path |

The callback pattern keeps the dependency arrow pointing the right way: the aggregator (diagnostics) depends on the motor's seam shape, never the reverse.

### Fork 2 — Where does the gating + wiring live: app_shell or the aggregator?

`opencohost/ui/app_shell.py` is a thin composition shell under a **hard line-count guard**: `test_app_shell_line_count_under_1500` in `tests/test_integration.py:203-217` asserts `len(lines) < 3270`. At the time of this change the file was ~1 line under that ceiling. A multi-line wiring block (gate on enabled, then two assignments) inside `_init_smart_aggregator` would have pushed it over and broken the guard.

| Option | Summary | Verdict |
|---|---|---|
| **1 — encapsulate gating+wiring in `aggregator.attach_motor_telemetry_seams`, ONE line in app_shell (chosen)** | The aggregator owns the `diagnostics_enabled` gate and the two motor assignments; app_shell adds a single call | **Selected** — keeps the thin-composition shell within budget AND is better encapsulation: the aggregator owns attaching its own seams; the shell stays declarative |
| 2 — raise the line-count ceiling with documented debt | Bump `< 3270` (the established ratchet pattern from ADR-008) and write the gate inline in app_shell | Rejected — the `ui_rendering_optimization_20260609` track owns SHRINKING `app_shell`; growing it for a diagnostics block is the wrong direction and inverts that track's intent |

The ratchet (raise-the-ceiling) is a legitimate house pattern (ADR-008 raised it 3100 → 3270), but it is for unavoidable growth. Here the growth WAS avoidable by putting the logic where it belongs.

---

## Edge Cases Considered

- **Cold start / disabled path (production)**: `smart_aggregator.yaml:55-57` ships `enabled: false`. `attach_motor_telemetry_seams` early-returns (`aggregator.py:206-207`), the motor callbacks stay `None`, and every motor guard (`llm_engine.py:636`, `llm_engine.py:1563`) is false. Behavior is byte-identical. Pinned by `test_attach_seams_when_disabled_leaves_motor_callbacks_none` (`tests/test_chat_activation_telemetry_cross_module.py:198-203`) and `test_default_seam_callbacks_are_none` (`tests/test_chat_activation_telemetry_cross_module.py:57-61`).
- **Callback unset but expiry/turn still happens**: the `is not None` guards mean an expiry or a spoken turn with no callback is a safe no-op. Pinned by `test_ttl_expiry_with_no_callback_is_safe` (`cross_module.py:95-104`) and `test_spoken_callback_with_no_callback_is_safe` (`cross_module.py:135-143`).
- **Thread boundary**: the in-aggregator seams write from the chat-source thread; these two cross-module seams write from the motor worker thread (`filter_telemetry.py:13-19`). The collector guards its mutation path with a lock when enabled (`filter_telemetry.py:152`, `197`); the disabled hot path is lock-free. ADR-019 covers that collector locking in full.
- **Lock-ordering failure mode (QUEUE_TTL)**: capturing inside `_pq_lock` and firing outside it (`llm_engine.py:633-641`) prevents holding the queue lock across the collector lock — no order-dependent two-lock chain.
- **TTS failure mode (spoken clock)**: the seam is deliberately outside any `finally` (`llm_engine.py:1560-1562`); a `_hablar` raise leaves the spoken gap growing, which is the operator's silent-TTS-failure signal.
- **Non-chat sources**: agenda/ptt/direct/accumulated never fire either seam (`source == "chat"` guards, Finding #G).
- **Failing callback**: every fire site is `try/except` (`llm_engine.py:638-641`, `1564-1567`), so a misbehaving diagnostics handler can never disturb queue processing or speech.

---

## Adversarial Review — What It Caught (value of the process)

The dual blind review (Judgment Day, two sonnet judges) on the seam commit caught the lock-ordering hazard that the first TDD-green pass missed: the QUEUE_TTL seam originally fired `on_chat_item_expired` **while `_pq_lock` was held**, which would hold the motor's queue lock across the aggregator collector's lock — an order-dependent two-lock pattern. The fix (committed in `0edbdd7`) was to collect expired-chat infos into a local list inside the lock and fire the callback after releasing it (`llm_engine.py:616-641`). The thread-safety reasoning the review surfaced is documented in full in ADR-019; this ADR records that the cross-module seam's lock discipline came out of that review, not the initial implementation.

---

## Implementation Notes

- **Files touched**: `opencohost/core/llm_engine.py` (two optional callbacks + two guarded fire sites), `opencohost/smart_aggregator/aggregator.py` (two handlers + `diagnostics_enabled` property + `attach_motor_telemetry_seams`), `opencohost/ui/app_shell.py` (one wiring line, `app_shell.py:2183`), `opencohost/config/smart_aggregator.yaml` (`chat_activation_diagnostics` block, lines 55-57). The shadow-log leak (`_log_input_contract_shadow`, author+text) was DELETED in `a8b8067` and replaced by the metadata-only `should_call` record (`aggregator.py:368-385`).
- **TDD**: the cross-module spec file `tests/test_chat_activation_telemetry_cross_module.py` was written RED first (its docstring, `cross_module.py:18`: "RED today: the callbacks/handlers do not exist yet") and drove the implementation. It covers default-None seams, TTL-fires-callback, non-chat-does-not-fire, no-callback-is-safe, spoken-fires/non-chat-silent, the aggregator handlers, the `diagnostics_enabled` property, and both `attach_motor_telemetry_seams` branches. The in-aggregator collector contract is covered by `tests/test_filter_telemetry.py` and `tests/test_chat_activation_telemetry.py`.
- **Guards**: `app_shell.py` stays under the `< 3270` line guard (`tests/test_integration.py:203-217`) because the wiring is a single line.
- **Test status**: full suite **2609 passed, 2 skipped** after `0edbdd7`.

---

## Consequences

- **Positive**: the motor can feed the aggregator's collector with zero coupling and byte-identical production behavior. The author+text shadow-log leak is gone; everything that crosses the boundary is metadata-only (`{age_sec, ttl_sec}` and a bare spoken signal). The chat-only gating preserves the LiveVoice/PTT separation rule. The composition shell grew by exactly one line, respecting the `ui_rendering_optimization` track's shrink mandate.
- **Deferred**: no operator UI surfaces the cross-module records yet; they land in `get_diagnostics()["activation_telemetry"]` (`aggregator.py:128-132`) for on-demand reading. The collector's full thread-safety rationale is owned by ADR-019.
- **Runtime validation pending (owner)**: the seams are off-by-default and have never run against real high-traffic chat with a stalling/heavy model. The owner's RF3 runtime validation must enable `chat_activation_diagnostics` during a live session and confirm the QUEUE_TTL and spoken-gap metadata actually answer the "why is Kira under-fed at high traffic" question.

---

**Cross-links**: ADR-017 (umbrella — why measure-first chat-activation telemetry exists, in-aggregator seams). ADR-019 (thread-safety of the `FilterTelemetry` collector these seams feed). Prior context: `ed54f53` chat repetition guard (ADR-011 / ADR-015) — referenced, not re-documented here.
