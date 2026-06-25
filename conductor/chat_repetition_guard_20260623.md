# Chat-Reactive Repetition Guard — Implementation Record (2026-06-23)

Implements the chat-path slice of the **Cohost Repetition Handling** track (ADR-011 /
ADR-015), driven by the real RF3 run analysis in `rf3_run_analysis_20260623.md`. Built
inline, strict-TDD, single-writer. **NOT committed** (owner commits).

## What landed

Two verified fixes, both gated to `source == "chat"` (the **chat-reactive** scope — see
"Honest scope" below):

- **FIX 1 — sampling brake.** `opciones_llm` now adds `repeat_penalty=1.2`,
  `presence_penalty=0.5`, `frequency_penalty=0.5` for chat-reactive turns
  (`llm_engine.py`, right after the reasoning branch, before `start_llm`). Preventive:
  stops the model entering the high-probability attractor. Additive — non-chat options
  stay byte-identical.
- **FIX 2 — reactive structural guard.** A new pure module `opencohost/core/repetition_guard.py`
  detects both verbatim duplicates and synonym-swap TEMPLATE repetition, then reuses the
  proven `_guardrail_fallback_line` seam to emit a neutral line and **returns before
  `_commit_history`**, so the suppressed line never poisons the history window that feeds
  the next prompt.

### The detector (the part the adversarial review fixed)

Layer 2 (scaffold) is **threshold-free**: mask every non-function-word token to `#`,
collapse consecutive masks, compare skeleton **sequences by equality**, require
`min_slots >= 2`. The first design used char-shingle Jaccard ≥ 0.85 over the skeleton and
a blind reviewer proved it MISSED its own headline case — `"…abismo sin fin"` vs
`"…abismo sin salida"` scored 0.71 because `fin` (3 chars) escaped the `len>=4` mask while
`salida` was masked, so the skeleton tails diverged. A scalar threshold cannot accept 0.71
and reject 0.64; equality + a slot rail can.

## Footprint

| File | New/Edited | What |
|---|---|---|
| `opencohost/core/repetition_guard.py` | new | pure module, stdlib only (~150 LOC) |
| `tests/test_repetition_guard.py` | new | Suite A — 10 unit |
| `tests/test_chat_repetition_guard.py` | new | Suite B — 7 integration (incl. isolation + default-enqueue freeze) |
| `opencohost/core/llm_engine.py` | edited | import + FIX1 block + FIX2 block (~25 LOC, additive) |
| `opencohost/config/settings.py` | edited | 3 `CHAT_*_PENALTY` constants |

## Verification

- New: **17 passed** (Suite A + B).
- Focused (CLAUDE.md tiers/panel/recovery): **98 passed**.
- Generation-path regression (11 files incl. agenda controller + chaos stream): **504 passed**.
- **619 total, 0 failures.** Isolation pinned by `test_chat_penalties_do_not_apply_to_other_sources`.

Run command:
```
E:\Miniconda\envs\flux_env\python.exe -m pytest tests/test_repetition_guard.py tests/test_chat_repetition_guard.py -p no:cacheprovider --basetemp=E:/VoiceAI/temp/pytest-piper-clean -q
```

## Honest scope (NOT "RF3-only")

Verified: `source == "chat"` is emitted by **three** producers —
1. RF3 viewer-chat (`smart_aggregator_ui.py:435, 457`),
2. agenda `HANDLE_CHAT` (`kira_agenda_controller.py:1165`),
3. the **default param** of `enqueue` / `replace_pending` / `enqueue_accumulation`
   (`llm_engine.py:364, 383, 506`).

So this fix applies to chat-reactive generations, currently represented by `source=="chat"`.
It intentionally covers RF3 viewer chat, agenda HANDLE_CHAT, and default-enqueue chat. It does
not affect `direct`, `ptt`, `accumulated`, or `kira-agenda` generation paths. A separate
`event_taxonomy_source_disambiguation` track will disambiguate producers later.

## Decisions locked (owner + researcher + intermediary)

- **D1** penalties = `1.2 / 0.5 / 0.5`.
- **D2** accept the chat-reactive scope (documented, not "RF3-only").
- **D3** fallback-first (no regenerate yet).
- D4 chat-only (not global). D5 detector is threshold-free now; remaining tunables conservative.

## Runtime validation — PARTIAL (2026-06-23)

Owner ran a live session. **Runtime-confirmed against the main observed loop — NOT a full
RF3 readiness sign-off.**

What the run showed:
- The long "abismo sin fin"-style collapse did **not** recur. With `llama3` the repetition
  guard fired only **twice** in ~56 min (`opening_ngram_repeat` 17:47:34, `exact_dup` 17:53:13),
  each recovering with a spoken neutral line. The rest was varied. (Original run: ~40 min of collapse.)
- All **6** guardrail trips (2 repetition + 4 output_guard non-negotiables) produced a TTS
  fallback, **never silence** — confirmed in code: `_guardrail_fallback_line` returns a non-empty
  rotating line for `source=="chat"` (only agenda returns `""`).
- The `gemma4:e4b` segment (~30 min) had **zero** repetition trips.

Why this is PARTIAL, not full validation:
- The run was not clean: mid-run model switches (`qwen3:1.7b`→`llama3`→`gemma4:e4b`), profile
  change (Akira→Comunidad), several `source=direct` operator pokes, two long idle gaps
  (~12 min @17:53→18:05, ~37 min @18:39→19:16), and the chat filter manually lowered to `0.1`.
- The long idle gaps are **"no input" from the over-aggressive `should_call_llm` filter**, NOT
  guardrail silence — every started turn finished with a response; no hung turn in the log.

Honest gap surfaced: the **`source=direct` path is NOT covered** by this guard (chat-only by
design) and showed a faint 2× mini-template ("El rey de la X está en acción", 18:06–18:07).
Logged, not urgent — see tracks.

Still owed for a clean readiness call: one RF3 run with **no direct pokes, single model,
45–60 min**, measuring skeleton-repeats (0 target), fallbacks/30min (≤2), first-audio P95 (<3s).

## Out of scope (deferred)

Model swap, periodic memory wipe — both excluded by design.
