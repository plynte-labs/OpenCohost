# ADR-017: Chat-Activation Telemetry — Measure-First Instrumentation of Kira's Starvation

**Date**: 2026-06-23
**Status**: Implemented (committed) — awaiting owner RF3 runtime validation
**Branch**: `feat/akira-voseo-fix-and-cohost-adr`
**Author**: Claude Code orchestrator + dual blind adversarial review (Judgment Day, 2 sonnet judges)
**Scope**: `opencohost/smart_aggregator/filter_telemetry.py`, `opencohost/smart_aggregator/aggregator.py`, `opencohost/core/llm_engine.py`, `opencohost/ui/app_shell.py`, `opencohost/config/smart_aggregator.yaml` — metadata-only diagnostic instrumentation of the chat-activation pipeline. Belongs to the OpenCohost launch-readiness validation effort.

---

## Context

During an **RF3 (Chat Live)** runtime — Kira reacting to a high-traffic Spanish Twitch stream of roughly **2k viewers** — Kira was **STARVED**. She barely activated. To get a single reaction roughly every ~30s the owner had to drop the activation range to ~**0.1**, and even then sometimes had to nudge her manually.

The instinct in the moment was to *tune*: lower thresholds, clear token memory, store less chat so she stops repeating. But two different problems were being conflated:

1. **Repetition / mode-collapse** — already addressed by a separate chat repetition guard (`ed54f53`, documented in ADR-011 / ADR-015). That is a *generation* layer concern. It is **not** re-documented here.
2. **Starvation** — Kira receives almost no signal to act on. That is a *pipeline* concern, and it has a verified primary suspect.

The verified primary suspect is the **live-safety context-sampling gate**. At high traffic it decimates accepted chat by ~10x before it can ever reach intent aggregation:

* The high-traffic threshold is **10 msg/s** — `live_safety.high_traffic_threshold_per_second: 10.0` at `opencohost/config/smart_aggregator.yaml:69`.
* Above it, only **1 in 10** accepted messages is sampled into context — `high_traffic_sample_every: 10` at `opencohost/config/smart_aggregator.yaml:70`.

The gate logic confirms it: once high traffic is on, `_should_sample_for_context` keeps `1-in-N` only — `return self._live_sample_counter % self._live_safety_sample_every == 0` at `opencohost/smart_aggregator/aggregator.py:442`. A 2k-viewer stream sits far above 10 msg/s, so ~90% of otherwise-accepted chat is dropped *for context purposes* before Kira ever sees it.

But sampling is only one of **six** decision stages, and the activation clock is not the same as the clock that measures whether Kira *actually spoke*. Guessing which stage starves her — and tuning that one — would be a bet, not a fix.

**The discipline chosen: MEASURE WHERE the signal dies BEFORE changing any threshold.** No tuning, no presets, no preset switch in this change. Instrument the whole pipeline once, run one diagnostic session, read the breakdown, and only then personalize.

---

## Findings & Code Audit

### #A — The activation pipeline has six independently-lethal stages

* **Location**: `opencohost/smart_aggregator/filter_telemetry.py:38-44` (`FilterStage`)
* **The Code**:
  ```python
  class FilterStage(str, Enum):
      MESSAGE_FILTER = "message_filter"      # per-message junk gate (message_filter.py)
      QUALITY_GATE = "quality_gate"          # aggregator quality/spam gate
      CONTEXT_SAMPLING = "context_sampling"  # high-traffic 1-in-N decimation (the starvation vector)
      ACTIVITY_TRIGGER = "activity_trigger"  # rate / cooldown gate
      SHOULD_CALL = "should_call"            # ChatContextPacket decision
      QUEUE_TTL = "queue_ttl"                # priority-queue TTL expiry
  ```
* **The Decision**: model every stage as a closed enum so a single diagnostic run produces a per-stage accept/reject breakdown. `CONTEXT_SAMPLING` is the prime suspect, but `QUEUE_TTL` (a `should_call` that was raised and then expired unspoken) and `ACTIVITY_TRIGGER` cooldown can starve her just as effectively, and only data tells them apart.

### #B — Two clocks, because "decided to call" is not "actually spoke"

* **Location**: `opencohost/smart_aggregator/filter_telemetry.py:109-110` (`FilterDecision`)
* **The Code**:
  ```python
  secs_since_last_activation: float  # gap since the last should_call==True
  secs_since_last_spoken: float      # gap since Kira ACTUALLY spoke (separate clock)
  ```
* **The Issue**: a `should_call==True` does not guarantee Kira speaks — the turn can later expire in the motor's priority queue via TTL and never play. If the only metric were the activation clock, a stream full of raised-then-expired calls would *look* healthy while the owner hears silence.
* **The Decision**: keep two clocks. The activation clock advances on `SHOULD_CALL` accept (`opencohost/smart_aggregator/filter_telemetry.py:213-215`); the spoken clock advances *only* when a chat turn actually finished playing, via `mark_spoken()` (`opencohost/smart_aggregator/filter_telemetry.py:165-178`). A divergence between `spoken_gap_p50` and `activation_gap_p50` in the rollup is itself a finding: it points at TTL expiry / TTS failure rather than at the filter.

### #C — Safety-by-type: the record physically cannot carry chat content

* **Location**: `opencohost/smart_aggregator/filter_telemetry.py:99-112` (`FilterDecision`)
* **The Issue**: telemetry over chat is a privacy hazard. A field named `text` or `user` will eventually get filled.
* **The Decision**: the dataclass has **no** text/user/message field. It carries only an int `msg_len` (length, never the text — `opencohost/smart_aggregator/filter_telemetry.py:107`), closed enums (`stage`, `reason_code`, `msg_category`), and numbers. Reasons outside the enum are coerced, not stored verbatim: `_coerce_reason` returns `ReasonCode.UNKNOWN.value` on `ValueError` (`opencohost/smart_aggregator/filter_telemetry.py:118-122`). The invariant is enforced by a unit test that fails if any forbidden field is ever added:
  ```python
  for forbidden in ("text", "user", "username", "message", "raw", "content", "author"):
      assert forbidden not in fields, forbidden
  ```
  (`tests/test_filter_telemetry.py:38-41`)

### #D — Privacy win shipped alongside: the shadow-log leak is DELETED

* **Location**: `opencohost/smart_aggregator/aggregator.py:368-385` (the should_call telemetry that replaced it)
* **The Issue**: the old `Aggregator._log_input_contract_shadow` wrote chat-derived intent text plus the `ChatContextPacket` into the persisted live-safety log — a direct violation of the project rule "never expose raw chat in persistence." It shipped OFF by default but the leak path existed.
* **The Decision**: the method was **removed entirely** (commit `a8b8067`). The should_call decision is now captured metadata-only, reading *only* packet metadata — `packet.should_call_llm`, `packet.confidence`, `packet.total_messages` — never `to_dict()` / `to_prompt_context()` (which carry author+text):
  ```python
  packet = ChatContextPacketBuilder().build(context)
  self._telemetry.record(
      stage=FilterStage.SHOULD_CALL.value,
      accepted=packet.should_call_llm,
      ...
      score=packet.confidence, threshold=None, msg_len=0,
  ```
  (`opencohost/smart_aggregator/aggregator.py:373-383`). Two tests lock the deletion in: `test_input_contract_shadow_mode_off_by_default` and `test_shadow_log_leak_method_is_removed` assert `not hasattr(Aggregator, "_log_input_contract_shadow")` (`tests/test_input_contract_shadow_privacy.py:16-29`).

### #E — Off-by-default, zero-overhead hot path

* **Location**: `opencohost/smart_aggregator/filter_telemetry.py:35` and `opencohost/smart_aggregator/aggregator.py:137-151` (`_emit_decision`)
* **The Code**:
  ```python
  FILTER_TELEMETRY_ENABLED = False
  ```
  ```python
  if not self._telemetry.enabled:
      return  # hot-path fast exit — no record constructed
  ```
* **The Decision**: every seam fast-exits on a single bool when disabled — `record()` (`opencohost/smart_aggregator/filter_telemetry.py:193-194`), `mark_spoken()` (`opencohost/smart_aggregator/filter_telemetry.py:173-174`), and the aggregator's `_emit_decision` (`opencohost/smart_aggregator/aggregator.py:143-144`). In production the diagnostic costs one bool check and constructs nothing. Enabled via config:
  ```yaml
  chat_activation_diagnostics:
    enabled: false
    ring_size: 512
  ```
  (`opencohost/config/smart_aggregator.yaml:55-57`), read into the collector at `opencohost/smart_aggregator/aggregator.py:77-81`.

### #F — The cross-module seams are only attached when enabled

* **Location**: `opencohost/smart_aggregator/aggregator.py:202-209` (`attach_motor_telemetry_seams`)
* **The Code**:
  ```python
  def attach_motor_telemetry_seams(self, motor) -> None:
      if not self.diagnostics_enabled:
          return
      motor.on_chat_item_expired = self.on_chat_item_expired
      motor.on_chat_turn_spoken = self.on_chat_turn_spoken
  ```
* **The Decision**: the motor's two callbacks default to `None` (`opencohost/core/llm_engine.py:199-200`), and the composition root only wires them when diagnostics are on. In production the motor's behavior is byte-identical — the guards at `opencohost/core/llm_engine.py:636` (`on_chat_item_expired is not None`) and `opencohost/core/llm_engine.py:1563` (`source == "chat" and self.on_chat_turn_spoken is not None`) skip entirely. Composition root wiring is one line: `opencohost/ui/app_shell.py:2183`.

### #G — Operator reads it on demand

* **Location**: `opencohost/smart_aggregator/aggregator.py:128-132` (`get_diagnostics`)
* **The Code**:
  ```python
  def get_diagnostics(self) -> dict:
      diag = self._diagnostics.get_diagnostics()
      if self._telemetry.enabled:
          diag = {**diag, "activation_telemetry": self._telemetry.rollup()}
      return diag
  ```
* **The Decision**: the operator reads `aggregator.get_diagnostics()["activation_telemetry"]` (= `FilterTelemetry.rollup()`, `opencohost/smart_aggregator/filter_telemetry.py:235-260`), which returns per-stage accept/reject counts, an accept ratio, and the p50/p95 of both clocks — the six-cause breakdown that answers "where does chat signal die" without a second instrumentation pass. When disabled the key is simply absent.

---

## Decisions & Alternatives Considered

### Fork 1 — Tune now vs. measure first

This was the owner's live fork during the RF3 incident.

| Option | Summary | Verdict |
|---|---|---|
| Lower thresholds now | Drop activation range / sampling rate live to force reactions | **Rejected** — fixes the symptom by guessing the cause; the owner already had to push the range to ~0.1 and *still* nudge manually, which means the bottleneck may not be the threshold he was moving |
| Clear token memory / store less chat | Treat starvation as the repetition problem | **Rejected** — wrong layer. Repetition is a generation concern already handled by `ed54f53` (ADR-011/ADR-015). Conflating the two would "fix" starvation by degrading context |
| **Measure first (chosen)** | Instrument all six stages + two clocks, run one diagnostic session, read the breakdown, *then* personalize | **Selected** — the only path that distinguishes the six possible death sites before spending a tuning change on the wrong one |

**Rationale**: tuning before measuring is a bet on which of six gates is the killer. CONTEXT_SAMPLING is the strongest suspect (`opencohost/config/smart_aggregator.yaml:69-70`), but QUEUE_TTL and ACTIVITY_TRIGGER cooldown are live alternatives, and the activation-vs-spoken clock split (#B) can only be read from data. Instrument once, decide on evidence.

### Fork 2 — How to make the record privacy-safe

| Option | Summary | Verdict |
|---|---|---|
| Store text, redact on read | Keep `text` in the record, scrub it in `rollup()` | **Rejected** — the raw text still lives in memory and in `recent()`; one careless serialization re-introduces the exact leak `_log_input_contract_shadow` was deleted for |
| Allow-list reason strings at call site | Trust callers to pass only safe strings | **Rejected** — a free `reason` string is a future leak; relies on discipline, not type |
| **Safety-by-type (chosen)** | No text/user field on the dataclass; only `msg_len`, closed enums, numbers; unknown reasons coerced to `UNKNOWN` | **Selected** — the record *physically cannot* carry chat content, and a unit test fails the build if a forbidden field is ever added (`tests/test_filter_telemetry.py:38-41`) |

### Fork 3 — How the TTL/spoken seams cross the thread boundary

| Option | Summary | Verdict |
|---|---|---|
| Read live chat rate inside the TTL seam | Enrich the expiry record with current rate | **Rejected** — reading the activity window from the MOTOR worker thread touches chat-source state across threads; and TTL is an age-vs-ttl signal, not a rate signal |
| Always attach motor callbacks | Wire `on_chat_item_expired` / `on_chat_turn_spoken` unconditionally, gate inside | **Rejected** — production would carry live (if no-op) callbacks; a future edit could make them do work |
| **Record-only, enabled-gated attach (chosen)** | Motor callbacks stay `None` unless diagnostics on; TTL carries age in `score`, ttl in `threshold`, `chat_rate=0.0`; collector locks its mutation path | **Selected** — production behavior is byte-identical, the seam never alters queue lifetime or speech, and the cross-thread writes are lock-guarded |

---

## Edge Cases Considered

- **Cold start**: the collector seeds both clocks to `clock()` at construction (`opencohost/smart_aggregator/filter_telemetry.py:159-163`), so the first record reports a small, sane gap rather than a huge one.
- **Disabled path (production default)**: every seam fast-exits on `if not self.enabled` / `if not self._telemetry.enabled` before constructing anything; `get_diagnostics()` omits the key; motor callbacks are never even attached (`opencohost/smart_aggregator/aggregator.py:206-207`). Zero behavioral change.
- **Thread boundary**: in-aggregator seams record from the chat-source thread; QUEUE_TTL and the spoken clock fire from the MOTOR worker thread. Mutation/read methods take `self._lock` when enabled (`opencohost/smart_aggregator/filter_telemetry.py:152`), so counters never lose increments and `rollup()` never returns a torn snapshot.
- **Lock ordering**: the motor emits expiry telemetry *outside* `_pq_lock` (`opencohost/core/llm_engine.py:633-641`), so the queue lock is never held across the collector lock — the two locks stay order-independent and cannot deadlock.
- **TTS failure**: the spoken-clock advance is intentionally **not** in a `finally`. If `_hablar` raises, Kira did not speak, so the spoken gap keeps growing — and that growing gap is the very signal that surfaces silent TTS failures (`opencohost/core/llm_engine.py:1557-1567`).
- **Non-chat expiry / non-chat turn**: both seams are gated strictly on `source == "chat"`, so agenda items (`opencohost/core/llm_engine.py:627`) and direct turns (`opencohost/core/llm_engine.py:1563`) never fire the chat-only clocks.
- **Failure isolation**: every seam invocation is wrapped so a failing collector callback can never disturb the queue or the speech path (`opencohost/core/llm_engine.py:638-641`, `opencohost/smart_aggregator/aggregator.py:384-385`).

---

## Adversarial Review — What It Caught (value of the process)

Two blind sonnet judges (Judgment Day) reviewed the first pass. The hardening in commit `0edbdd7` is their output:

1. **Thread-safety hole (both judges)**: the first pass left `FilterTelemetry` mutation lock-free. The cross-module QUEUE_TTL seam and spoken clock fire from the MOTOR worker thread while the in-aggregator seams write from the chat-source thread — the counter read-modify-writes lose increments under contention, and `rollup()` can return a torn snapshot where `accepted + rejected != total`. Fixed by guarding every mutating/reading method with `self._lock` (`opencohost/smart_aggregator/filter_telemetry.py:176-177, 197, 232, 241`). Two stress tests now drive both entry points from 8 threads and assert losslessness and snapshot consistency (`tests/test_filter_telemetry.py:175-268`).
2. **Lock-ordering risk**: emitting the TTL telemetry while holding `_pq_lock` would nest the collector lock inside the queue lock. Moved the emit outside `_pq_lock` (`opencohost/core/llm_engine.py:633-641`) so the locks stay order-independent.

These were not visible in the green baseline because they only manifest under real cross-thread contention.

---

## Implementation Notes

- **Files touched**: `opencohost/smart_aggregator/filter_telemetry.py` (new collector), `opencohost/smart_aggregator/aggregator.py` (in-aggregator seams, handlers, `attach_motor_telemetry_seams`, deleted shadow log), `opencohost/core/llm_engine.py` (motor callbacks + TTL/spoken seams), `opencohost/ui/app_shell.py` (one composition-root line), `opencohost/config/smart_aggregator.yaml` (`chat_activation_diagnostics` block).
- **Commits**: `a8b8067` (in-aggregator seams + deleted the `_log_input_contract_shadow` author+text leak) and `0edbdd7` (cross-module seams + thread-safety hardening from the judge review).
- **TDD**: strict test-first. The cross-module file is explicitly RED-first ("the callbacks/handlers do not exist yet", `tests/test_chat_activation_telemetry_cross_module.py:18`). Coverage spans the type-safety invariant, both clocks, the rollup, both stress tests, the seam wiring, and the privacy deletion.
- **Test status**: full suite **2609 passed, 2 skipped** after `0edbdd7`.
- **Guard / debt**: the collector is metadata-only by construction; there is no tuning code in this change by design.

---

## Consequences

- **Positive**: one diagnostic session now answers "where does chat signal die" across all six stages plus the activation-vs-spoken divergence, with zero production overhead and zero raw-chat exposure. A real privacy leak (`_log_input_contract_shadow`) was removed as a side effect.
- **Deferred** — a separate FUTURE personalization track will add cadence presets (Calmado ~90s / Normal ~45s / Activo ~25-30s) with `sensitivity` / `target_cadence_sec` / `burst_mode` controls. **Do NOT tune any threshold (sampling rate, activation range, cooldown, TTL) until the RF3 runtime data exists.** The whole point of this ADR is to refuse that guess.
- **Runtime validation pending (owner)**: enable `chat_activation_diagnostics.enabled`, run one RF3 session against a high-traffic stream, and read `get_diagnostics()["activation_telemetry"]` — confirm whether CONTEXT_SAMPLING is the dominant death site or whether QUEUE_TTL / ACTIVITY_TRIGGER share the blame, and whether the spoken clock diverges from the activation clock.

---

**Related ADRs**: ADR-011 / ADR-015 (chat repetition guard — separate generation-layer concern, referenced not re-documented). This ADR is the umbrella; **ADR-018** covers the cross-module seam mechanism in depth, and **ADR-019** covers the thread-safety hardening and the adversarial review.
