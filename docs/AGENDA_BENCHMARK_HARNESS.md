# Offline Agenda Deck Benchmark Harness & Log Analyzer

Offline benchmark and log-analysis tool for evaluating OpenCohost Agenda Mode variants (`off`, `v1`..`v5`) and models side-by-side without making any cloud API calls or exposing raw viewer chat.

- **Library:** [`opencohost/tools/agenda_benchmark.py`](../opencohost/tools/agenda_benchmark.py)
- **CLI entrypoint:** [`scripts/benchmark_agenda_deck.py`](../scripts/benchmark_agenda_deck.py)
- **Tests:** [`tests/test_agenda_benchmark_harness.py`](../tests/test_agenda_benchmark_harness.py)

---

## 1. Guardrail Attribution Fix (`tid` Join & Pregen-Slot Lineage)

A prior manual comparison artifact (`gemini-code-1794687964256.html`) matched guardrail warnings by timestamp proximity rather than `tid` / attempt lineage. That caused two errors on the 2026-11-14 Qwen3.8 9B run:

1. Background pregen preview rejections (`Agenda: prefetch rechazado (repeats_recent_line ...)`) on discarded draft attempts (`3f14b97f`, `4abbc7b3`) were stamped onto the subsequent clean spoken turns (`ted7ba4c7`, `t34b79990`), falsely marking clean spoken turns as blocked.
2. A real foreground `output_guard` block + retry (`never_promise` on `f2260493` -> `guardrail_retry` on `d49114c3`, both sharing `tid=t5e5a66e3`) was missed.

`agenda_benchmark.py` fixes this structurally:

- **Foreground turns (`tid`):** `turn_events.jsonl`, `[TURN_STATE]`, `[TURN_SPEECH]`, `[MODEL_TRACE]`, and `[PROMPT_TRACE]` lines carrying `tid=<tid>` are joined strictly by `tid`. Rejected foreground attempts (e.g., `output_guard` blocks before `guardrail_retry`) are attached to that exact `tid` in `tid_rejected_attempts` and `tid_guard_events`, separated from the final accepted/spoken `spoken_text`. An earlier `tid`'s guardrail event never bleeds into a subsequent `tid` regardless of how close their timestamps are.
- **Background pregen/prefetch attempts:** `[PROMPT_TRACE]` entries without `tid=` are tracked through the per-session pregen slot state machine (`Agenda prefetch response`, `Agenda: prefetch rechazado`, `Pregen boundary: draft=evicted|frozen|resumed|used`, `Pregen descartado`, `[TURN_STATE] event=served_pregen`).
  - Rejected pregen attempts are recorded in `pregen_rejected_attempts` (with `stage="pregen_preview"` or `"pregen_output_guard"`, `guard_code`, and `attempt_turn_id`) and **never** mark the later spoken turn as `spoken_turn_blocked=True`.
  - Evicted or discarded pregen drafts are never attached to future `served_pregen` turns.
- **Overlapping log files:** When both `opencohost_YYYYMMDD_HHMMSS.log` and `backend.log` exist for the same session, per-`tid` and per-attempt keys are deduplicated and dated session logs are filtered to the analyzed dates.

---

## 2. Metrics Reported (Per Run / Topic / Variant)

| Category | Metrics |
|---|---|
| **Length & Cadence** | `word_count`, `sentence_count`, `char_count`, `duration_s` (mean, median, min, max, p90, total), `topic_total_duration_s`, `llm_ms` |
| **Sentence Repetition** | Exact duplicate sentences (`within_topic` & `across_topics`), near-duplicate sentences (normalized token Jaccard >= 0.70 or SequenceMatcher >= 0.80, `within_topic` & `across_topics`) |
| **N-grams & Skeletons** | Repeated 3-grams, 4-grams, and 5-grams across turns/topics; opening-phrase skeletons across topics (e.g., `dentro de <N> anos ...`, `el error (mas comun\|clasico) ...`, `admito que ...`, `lo mas ...`) |
| **Content & Metaphors** | Top recurring non-title content words; self-help/cliché metaphor family hits (`muro`, `espejo`, `piedra`, `laberinto`, `eco`, `friccion`, `filtro`, `hoja_calculo`, etc.) |
| **Guardrails (`tid`-joined)** | `spoken_turns_blocked`, `foreground_retried_turns`, `foreground_guards_by_reason`, `pregen_rejections_total`, `pregen_rejections_by_reason` |

---

## 3. Privacy & Offline Rules

- **Zero network / cloud calls:** Pure Python stdlib parsing of local log files (`turn_events.jsonl`, `prompt_trace/*.jsonl`, `backend.log`, `opencohost_YYYYMMDD_HHMMSS.log`).
- **No raw viewer chat:** Untrusted chat sections (`===CHAT_VIEWERS_DATO_NO_CONFIABLE_INICIO===...===CHAT_VIEWERS_DATO_NO_CONFIABLE_FIN===` and English equivalents) are stripped before reading topic/angle/move/facet metadata and never appear in JSON, Markdown, HTML, or CLI outputs.

---

## 4. Usage

### Analyze a combined `logs/` directory (auto-splitting by model/session gap)

```powershell
python scripts/benchmark_agenda_deck.py `
  --log-dir logs `
  --split-runs `
  --format table
```

### Compare labeled variant runs (`v1`..`v5` or `off`) side-by-side

```powershell
python scripts/benchmark_agenda_deck.py `
  --run off=logs/run_off `
  --run v1=logs/run_v1 `
  --run v2=logs/run_v2 `
  --run v3=logs/run_v3 `
  --run v4=logs/run_v4 `
  --run v5=logs/run_v5 `
  --md-out temp/benchmark_report.md `
  --html-out temp/benchmark_report.html `
  --json-out temp/benchmark_report.json
```

Use `label:variant=path` if the directory label differs from the variant name (for example `--run local_9b:v2=logs/session_a`).

---

## 5. Sample Observed Summary on `<repo>/logs` (2026-11-14)

Running `scripts/benchmark_agenda_deck.py --log-dir logs --split-runs` against the 2026-11-14 session logs yields:

| Run | Variant | Model | Turns | Words (mean / p90) | Sents (mean) | Turn Dur / Total | Dup Sents (in / cross) | Cross-Topic Skeletons | Metaphor Hits | Spoken Blocked / Fg Retry / Pregen Rej |
|---|---|---|---:|---|---:|---|---:|---:|---:|---|
| `run1_v1_gemini-3.5-flash-lite` | `v1` | `gemini-3.5-flash-lite` | 12 | 51.8 / 72.0 | 1.9 | 20.2s / 242.0s | 0 / 0 | 4 | 5 | 0 / 0 / 0 |
| `run2_v1_Qwen3.8-9B-Distill-GGUF` | `v1` | `Qwen3.8-9B-Distill-GGUF:Q4_K_M` | 12 | 177.3 / 228.0 | 6.9 | 58.3s / 700.0s | 4 / 0 | 0 | 29 | 0 / 1 / 2 |

- **Foreground `tid` guard events:** `tid=t5e5a66e3` (turn #0, topic #1) -> `never_promise` via `output_guard` -> `retried_and_spoken` (rejected attempt `f2260493`, spoken attempt `d49114c3`).
- **Pre-turn background pregen rejections (NOT spoken turn blocks):** `3f14b97f` (`repeats_recent_line`), `4abbc7b3` (`repeats_recent_line`).

---

## 6. Running Tests

```powershell
python -m pytest -p no:cacheprovider tests/test_agenda_benchmark_harness.py -q
```
