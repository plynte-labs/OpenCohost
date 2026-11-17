"""Tests for the Offline Agenda Deck Benchmark Harness & Log Analyzer.

Strict TDD suite verifying:
1. Guardrail attribution strictly by ``tid`` and attempt lineage (NEVER by timestamp proximity).
2. Accurate per-run / per-topic / per-variant metrics (words, sentences, chars, duration,
   exact & near-duplicate sentences, n-grams, opening skeletons, metaphor loops).
3. Privacy guarantee: raw viewer chat is never exposed in any parsed structure or output.
4. Multi-run comparison outputs (CLI table, Markdown, JSON, self-contained HTML).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from opencohost.tools.agenda_benchmark import (
    analyze_log_dir,
    compare_runs,
    detect_variant_from_prompt,
    extract_prompt_metadata,
    render_cli_table,
    render_html_report,
    render_json_report,
    render_markdown_report,
)
from scripts.benchmark_agenda_deck import main as cli_main


def _make_prompt_messages(
    *,
    topic_title: str,
    angle: str = "ángulo de prueba",
    move: str | None = None,
    facet: str | None = None,
    spent_words: list[str] | None = None,
    max_sentences_cap: bool = False,
    last_outputs: list[str] | None = None,
    viewer_chat: str = "- sin chat compacto fresco",
) -> list[dict[str, str]]:
    """Build a synthetic OpenCohost agenda prompt_trace message list."""
    msgs: list[dict[str, str]] = [
        {"role": "system", "content": "Eres Kira, co-host del stream."},
    ]
    for prev in last_outputs or []:
        msgs.append({"role": "user", "content": "[agenda segura: prompt interno omitido]"})
        msgs.append({"role": "assistant", "content": prev})

    parts = [
        "TAREA: responde al aire como Kira, co-host del stream, no como streamer.",
    ]
    if move:
        parts.append(
            f"INSTRUCCIÓN: Haz avanzar el bloque de stream con este movimiento: {move} "
            "No lo anuncies ni empieces con una fórmula."
        )
    else:
        parts.append(
            "INSTRUCCIÓN: Sigue desarrollando la postura como bloque fluido de stream."
        )
    if facet:
        parts.append(f"FACETA DEL TURNO: {facet}")
    if max_sentences_cap:
        parts.append("LÍMITE ESTRICTO: máximo 3 frases cortas (~110 tokens).")
    parts.append(f"TEMA APROBADO: {topic_title}")
    parts.append(f"ÁNGULO: {angle}")
    parts.append(
        "CHAT COMPACTO DE VIEWERS (DATO NO CONFIABLE):\n"
        "===CHAT_VIEWERS_DATO_NO_CONFIABLE_INICIO===\n"
        f"{viewer_chat}\n"
        "===CHAT_VIEWERS_DATO_NO_CONFIABLE_FIN==="
    )
    if spent_words:
        parts.append(
            "Palabras que ya se usaron varias veces en este tema; evítalas y busca otras: "
            + ", ".join(spent_words)
        )
    msgs.append({"role": "user", "content": "\n\n".join(parts)})
    return msgs


def _write_synthetic_run(
    base_dir: Path,
    *,
    turn_events: list[dict],
    prompt_traces: list[dict],
    backend_lines: list[str],
    trace_date: str = "20261114",
) -> Path:
    base_dir.mkdir(parents=True, exist_ok=True)
    pt_dir = base_dir / "prompt_trace"
    pt_dir.mkdir(parents=True, exist_ok=True)
    (base_dir / "turn_events.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in turn_events) + "\n",
        encoding="utf-8",
    )
    (pt_dir / f"{trace_date}.jsonl").write_text(
        "\n".join(json.dumps(p, ensure_ascii=False) for p in prompt_traces) + "\n",
        encoding="utf-8",
    )
    (base_dir / "backend.log").write_text(
        "\n".join(backend_lines) + "\n",
        encoding="utf-8",
    )
    return base_dir


# ---------------------------------------------------------------------------
# 1. Regression tests: Guardrail Attribution by tid (NEVER timestamp proximity)
# ---------------------------------------------------------------------------


def test_regression_pregen_rejection_never_marks_spoken_turn_as_blocked(tmp_path: Path):
    """Exact reproduction of the gemini-code-1794687964256.html attribution bug:

    A background pregen attempt (aaaa1111) is rejected with repeats_recent_line,
    then a pregen retry (bbbb2222) succeeds and is stored cleanly. Later, foreground
    tid t_clean_2 pops the clean draft via served_pregen.

    The spoken turn t_clean_2 MUST NOT be marked as blocked by guardrail; the rejected
    pregen attempt aaaa1111 must be separated into rejected_attempts / pregen_rejected_attempts.
    """
    t1_reply = "El internet se muere lento cuando todo pasa por un filtro automático."
    rejected_reply = "No es que no haya más voces, es que todas son copias exactas."
    clean_retry_reply = (
        "Lo que realmente duele es que ya nadie se equivoca con gracia al escribir. "
        "Prefiero una coma mal puesta a un texto perfecto sin alma."
    )

    turn_events = [
        {
            "v": 1,
            "ts": 1794687234000,
            "tid": "t_open_01",
            "source": "kira-agenda",
            "model": "qwen3.8-9b",
            "outcome": "completed",
            "latency_outcome": "ok",
            "phases": ["assembling", "generating", "completed"],
            "events": {"prompt_built": 1, "complete": 1},
            "guards": [],
            "attempts": 1,
            "timings": {"engine_ms": 2000, "llm_ms": 1900},
        },
        {
            "v": 1,
            "ts": 1794687264000,
            "tid": "t_clean_2",
            "source": "kira-agenda",
            "model": "qwen3.8-9b",
            "outcome": "completed",
            "phases": ["assembling", "completed"],
            "events": {"served_pregen": 1},
            "guards": [],
            "attempts": 0,
            "timings": {},
        },
    ]

    prompt_traces = [
        {
            "turn": "00001111",
            "ts": "2026-11-14T14:14:06.000",
            "source": "kira-agenda",
            "model": "qwen3.8-9b",
            "messages": _make_prompt_messages(topic_title="Teoría del internet muerto"),
            "reply": t1_reply,
        },
        {
            "turn": "aaaa1111",
            "ts": "2026-11-14T14:14:20.000",
            "source": "kira-agenda",
            "model": "qwen3.8-9b",
            "messages": _make_prompt_messages(
                topic_title="Teoría del internet muerto",
                move="Zoom en un detalle pequeño",
                last_outputs=[t1_reply],
            ),
            "reply": rejected_reply,
        },
        {
            "turn": "bbbb2222",
            "ts": "2026-11-14T14:14:30.000",
            "source": "kira-agenda",
            "model": "qwen3.8-9b",
            "messages": _make_prompt_messages(
                topic_title="Teoría del internet muerto",
                move="Zoom en un detalle pequeño",
                last_outputs=[t1_reply],
            ),
            "reply": clean_retry_reply,
        },
    ]

    backend_lines = [
        "[14:14:04] [INFO] [OpenCohost] [TURN_STATE] tid=t_open_01 source=kira-agenda start->assembling event=begin",
        "[14:14:04] [INFO] [OpenCohost] [TURN_STATE] tid=t_open_01 source=kira-agenda assembling->generating event=prompt_built ms=10",
        "[14:14:06] [INFO] [OpenCohost] [PROMPT_TRACE] turn=00001111 source=kira-agenda model=qwen3.8-9b tid=t_open_01",
        "[14:14:06] [INFO] [OpenCohost] [TURN_STATE] tid=t_open_01 source=kira-agenda generating->completed event=complete ms=2000",
        "[14:14:06] [INFO] [OpenCohost] [TURN_SPEECH] tid=t_open_01 job=1 state=started reason=activated",
        # Background pregen attempt 1 -> rejected by preview guardrail
        "[14:14:20] [INFO] [OpenCohost] [PROMPT_TRACE] turn=aaaa1111 source=kira-agenda model=qwen3.8-9b",
        "[14:14:20] [INFO] [OpenCohost] Agenda prefetch response (14.00s): No es que no haya más voces",
        '[14:14:20] [WARNING] [OpenCohost] Motor: Agenda: prefetch rechazado (repeats_recent_line ("no es que no haya más voces...")).',
        "[14:14:20] [DEBUG] [OpenCohost] Pregen retry attempt rejected: source=kira-agenda",
        # Background pregen attempt 2 -> clean retry accepted into pregen slot
        "[14:14:30] [INFO] [OpenCohost] [PROMPT_TRACE] turn=bbbb2222 source=kira-agenda model=qwen3.8-9b",
        "[14:14:30] [INFO] [OpenCohost] Agenda prefetch response (10.00s): Lo que realmente duele",
        # t_open_01 finishes speaking, t_clean_2 consumes clean retry bbbb2222
        "[14:14:34] [INFO] [OpenCohost] [TURN_SPEECH] tid=t_open_01 job=1 state=finished reason=complete",
        "[14:14:34] [INFO] [OpenCohost] [TURN_STATE] tid=t_clean_2 source=kira-agenda start->assembling event=begin",
        "[14:14:34] [INFO] [OpenCohost] Motor: Agenda: usando respuesta pregenerada.",
        "[14:14:34] [INFO] [OpenCohost] Motor: Pregen boundary: draft=used source=kira-agenda gap_ms=10 gen_ms=10000 speech_ms=28000",
        "[14:14:34] [INFO] [OpenCohost] [TURN_STATE] tid=t_clean_2 source=kira-agenda assembling->completed event=served_pregen",
        "[14:14:34] [INFO] [OpenCohost] [TURN_SPEECH] tid=t_clean_2 job=2 state=started reason=activated",
        "[14:14:54] [INFO] [OpenCohost] [TURN_SPEECH] tid=t_clean_2 job=2 state=finished reason=complete",
    ]

    log_dir = _write_synthetic_run(
        tmp_path / "run_pregen",
        turn_events=turn_events,
        prompt_traces=prompt_traces,
        backend_lines=backend_lines,
    )
    runs = analyze_log_dir(log_dir, variant="v1")
    assert len(runs) == 1
    run = runs[0]
    assert len(run.spoken_turns) == 2

    t1, t2 = run.spoken_turns
    assert t1.tid == "t_open_01"
    assert t1.tid_guard_events == []
    assert t1.pregen_rejected_attempts == []

    assert t2.tid == "t_clean_2"
    assert t2.spoken_text == clean_retry_reply
    assert t2.accepted_attempt_turn_id == "bbbb2222"
    # Critical invariant: t_clean_2 was NOT blocked!
    assert t2.spoken_turn_blocked is False
    assert t2.tid_guard_events == []
    assert t2.tid_rejected_attempts == []
    # The earlier pregen rejection is cleanly tracked separately as a pre-turn pregen retry
    assert len(t2.pregen_rejected_attempts) == 1
    assert t2.pregen_rejected_attempts[0].attempt_turn_id == "aaaa1111"
    assert t2.pregen_rejected_attempts[0].guard_code == "repeats_recent_line"
    assert t2.pregen_rejected_attempts[0].stage == "pregen_preview"

    # Guard summary distinguishes spoken-turn blocks (0) from pregen preview retries (1)
    assert run.guard_summary.spoken_turns_blocked == 0
    assert run.guard_summary.pregen_rejections_by_reason == {"repeats_recent_line": 1}


def test_regression_earlier_tid_guardrail_never_bleeds_into_subsequent_good_turn(tmp_path: Path):
    """Regression test: an earlier turn's foreground guardrail block + retry (50ms earlier!)
    must NEVER be attributed to the subsequent good turn, proving join is strictly by tid.
    """
    blocked_text = "Te prometo que mañana todo será distinto en tu vida."
    retried_good_text = "A veces creemos que elegimos todo, pero la rutina también pesa."
    next_good_text = "La libertad se nota en los detalles pequeños del día a día."

    turn_events = [
        {
            "v": 1,
            "ts": 1794686876973,
            "tid": "t_blocked_1",
            "source": "kira-agenda",
            "model": "qwen3.8-9b",
            "outcome": "completed",
            "latency_outcome": "retried",
            "phases": ["assembling", "generating", "guard_blocked", "generating", "completed"],
            "events": {"prompt_built": 1, "guard_block": 1, "guard_retry": 1, "complete": 1},
            "guards": ["guard_block", "guard_retry"],
            "attempts": 2,
            "timings": {"engine_ms": 5000, "llm_ms": 4800},
        },
        {
            "v": 1,
            "ts": 1794686877023,  # ONLY 50ms after t_blocked_1!
            "tid": "t_good_2",
            "source": "kira-agenda",
            "model": "qwen3.8-9b",
            "outcome": "completed",
            "latency_outcome": "ok",
            "phases": ["assembling", "generating", "completed"],
            "events": {"prompt_built": 1, "complete": 1},
            "guards": [],
            "attempts": 1,
            "timings": {"engine_ms": 2000, "llm_ms": 1900},
        },
    ]

    prompt_traces = [
        {
            "turn": "1111aaaa",
            "ts": "2026-11-14T14:08:12.100",
            "source": "kira-agenda",
            "model": "qwen3.8-9b",
            "messages": _make_prompt_messages(topic_title="Si el universo nos controla"),
            "reply": blocked_text,
        },
        {
            "turn": "1111bbbb",
            "ts": "2026-11-14T14:08:12.400",
            "source": "guardrail_retry",
            "model": "qwen3.8-9b",
            "messages": _make_prompt_messages(topic_title="Si el universo nos controla"),
            "reply": retried_good_text,
        },
        {
            "turn": "2222cccc",
            "ts": "2026-11-14T14:08:12.450",  # 50ms later!
            "source": "kira-agenda",
            "model": "qwen3.8-9b",
            "messages": _make_prompt_messages(
                topic_title="Si el universo nos controla",
                move="Zoom en un detalle pequeño",
                last_outputs=[retried_good_text],
            ),
            "reply": next_good_text,
        },
    ]

    backend_lines = [
        "[14:08:10] [INFO] [OpenCohost] [TURN_STATE] tid=t_blocked_1 source=kira-agenda start->assembling event=begin",
        "[14:08:10] [INFO] [OpenCohost] [TURN_STATE] tid=t_blocked_1 source=kira-agenda assembling->generating event=prompt_built ms=10",
        "[14:08:12] [INFO] [OpenCohost] [PROMPT_TRACE] turn=1111aaaa source=kira-agenda model=qwen3.8-9b tid=t_blocked_1",
        "[14:08:12] [WARNING] [OpenCohost] Motor: Salida bloqueada por guardrail: Non-negotiable violation [never_promise]: response contains a promise or guarantee",
        "[14:08:12] [INFO] [OpenCohost] [TURN_STATE] tid=t_blocked_1 source=kira-agenda generating->guard_blocked event=guard_block",
        "[14:08:12] [INFO] [OpenCohost] [TURN_STATE] tid=t_blocked_1 source=kira-agenda guard_blocked->generating event=guard_retry",
        "[14:08:12] [INFO] [OpenCohost] [PROMPT_TRACE] turn=1111bbbb source=guardrail_retry model=qwen3.8-9b tid=t_blocked_1",
        "[14:08:12] [INFO] [OpenCohost] Motor: Guardrail retry: respuesta corregida aceptada tras un intento adicional.",
        "[14:08:12] [INFO] [OpenCohost] [TURN_STATE] tid=t_blocked_1 source=kira-agenda generating->completed event=complete ms=5000",
        "[14:08:12] [INFO] [OpenCohost] [TURN_SPEECH] tid=t_blocked_1 job=1 state=started reason=activated",
        "[14:08:12] [INFO] [OpenCohost] [TURN_SPEECH] tid=t_blocked_1 job=1 state=finished reason=complete",
        # Next turn starts in the exact same second [14:08:12]
        "[14:08:12] [INFO] [OpenCohost] [TURN_STATE] tid=t_good_2 source=kira-agenda start->assembling event=begin",
        "[14:08:12] [INFO] [OpenCohost] [TURN_STATE] tid=t_good_2 source=kira-agenda assembling->generating event=prompt_built ms=5",
        "[14:08:12] [INFO] [OpenCohost] [PROMPT_TRACE] turn=2222cccc source=kira-agenda model=qwen3.8-9b tid=t_good_2",
        "[14:08:12] [INFO] [OpenCohost] [TURN_STATE] tid=t_good_2 source=kira-agenda generating->completed event=complete ms=2000",
        "[14:08:12] [INFO] [OpenCohost] [TURN_SPEECH] tid=t_good_2 job=2 state=started reason=activated",
        "[14:08:25] [INFO] [OpenCohost] [TURN_SPEECH] tid=t_good_2 job=2 state=finished reason=complete",
    ]

    log_dir = _write_synthetic_run(
        tmp_path / "run_tid_join",
        turn_events=turn_events,
        prompt_traces=prompt_traces,
        backend_lines=backend_lines,
    )
    runs = analyze_log_dir(log_dir, variant="v1")
    run = runs[0]
    assert len(run.spoken_turns) == 2

    first, second = run.spoken_turns
    assert first.tid == "t_blocked_1"
    assert first.was_retried_foreground is True
    assert first.spoken_turn_blocked is False  # retry succeeded and was spoken
    assert first.spoken_text == retried_good_text
    assert first.accepted_attempt_turn_id == "1111bbbb"
    assert len(first.tid_rejected_attempts) == 1
    assert first.tid_rejected_attempts[0].attempt_turn_id == "1111aaaa"
    assert first.tid_rejected_attempts[0].guard_code == "never_promise"
    assert any(g.reason == "never_promise" for g in first.tid_guard_events)

    # Critical: t_good_2 has ZERO guard events despite occurring in the same second!
    assert second.tid == "t_good_2"
    assert second.spoken_text == next_good_text
    assert second.accepted_attempt_turn_id == "2222cccc"
    assert second.was_retried_foreground is False
    assert second.spoken_turn_blocked is False
    assert second.tid_guard_events == []
    assert second.tid_rejected_attempts == []
    assert second.pregen_rejected_attempts == []


def test_evicted_pregen_rejection_does_not_attach_to_later_turn(tmp_path: Path):
    """If a pregen draft was rejected and then evicted before any served_pregen,
    the next served_pregen turn must NOT inherit that evicted rejection."""
    turn_events = [
        {
            "v": 1,
            "ts": 1794687300000,
            "tid": "t_later_1",
            "source": "kira-agenda",
            "model": "qwen3.8-9b",
            "outcome": "completed",
            "events": {"served_pregen": 1},
            "guards": [],
            "attempts": 0,
        },
    ]
    prompt_traces = [
        {
            "turn": "eeee1111",
            "ts": "2026-11-14T14:20:00.000",
            "source": "kira-agenda",
            "model": "qwen3.8-9b",
            "messages": _make_prompt_messages(topic_title="Tema A"),
            "reply": "Intento viejo rechazado.",
        },
        {
            "turn": "ffff2222",
            "ts": "2026-11-14T14:20:20.000",
            "source": "kira-agenda",
            "model": "qwen3.8-9b",
            "messages": _make_prompt_messages(topic_title="Tema A"),
            "reply": "Borrador limpio nuevo tras PTT.",
        },
    ]
    backend_lines = [
        "[14:20:00] [INFO] [OpenCohost] [PROMPT_TRACE] turn=eeee1111 source=kira-agenda model=qwen3.8-9b",
        '[14:20:00] [WARNING] [OpenCohost] Motor: Agenda: prefetch rechazado (reused_opening ("y eso...")).',
        "[14:20:05] [INFO] [OpenCohost] Motor: Pregen boundary: draft=evicted source=kira-agenda gap_ms=-1 gen_ms=-1 speech_ms=10000",
        "[14:20:20] [INFO] [OpenCohost] [PROMPT_TRACE] turn=ffff2222 source=kira-agenda model=qwen3.8-9b",
        "[14:20:20] [INFO] [OpenCohost] Agenda prefetch response (5.00s): Borrador limpio nuevo tras PTT.",
        "[14:20:30] [INFO] [OpenCohost] [TURN_STATE] tid=t_later_1 source=kira-agenda start->assembling event=begin",
        "[14:20:30] [INFO] [OpenCohost] Motor: Agenda: usando respuesta pregenerada.",
        "[14:20:30] [INFO] [OpenCohost] [TURN_STATE] tid=t_later_1 source=kira-agenda assembling->completed event=served_pregen",
        "[14:20:30] [INFO] [OpenCohost] [TURN_SPEECH] tid=t_later_1 job=1 state=started reason=activated",
        "[14:20:45] [INFO] [OpenCohost] [TURN_SPEECH] tid=t_later_1 job=1 state=finished reason=complete",
    ]
    log_dir = _write_synthetic_run(
        tmp_path / "run_evict",
        turn_events=turn_events,
        prompt_traces=prompt_traces,
        backend_lines=backend_lines,
    )
    run = analyze_log_dir(log_dir, variant="v1")[0]
    t = run.spoken_turns[0]
    assert t.tid == "t_later_1"
    assert t.pregen_rejected_attempts == []
    assert run.guard_summary.pregen_rejections_by_reason == {"reused_opening": 1}


# ---------------------------------------------------------------------------
# 2. Metrics: Words, Sentences, Chars, Duration, Repetition, Skeletons, Metaphors
# ---------------------------------------------------------------------------


def test_metrics_repetition_ngrams_skeletons_and_metaphors(tmp_path: Path):
    """Verify calculation of word/sentence/char/duration stats, exact/near-dup sentences,
    3/4/5-grams, cross-topic opening skeletons, and self-help metaphor loops."""
    # Topic 1 (3 turns) + Topic 2 (2 turns)
    t1_turn0 = (
        "Dentro de diez años probablemente seguiremos buscando respuestas en el espejo roto. "
        "Cada muro que levantas en tu cabeza es una piedra en el camino."
    )
    t1_turn1 = (
        "El error más común es creer que esos muros son reales cuando son solo un espejo. "
        "Cada muro que levantas en tu cabeza es una piedra en el camino."
    )
    t1_turn2 = (
        "Al final, ese espejo digital nos muestra siempre la misma piedra en la ruta."
    )
    t2_turn0 = (
        "Dentro de diez años probablemente veremos algoritmos hablando entre ellos sin freno. "
        "La creatividad nace de la fricción inesperada."
    )
    t2_turn1 = (
        "El error más común es creer que el algoritmo puede crear sin fricción alguna. "
        "La creatividad surge de la fricción inesperada."
    )

    replies = [t1_turn0, t1_turn1, t1_turn2, t2_turn0, t2_turn1]
    topics = [
        ("Si el universo nos controla", None),
        ("Si el universo nos controla", "Imagina cómo se vería esto dentro de diez años"),
        ("Si el universo nos controla", "Cuenta qué error común comete la gente"),
        ("Teoría del internet muerto", None),
        ("Teoría del internet muerto", "Cuenta qué error común comete la gente"),
    ]
    turn_events = []
    prompt_traces = []
    backend_lines = []
    base_ts = 1794686000000
    for idx, (reply, (topic_title, move)) in enumerate(zip(replies, topics)):
        tid = f"t0000000{idx+1}"
        turn_hex = f"a000000{idx+1}"
        start_sec = 10 + idx * 20
        end_sec = start_sec + 15  # 15s speech per turn
        turn_events.append(
            {
                "v": 1,
                "ts": base_ts + idx * 20000,
                "tid": tid,
                "source": "kira-agenda",
                "model": "qwen3.8-9b",
                "outcome": "completed",
                "latency_outcome": "ok",
                "events": {"prompt_built": 1, "complete": 1},
                "guards": [],
                "attempts": 1,
                "timings": {"engine_ms": 2000, "llm_ms": 1800},
            }
        )
        prompt_traces.append(
            {
                "turn": turn_hex,
                "ts": f"2026-11-14T14:00:{start_sec:02d}.000",
                "source": "kira-agenda",
                "model": "qwen3.8-9b",
                "messages": _make_prompt_messages(topic_title=topic_title, move=move),
                "reply": reply,
            }
        )
        backend_lines.extend(
            [
                f"[14:00:{start_sec:02d}] [INFO] [OpenCohost] [TURN_STATE] tid={tid} source=kira-agenda start->assembling event=begin",
                f"[14:00:{start_sec:02d}] [INFO] [OpenCohost] [PROMPT_TRACE] turn={turn_hex} source=kira-agenda model=qwen3.8-9b tid={tid}",
                f"[14:00:{start_sec:02d}] [INFO] [OpenCohost] [TURN_STATE] tid={tid} source=kira-agenda generating->completed event=complete ms=2000",
                f"[14:00:{start_sec:02d}] [INFO] [OpenCohost] [TURN_SPEECH] tid={tid} job={idx+1} state=started reason=activated",
                f"[14:00:{end_sec:02d}] [INFO] [OpenCohost] [TURN_SPEECH] tid={tid} job={idx+1} state=finished reason=complete",
            ]
        )

    log_dir = _write_synthetic_run(
        tmp_path / "run_metrics",
        turn_events=turn_events,
        prompt_traces=prompt_traces,
        backend_lines=backend_lines,
    )
    run = analyze_log_dir(log_dir, variant="v1")[0]

    assert run.variant == "v1"
    assert len(run.topics) == 2
    assert run.word_stats.mean > 0
    assert run.word_stats.min <= run.word_stats.median <= run.word_stats.p90 <= run.word_stats.max
    assert run.sentence_stats.mean >= 1.0
    assert run.char_stats.mean > 50
    assert run.duration_stats.mean == pytest.approx(15.0, abs=0.5)
    assert run.topics[0].total_duration_s >= 45.0

    # Exact duplicate sentence within topic 1:
    # "Cada muro que levantas en tu cabeza es una piedra en el camino."
    assert run.repetition.exact_duplicate_sentences_within_topic >= 1
    # Near duplicate sentence within topic 2:
    # "La creatividad nace de la fricción inesperada." vs "La creatividad surge de la fricción inesperada."
    assert run.repetition.near_duplicate_sentences_within_topic >= 1

    # Repeated 3/4/5-grams detected
    assert run.repetition.repeated_ngrams[3]
    assert run.repetition.repeated_ngrams[4]
    assert run.repetition.repeated_ngrams[5]

    # Opening-phrase skeletons across topics: "dentro de diez años probablemente" & "el error más común"
    skeleton_phrases = {s.skeleton for s in run.repetition.cross_topic_opening_skeletons}
    assert any("dentro de" in s for s in skeleton_phrases)
    assert any("error" in s for s in skeleton_phrases)

    # Metaphor loops detected (muros/muro, espejo, piedra)
    metaphor_terms = {m.family for m in run.metaphor_loops}
    assert "muro" in metaphor_terms or "espejo" in metaphor_terms
    assert "piedra" in metaphor_terms


# ---------------------------------------------------------------------------
# 3. Privacy Invariant: Never Expose Raw Viewer Chat
# ---------------------------------------------------------------------------


def test_raw_viewer_chat_never_exposed_in_any_output(tmp_path: Path):
    """Raw viewer chat strings inside prompt_trace must NEVER appear in JSON, Markdown, HTML, or CLI."""
    sentinel = "RAW_VIEWER_CHAT_SECRET_USER123_DO_NOT_LEAK"
    turn_events = [
        {
            "v": 1,
            "ts": 1794686000000,
            "tid": "t_priv_01",
            "source": "kira-agenda",
            "model": "qwen3.8-9b",
            "outcome": "completed",
            "events": {"prompt_built": 1, "complete": 1},
            "guards": [],
            "attempts": 1,
        }
    ]
    prompt_traces = [
        {
            "turn": "p1111111",
            "ts": "2026-11-14T14:00:10.000",
            "source": "kira-agenda",
            "model": "qwen3.8-9b",
            "messages": _make_prompt_messages(
                topic_title="Privacidad en stream",
                viewer_chat=f"- viewer_juan: {sentinel}",
            ),
            "reply": "Hablemos de cómo mantener una charla fluida sin perder el ritmo.",
        }
    ]
    backend_lines = [
        "[14:00:10] [INFO] [OpenCohost] [TURN_STATE] tid=t_priv_01 source=kira-agenda start->assembling event=begin",
        "[14:00:10] [INFO] [OpenCohost] [PROMPT_TRACE] turn=p1111111 source=kira-agenda model=qwen3.8-9b tid=t_priv_01",
        "[14:00:10] [INFO] [OpenCohost] [TURN_STATE] tid=t_priv_01 source=kira-agenda generating->completed event=complete ms=1000",
    ]
    log_dir = _write_synthetic_run(
        tmp_path / "run_priv",
        turn_events=turn_events,
        prompt_traces=prompt_traces,
        backend_lines=backend_lines,
    )
    runs = analyze_log_dir(log_dir, variant="off")
    report = compare_runs(runs)
    for rendered in (
        render_json_report(report),
        render_markdown_report(report),
        render_html_report(report),
        render_cli_table(report),
    ):
        assert sentinel not in rendered


# ---------------------------------------------------------------------------
# 4. Variant Detection & Multi-Run Comparison + CLI Entrypoint
# ---------------------------------------------------------------------------


def test_variant_detection_and_side_by_side_comparison_cli(tmp_path: Path):
    """Compare v1 vs v2 vs v5 runs via CLI and verify JSON, Markdown, and HTML outputs."""
    specs = [
        ("off", False, False, None, "Un comentario largo repetitivo sobre muros y espejos en el camino diario."),
        ("v1", True, False, None, "Un comentario con mazo base sobre la vida cotidiana."),
        ("v2", True, True, None, "Tres frases breves y directas. Sin rodeos. Cierre limpio."),
        ("v5", True, True, "mecanismo -> tensión humana", "Primero el mecanismo invisible. Luego la tensión real."),
    ]
    run_args: list[str] = []
    for label, has_move, has_cap, facet, reply in specs:
        d = tmp_path / f"run_{label}"
        _write_synthetic_run(
            d,
            turn_events=[
                {
                    "v": 1,
                    "ts": 1794686000000,
                    "tid": f"t_{label}_01",
                    "source": "kira-agenda",
                    "model": "qwen3.8-9b",
                    "outcome": "completed",
                    "events": {"prompt_built": 1, "complete": 1},
                    "guards": [],
                    "attempts": 1,
                }
            ],
            prompt_traces=[
                {
                    "turn": f"{label[:2]}000001",
                    "ts": "2026-11-14T14:00:10.000",
                    "source": "kira-agenda",
                    "model": "qwen3.8-9b",
                    "messages": _make_prompt_messages(
                        topic_title="Tema único",
                        move="Caso concreto" if has_move else None,
                        max_sentences_cap=has_cap,
                        facet=facet,
                    ),
                    "reply": reply,
                }
            ],
            backend_lines=[
                f"[14:00:10] [INFO] [OpenCohost] [TURN_STATE] tid=t_{label}_01 source=kira-agenda start->assembling event=begin",
                f"[14:00:10] [INFO] [OpenCohost] [PROMPT_TRACE] turn={label[:2]}000001 source=kira-agenda model=qwen3.8-9b tid=t_{label}_01",
                f"[14:00:10] [INFO] [OpenCohost] [TURN_STATE] tid=t_{label}_01 source=kira-agenda generating->completed event=complete ms=1200",
                f"[14:00:10] [INFO] [OpenCohost] [TURN_SPEECH] tid=t_{label}_01 job=1 state=started reason=activated",
                f"[14:00:22] [INFO] [OpenCohost] [TURN_SPEECH] tid=t_{label}_01 job=1 state=finished reason=complete",
            ],
        )
        run_args.extend(["--run", f"{label}={d}"])

    json_out = tmp_path / "out.json"
    md_out = tmp_path / "out.md"
    html_out = tmp_path / "out.html"

    rc = cli_main([
        *run_args,
        "--json-out", str(json_out),
        "--md-out", str(md_out),
        "--html-out", str(html_out),
    ])
    assert rc == 0

    data = json.loads(json_out.read_text(encoding="utf-8"))
    assert [r["label"] for r in data["runs"]] == ["off", "v1", "v2", "v5"]
    md = md_out.read_text(encoding="utf-8")
    assert "v1" in md and "v2" in md and "v5" in md
    html_doc = html_out.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in html_doc
    assert "t_v2_01" in html_doc
    assert "t_v5_01" in html_doc


def test_overlapping_session_logs_dedup_and_cloud_model_trace_override(tmp_path: Path):
    """Overlapping opencohost_YYYYMMDD_HHMMSS.log + backend.log must not double-count
    guard events, unrelated dates must be ignored, and [MODEL_TRACE] cloud_model must
    resolve served_pregen turns when turn_events omits model."""
    turn_events = [
        {
            "v": 1,
            "ts": 1794686271796,
            "tid": "t_cloud_1",
            "source": "kira-agenda",
            "model": "gemini-3.5-flash-lite",
            "outcome": "completed",
            "events": {"prompt_built": 1, "complete": 1},
            "guards": ["guard_block", "guard_retry"],
            "attempts": 2,
        },
        {
            "v": 1,
            "ts": 1794686289163,
            "tid": "t_cloud_2",
            "source": "kira-agenda",
            "outcome": "completed",
            "events": {"served_pregen": 1},
            "guards": [],
            "attempts": 0,
        },
    ]
    prompt_traces = [
        {
            "turn": "c1111111",
            "ts": "2026-11-14T13:57:51.000",
            "source": "kira-agenda",
            "model": "local-qwen-9b",
            "messages": _make_prompt_messages(topic_title="Tema Cloud"),
            "reply": "Te prometo que va a salir bien.",
        },
        {
            "turn": "c2222222",
            "ts": "2026-11-14T13:57:52.000",
            "source": "guardrail_retry",
            "model": "local-qwen-9b",
            "messages": _make_prompt_messages(topic_title="Tema Cloud"),
            "reply": "Vamos a ver cómo evoluciona la conversación.",
        },
        {
            "turn": "c3333333",
            "ts": "2026-11-14T13:57:54.000",
            "source": "kira-agenda",
            "model": "local-qwen-9b",
            "messages": _make_prompt_messages(topic_title="Tema Cloud", move="Caso concreto"),
            "reply": "En un stream real esto cambia rápido.",
        },
    ]
    session_lines = [
        "[13:57:51] [INFO] [OpenCohost] [TURN_STATE] tid=t_cloud_1 source=kira-agenda start->assembling event=begin",
        "[13:57:52] [INFO] [OpenCohost] [PROMPT_TRACE] turn=c1111111 source=kira-agenda model=local-qwen-9b tid=t_cloud_1",
        "[13:57:52] [INFO] [OpenCohost] Motor: [MODEL_TRACE] desired=local-qwen-9b provider=gemini transport=cloud cloud_model=gemini-3.5-flash-lite",
        "[13:57:52] [WARNING] [OpenCohost] Motor: Salida bloqueada por guardrail: Non-negotiable violation [never_promise]: response contains a promise",
        "[13:57:52] [INFO] [OpenCohost] [TURN_STATE] tid=t_cloud_1 source=kira-agenda generating->guard_blocked event=guard_block",
        "[13:57:52] [INFO] [OpenCohost] [TURN_STATE] tid=t_cloud_1 source=kira-agenda guard_blocked->generating event=guard_retry",
        "[13:57:52] [INFO] [OpenCohost] [PROMPT_TRACE] turn=c2222222 source=guardrail_retry model=local-qwen-9b tid=t_cloud_1",
        "[13:57:52] [INFO] [OpenCohost] [TURN_STATE] tid=t_cloud_1 source=kira-agenda generating->completed event=complete ms=1200",
        "[13:57:54] [INFO] [OpenCohost] [PROMPT_TRACE] turn=c3333333 source=kira-agenda model=local-qwen-9b",
        "[13:57:54] [INFO] [OpenCohost] Motor: [MODEL_TRACE] desired=local-qwen-9b provider=gemini transport=cloud cloud_model=gemini-3.5-flash-lite",
        "[13:57:54] [INFO] [OpenCohost] Agenda prefetch response (1.20s): En un stream real",
        "[13:58:09] [INFO] [OpenCohost] [TURN_STATE] tid=t_cloud_2 source=kira-agenda start->assembling event=begin",
        "[13:58:09] [INFO] [OpenCohost] [TURN_STATE] tid=t_cloud_2 source=kira-agenda assembling->completed event=served_pregen",
    ]
    log_dir = _write_synthetic_run(
        tmp_path / "run_overlap",
        turn_events=turn_events,
        prompt_traces=prompt_traces,
        backend_lines=session_lines,
        trace_date="20261114",
    )
    # Overlapping rotated file for the same date
    (log_dir / "opencohost_20261114_135142.log").write_text(
        "\n".join(session_lines) + "\n", encoding="utf-8"
    )
    # Unrelated older session log that must be ignored
    (log_dir / "opencohost_20261015_120000.log").write_text(
        "[12:00:00] [INFO] [OpenCohost] [PROMPT_TRACE] turn=deadbeef source=kira-agenda model=old\n"
        "[12:00:00] [WARNING] [OpenCohost] Motor: Agenda: prefetch rechazado (reused_opening).\n",
        encoding="utf-8",
    )

    run = analyze_log_dir(log_dir)[0]
    assert run.model == "gemini-3.5-flash-lite"
    assert run.spoken_turns[1].model == "gemini-3.5-flash-lite"
    assert len(run.guard_summary.foreground_guard_events) == 1
    assert run.guard_summary.foreground_guards_by_reason == {"never_promise": 1}
    assert run.guard_summary.pregen_rejections_total == 0


def test_variant_detection_matches_es_and_en_deck_prompts():
    """Verify prompt metadata extraction and variant auto-detection match the ES/EN deck strings."""
    es_v2 = (
        "TEMA APROBADO: Prueba\n"
        "Límite de turno: máximo 3 frases cortas y directas (~110 tokens), sin rodeos ni relleno.\n"
        "Mueve el bloque con este movimiento: Cuenta un caso concreto. No lo anuncies."
    )
    en_v2 = (
        "APPROVED TOPIC: Test\n"
        "Turn budget: at most 3 short, direct sentences (~110 tokens), with no padding or rambling.\n"
        "Move the conversation forward with this move: Ground it in a concrete case. Do not announce the move."
    )
    es_v4 = (
        "TEMA APROBADO: Prueba\n"
        "Aperturas y giros ya usados en este tema; no los repitas: En realidad | Al final\n"
        "Mueve el bloque con este movimiento: Haz una pregunta al chat. No lo anuncies."
    )
    en_v4 = (
        "APPROVED TOPIC: Test\n"
        "Openings and phrasings already used in this topic; do not reuse them: Honestly | In the end\n"
        "Move the conversation forward with this move: Ask chat. Do not announce the move."
    )
    es_v5 = (
        "TEMA APROBADO: Prueba\n"
        "Foco de esta etapa: Mecanismo subyacente: explica el incentivo real. "
        "Ejecútalo con este movimiento: Plantea el dilema. "
        "No anuncies la etapa ni el movimiento, evita fórmulas fijas."
    )
    en_v5 = (
        "APPROVED TOPIC: Test\n"
        "Stage focus: Underlying mechanism: explain the real incentive. "
        "Carry it out with this move: Lay out the dilemma. "
        "Do not announce the stage or the move, avoid stock openers."
    )

    assert detect_variant_from_prompt(es_v2) == "v2"
    assert detect_variant_from_prompt(en_v2) == "v2"
    assert detect_variant_from_prompt(es_v4) == "v4"
    assert detect_variant_from_prompt(en_v4) == "v4"
    assert detect_variant_from_prompt(es_v5) == "v5"
    assert detect_variant_from_prompt(en_v5) == "v5"

    meta_es_v5 = extract_prompt_metadata([{"role": "user", "content": es_v5}])
    assert meta_es_v5["variant_hint"] == "v5"
    assert meta_es_v5["facet"] == "Mecanismo subyacente: explica el incentivo real."
    assert meta_es_v5["move"] == "Plantea el dilema."

    meta_en_v5 = extract_prompt_metadata([{"role": "user", "content": en_v5}])
    assert meta_en_v5["variant_hint"] == "v5"
    assert meta_en_v5["facet"] == "Underlying mechanism: explain the real incentive."
    assert meta_en_v5["move"] == "Lay out the dilemma."


