"""Phase-1 streaming loop (llm_output_streaming_20260813 design §3, §5, §6, §7).

Covers: the eligibility gate (an ineligible turn takes the buffered path
byte-identically), the sanitize-then-guard per-sentence order (the markdown
evasion 'Como *IA*, no puedo opinar.' must be BLOCKED), trip semantics on both
sides of the lazy first submit, the partial-turn exit on mid-stream failures,
generator close on every abort path, [STREAM_TTFA] telemetry, the
reconstructed `respuesta` keeping ctx telemetry alive, and the no-dialogue-in-
logs rule for every stream-tagged log line.

Phase 2 (§9 decision 1, §10) extends it to owner bundles: eligibility, and the
requeue-AND-emit contract for a bundle that dies after its first submit.
"""

import logging
import re
import queue
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from opencohost.config.settings import OWNER_BUNDLE_SOURCE
from opencohost.core import llm_engine


class FakeMessage:
    def __init__(self, content="", thinking=None):
        self.content = content
        self.thinking = thinking


class FakeChunk:
    """Attr-shaped like ollama's ChatResponse chunks; `.get`-capable like the
    attempt loop expects of the final `respuesta` object."""

    def __init__(self, content="", done=False, **stats):
        self.message = FakeMessage(content)
        self.done = done
        for key, value in stats.items():
            setattr(self, key, value)

    def get(self, key, default=None):
        return getattr(self, key, default)


class FakeJob:
    def __init__(self, job_id):
        self.job_id = job_id
        self.sealed = False


class FakeRouter:
    """Records the streaming API surface the loop is allowed to touch."""

    def __init__(self, append_results=None):
        self.jobs = []      # (job, source, priority)
        self.appends = []   # (job, chunks)
        self.sealed = []
        self._append_results = list(append_results or [])

    def submit_streaming(self, source, priority):
        job = FakeJob(len(self.jobs) + 1)
        self.jobs.append((job, source, priority))
        return job

    def append_chunks(self, job, chunks):
        self.appends.append((job, list(chunks)))
        if self._append_results:
            return self._append_results.pop(0)
        return True

    def seal(self, job):
        job.sealed = True
        self.sealed.append(job)


def _arm_stream(motor, chunks):
    """Install a fake `_ollama_chat_streaming` built on a REAL generator, so
    close() semantics (GeneratorExit -> finally) match the committed seam.
    An item that is an exception instance is raised in place of a yield."""
    record = {"closed": False, "yielded": 0, "kwargs": None}

    def _streaming(*, timeout, **kwargs):
        record["kwargs"] = {"timeout": timeout, **kwargs}

        def _gen():
            try:
                for item in chunks:
                    if isinstance(item, BaseException):
                        raise item
                    record["yielded"] += 1
                    yield item
            finally:
                record["closed"] = True

        return _gen()

    motor._ollama_chat_streaming = _streaming
    return record


@pytest.fixture
def motor(monkeypatch):
    m = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    m.current_model = "llama3"
    m.use_system_role = True
    m.ollama = MagicMock()
    m._speech_router_enabled = True
    monkeypatch.setattr(llm_engine, "LLM_STREAMING_ENABLED", True)
    router = FakeRouter()
    m._ensure_router = lambda: router
    m._speech_router = router
    m._fake_router = router
    return m


# ---------------------------------------------------------------- eligibility


@pytest.mark.parametrize(
    "breaker", ["flag_off", "router_off", "source_chat", "no_commit"]
)
def test_ineligible_turn_takes_buffered_path_byte_identically(
    motor, monkeypatch, breaker
):
    motor._ollama_chat_with_watchdog = MagicMock(
        return_value={"message": {"content": "Hola tranquila respuesta."}}
    )
    motor._ollama_chat_streaming = MagicMock(
        side_effect=AssertionError("streaming must not run for an ineligible turn")
    )
    kwargs = {"source": "direct", "commit_history": True}
    if breaker == "flag_off":
        monkeypatch.setattr(llm_engine, "LLM_STREAMING_ENABLED", False)
    elif breaker == "router_off":
        motor._speech_router_enabled = False
    elif breaker == "source_chat":
        kwargs["source"] = "chat"
    elif breaker == "no_commit":
        kwargs["commit_history"] = False

    dialogo = motor._generar_dialogo("hola", **kwargs)

    assert dialogo == "Hola tranquila respuesta."
    assert motor._ollama_chat_with_watchdog.call_count == 1
    assert motor._fake_router.jobs == []
    assert motor._fake_router.appends == []


def test_cloud_turn_is_not_stream_eligible_when_cloud_streaming_flag_off(motor, monkeypatch):
    """live_session_fixes_20260925 T8: cloud streaming is default ON, but its
    OWN gate (CLOUD_STREAMING_ENABLED) must still be able to force the
    byte-identical buffered path, independent of LLM_STREAMING_ENABLED."""
    monkeypatch.setattr(llm_engine, "CLOUD_STREAMING_ENABLED", False)
    motor._provider_config = {
        "active_provider": "openai",
        "fallback_mode": "manual",
        "profiles": {
            "openai": {"base_url": "https://api.example.com/v1", "model": "gpt-cloud"}
        },
    }
    motor._ollama_chat_with_watchdog = MagicMock(
        return_value={"message": {"content": "Respuesta desde la nube."}}
    )
    motor._llm_chat_streaming = MagicMock(
        side_effect=AssertionError("cloud transport must not stream when the flag is off")
    )

    dialogo = motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert dialogo == "Respuesta desde la nube."
    assert motor._ollama_chat_with_watchdog.call_count == 1
    assert motor._fake_router.jobs == []


# ------------------------------------------------------------------- success


def test_eligible_turn_streams_lazy_submits_and_seals(motor, caplog):
    record = _arm_stream(
        motor,
        [
            FakeChunk("Primera frase limpia. "),
            FakeChunk("Segunda frase tranquila."),
            FakeChunk(
                "",
                done=True,
                eval_count=194,
                prompt_eval_count=50,
                eval_duration=2_963_000_000,
                prompt_eval_duration=407_000_000,
                load_duration=175_000_000,
            ),
        ],
    )

    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        dialogo = motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert dialogo == "Primera frase limpia. Segunda frase tranquila."
    router = motor._fake_router
    assert len(router.jobs) == 1
    job, source, priority = router.jobs[0]
    assert source == "direct"
    assert priority == 0
    assert router.appends == [
        (job, ["Primera frase limpia."]),
        (job, ["Segunda frase tranquila."]),
    ]
    assert router.sealed == [job]
    assert record["closed"] is True
    ttfa = [r for r in caplog.records if "[STREAM_TTFA]" in r.getMessage()]
    assert len(ttfa) == 1
    assert "source=direct" in ttfa[0].getMessage()
    # full final text committed to history, exactly as today
    assert motor.historial[-1]["content"] == dialogo
    assert motor.historial[-2]["content"] == "hola"


def test_streamed_turn_skips_speak_or_submit_but_emits_dialogue(motor, caplog):
    _arm_stream(motor, [FakeChunk("Primera frase limpia. "), FakeChunk("", done=True)])
    motor._speak_or_submit = MagicMock()
    emitted = []
    motor.dialogue_callback = lambda text, source, **kw: emitted.append((text, source))

    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        motor._ejecutar_inferencia("hola", source="direct")

    motor._speak_or_submit.assert_not_called()
    assert emitted == [("Primera frase limpia.", "direct")]
    assert any("[TURN_LATENCY]" in r.getMessage() for r in caplog.records)


def test_buffered_turn_still_speaks_via_speak_or_submit(motor, monkeypatch):
    monkeypatch.setattr(llm_engine, "LLM_STREAMING_ENABLED", False)
    motor._ollama_chat_with_watchdog = MagicMock(
        return_value={"message": {"content": "Hola tranquila respuesta."}}
    )
    motor._speak_or_submit = MagicMock()

    motor._ejecutar_inferencia("hola", source="direct")

    motor._speak_or_submit.assert_called_once()
    assert motor._speak_or_submit.call_args.args[0] == "Hola tranquila respuesta."


def test_reconstructed_response_keeps_ctx_telemetry_fields(motor, caplog):
    _arm_stream(
        motor,
        [
            FakeChunk("Primera frase limpia. "),
            FakeChunk("Segunda frase tranquila."),
            FakeChunk(
                "",
                done=True,
                eval_count=194,
                prompt_eval_count=50,
                eval_duration=2_963_000_000,
                prompt_eval_duration=407_000_000,
                load_duration=175_000_000,
            ),
        ],
    )

    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        motor._generar_dialogo("hola", source="direct", commit_history=True)

    ctx = [r for r in caplog.records if "ctx_utilization" in r.getMessage()]
    assert len(ctx) == 1
    msg = ctx[0].getMessage()
    assert "prompt_eval_count=50" in msg
    assert "eval_count=194" in msg
    assert "load_ms=175" in msg
    snap = motor.ctx_telemetry_snapshot()
    assert snap["latest"]["prompt_eval_count"] == 50
    assert snap["latest"]["eval_count"] == 194


def test_empty_stream_takes_the_legacy_empty_retry_and_returns_empty(motor):
    """§3 loop-internal interaction: an empty stream means no sentence ever
    closed, so no job exists and the attempt loop's empty-response retry is
    safe and unchanged."""
    calls = {"n": 0}

    def _streaming(*, timeout, **kwargs):
        calls["n"] += 1

        def _gen():
            return
            yield  # pragma: no cover — makes this a generator

        return _gen()

    motor._ollama_chat_streaming = _streaming

    dialogo = motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert dialogo == ""
    assert calls["n"] >= 2  # the max_intentos retry loop actually re-streamed
    assert motor._fake_router.jobs == []
    assert list(motor.historial) == []


# ---------------------------------------------------------------- guard trips


def test_markdown_evasion_trips_per_sentence_guard_before_any_submit(motor):
    """'Como *IA*, no puedo opinar.' passes the RAW guard (R9 joins tokens with
    \\s+ and * is not whitespace) and the sanitizer then strips the asterisks.
    The per-sentence guard runs on the SANITIZED sentence, so it must block —
    and because it is sentence 1, no job may ever be created."""
    record = _arm_stream(
        motor,
        [
            FakeChunk("Como *IA*, no puedo opinar. "),
            FakeChunk("Segunda frase inocente."),
            FakeChunk("", done=True),
        ],
    )
    motor._retry_after_guard_block = MagicMock(return_value="")
    motor._guardrail_fallback_line = MagicMock(
        return_value="linea neutral de emergencia"
    )

    dialogo = motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert motor._fake_router.jobs == []
    assert motor._fake_router.appends == []
    # revert-to-buffered: the stream is consumed to completion, not aborted
    assert record["yielded"] == 3
    assert record["closed"] is True
    # full legacy fallback path, including the one-shot retry
    motor._retry_after_guard_block.assert_called_once()
    assert dialogo == "linea neutral de emergencia"
    assert motor.historial[-1]["content"] == dialogo


def test_per_sentence_guard_also_blocks_what_only_the_RAW_form_violates(motor, monkeypatch):
    """The other direction of the same asymmetry, and the reason the guard
    checks BOTH forms rather than just the sanitized one.

    Guarding only the sanitized sentence closes the markdown evasion (the test
    above) but opens the mirror hole: anything the sanitizer happens to mangle
    out of a pattern would sail through on the streamed path while the
    buffered path (`f07b360`) and the full-text backstop still block it. That
    asymmetry — streaming strictly weaker than buffered on any axis — is
    exactly what this track must not introduce.

    Simulated with a sanitizer that eats the violation, since today's real
    sanitizer only strips markdown and non-Latin glyphs and no shipped rule
    keys on those. It pins the CONTRACT, so a future sanitizer change cannot
    silently open the hole.
    """
    monkeypatch.setattr(
        llm_engine, "_sanitize_tts_text_for_playback",
        lambda text: text.replace("Como IA,", "Bueno,"),
    )
    _arm_stream(
        motor,
        [FakeChunk("Como IA, no puedo opinar. "), FakeChunk("", done=True)],
    )
    motor._retry_after_guard_block = MagicMock(return_value="")
    motor._guardrail_fallback_line = MagicMock(return_value="linea neutral")

    dialogo = motor._generar_dialogo("hola", source="direct", commit_history=True)

    # The sanitized form is clean, so a sanitized-only guard would have
    # submitted this to the air. The raw form violates R9, so nothing goes out.
    assert motor._fake_router.jobs == []
    assert motor._fake_router.appends == []
    assert dialogo == "linea neutral"


def test_sentence_one_trip_takes_legacy_retry_rescue(motor):
    _arm_stream(
        motor,
        [FakeChunk("Como modelo de lenguaje, no puedo. "), FakeChunk("", done=True)],
    )
    motor._retry_after_guard_block = MagicMock(
        return_value="Respuesta corregida tranquila."
    )

    dialogo = motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert dialogo == "Respuesta corregida tranquila."
    assert motor._fake_router.jobs == []
    assert motor.historial[-1]["content"] == dialogo


def test_later_sentence_trip_seals_commits_prefix_and_never_retries(motor, caplog):
    record = _arm_stream(
        motor,
        [
            FakeChunk("Primera frase limpia. "),
            FakeChunk("Como modelo de lenguaje, no puedo. "),
            FakeChunk("Tercera frase nunca vista."),
            FakeChunk("", done=True),
        ],
    )
    motor._retry_after_guard_block = MagicMock()
    motor._guardrail_fallback_line = MagicMock()

    with caplog.at_level(logging.WARNING, logger="OpenCohost"):
        dialogo = motor._generar_dialogo("hola", source="direct", commit_history=True)

    router = motor._fake_router
    assert dialogo == "Primera frase limpia."
    assert len(router.jobs) == 1
    job = router.jobs[0][0]
    # nothing appended after the trip
    assert router.appends == [(job, ["Primera frase limpia."])]
    assert router.sealed == [job]
    # never regenerate, never the canned line, mid-turn
    motor._retry_after_guard_block.assert_not_called()
    motor._guardrail_fallback_line.assert_not_called()
    # the spoken prefix is committed — Kira said it on air
    assert motor.historial[-1]["content"] == "Primera frase limpia."
    # the stream was aborted (chunk 3 never consumed) and closed
    assert record["yielded"] == 2
    assert record["closed"] is True
    trunc = [r for r in caplog.records if "stream_truncation" in r.getMessage()]
    assert len(trunc) == 1
    msg = trunc[0].getMessage()
    assert "no_ai_self_identification" in msg
    assert "sentence_index=2" in msg
    assert "spoken_upto=1" in msg


# ------------------------------------------------------------- partial exits


def test_midstream_watchdog_recovers_and_commits_spoken_prefix(motor, caplog):
    record = _arm_stream(
        motor,
        [
            FakeChunk("Primera frase limpia. "),
            TimeoutError("watchdog_timeout:45.00s"),
        ],
    )
    motor._recover_from_stalled_inference = MagicMock()

    with caplog.at_level(logging.WARNING, logger="OpenCohost"):
        dialogo = motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert dialogo == ""
    # the model IS stalled — rollback is about the model, not the turn
    motor._recover_from_stalled_inference.assert_called_once()
    router = motor._fake_router
    job = router.jobs[0][0]
    assert router.sealed == [job]
    assert motor.historial[-1]["content"] == "Primera frase limpia."
    assert record["closed"] is True
    partial = [r for r in caplog.records if "[STREAM_PARTIAL_EXIT]" in r.getMessage()]
    assert len(partial) == 1
    assert "TimeoutError" in partial[0].getMessage()


def test_hung_model_before_any_sentence_keeps_legacy_semantics(motor, caplog):
    record = _arm_stream(motor, [TimeoutError("watchdog_timeout:40.00s")])
    motor._recover_from_stalled_inference = MagicMock()

    with caplog.at_level(logging.WARNING, logger="OpenCohost"):
        dialogo = motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert dialogo == ""
    motor._recover_from_stalled_inference.assert_called_once()
    assert motor._fake_router.jobs == []
    assert list(motor.historial) == []
    assert record["closed"] is True
    assert not [r for r in caplog.records if "[STREAM_PARTIAL_EXIT]" in r.getMessage()]


def test_cancel_token_after_first_sentence_takes_partial_exit(motor, caplog):
    record = _arm_stream(
        motor,
        [
            FakeChunk("Primera frase limpia. "),
            FakeChunk("Segunda frase tranquila. "),
            FakeChunk("Tercera frase. "),
            FakeChunk("", done=True),
        ],
    )
    motor._speech_cancelled = MagicMock(side_effect=[False, True])

    with caplog.at_level(logging.WARNING, logger="OpenCohost"):
        dialogo = motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert dialogo == ""
    router = motor._fake_router
    job = router.jobs[0][0]
    assert router.appends == [(job, ["Primera frase limpia."])]
    assert router.sealed == [job]
    assert motor.historial[-1]["content"] == "Primera frase limpia."
    assert record["closed"] is True
    assert record["yielded"] == 2
    assert any(
        "[STREAM_PARTIAL_EXIT]" in r.getMessage()
        and "speech_cancelled" in r.getMessage()
        for r in caplog.records
    )


def test_cancel_token_before_first_sentence_aborts_with_no_job(motor):
    record = _arm_stream(
        motor, [FakeChunk("Primera frase limpia. "), FakeChunk("", done=True)]
    )
    motor._speech_cancelled = MagicMock(return_value=True)

    dialogo = motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert dialogo == ""
    assert motor._fake_router.jobs == []
    assert list(motor.historial) == []
    assert record["closed"] is True


def test_append_refused_midturn_seals_and_commits_prefix(motor, caplog):
    router = FakeRouter(append_results=[True, False])
    motor._ensure_router = lambda: router
    motor._speech_router = router
    motor._fake_router = router
    record = _arm_stream(
        motor,
        [
            FakeChunk("Primera frase limpia. "),
            FakeChunk("Segunda frase tranquila. "),
            FakeChunk("Tercera frase. "),
            FakeChunk("", done=True),
        ],
    )

    with caplog.at_level(logging.WARNING, logger="OpenCohost"):
        dialogo = motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert dialogo == ""
    job = router.jobs[0][0]
    assert router.sealed == [job]
    assert motor.historial[-1]["content"] == "Primera frase limpia."
    assert record["closed"] is True
    assert any("append_refused" in r.getMessage() for r in caplog.records)


# -------------------------------------------------------- ADR-039 divergence


def _fake_sanitizer_result():
    return SimpleNamespace(
        verdict="repaired",
        text="TEXTO MUTADO POR EL SANITIZADOR",
        removed_fragments=1,
        distinct_looping=1,
        max_occurrences=2,
        original_len=10,
        remaining_len=5,
        removed_pct=0.5,
    )


def test_clause_sanitizer_is_verdict_log_only_for_streamed_turns(
    motor, monkeypatch, caplog
):
    monkeypatch.setattr(llm_engine, "CLAUSE_SANITIZER_SOURCES", frozenset({"direct"}))
    monkeypatch.setattr(
        llm_engine, "sanitize_clause_repetition", lambda text: _fake_sanitizer_result()
    )
    _arm_stream(motor, [FakeChunk("Primera frase limpia. "), FakeChunk("", done=True)])

    with caplog.at_level(logging.DEBUG, logger="OpenCohost"):
        dialogo = motor._generar_dialogo("hola", source="direct", commit_history=True)

    # the repair mutation is NOT applied to a turn already speaking...
    assert dialogo == "Primera frase limpia."
    # ...but the ADR-039 counters keep accruing
    assert any("[CLAUSE_SANITIZER]" in r.getMessage() for r in caplog.records)


def test_clause_sanitizer_still_repairs_buffered_turns(motor, monkeypatch):
    monkeypatch.setattr(llm_engine, "CLAUSE_SANITIZER_SOURCES", frozenset({"direct"}))
    monkeypatch.setattr(
        llm_engine, "sanitize_clause_repetition", lambda text: _fake_sanitizer_result()
    )
    monkeypatch.setattr(llm_engine, "LLM_STREAMING_ENABLED", False)
    motor._ollama_chat_with_watchdog = MagicMock(
        return_value={"message": {"content": "Primera frase limpia."}}
    )

    dialogo = motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert dialogo == "TEXTO MUTADO POR EL SANITIZADOR"


# -------------------------------------------------------------------- privacy


def test_stream_log_lines_carry_no_dialogue_text(motor, caplog):
    secret = "zanahoria-cuantica-42"
    _arm_stream(
        motor,
        [
            FakeChunk(f"Primera frase con {secret} adentro. "),
            FakeChunk("Como modelo de lenguaje, no puedo. "),
            FakeChunk("", done=True),
        ],
    )

    with caplog.at_level(logging.DEBUG, logger="OpenCohost"):
        motor._generar_dialogo("hola", source="direct", commit_history=True)

    stream_lines = [
        r.getMessage()
        for r in caplog.records
        if "[STREAM_" in r.getMessage() or "stream_truncation" in r.getMessage()
    ]
    assert stream_lines  # the stream-tagged telemetry fired
    assert all(secret not in line for line in stream_lines)


# ------------------------------------------- the empty return that DID air


def test_partial_exit_emits_the_spoken_prefix_and_never_says_turn_dropped(motor):
    """Streaming creates a state that could not exist before: generation
    returns "" and yet audio ALREADY went out.

    Every empty-return branch in `_ejecutar_inferencia` was written on the
    premise that empty means silence -- the `turn_dropped` toast says so in
    its own comment. On a partial exit that premise is false: the audience
    heard the head. Firing the toast would tell the owner nothing aired while
    it audibly did, and skipping `_emit_dialogue` would leave the transcript
    blank for words the stream already carried.
    """
    seen = []
    motor.ui_callback = lambda event, *a, **k: seen.append(event)
    motor._emit_dialogue = MagicMock()
    _arm_stream(
        motor,
        [
            FakeChunk("Primera frase limpia. "),
            FakeChunk("Segunda frase tranquila. "),
            FakeChunk("", done=True),
        ],
    )
    motor._speech_cancelled = MagicMock(side_effect=[False, True])

    motor._ejecutar_inferencia("hola", source="direct")

    assert "turn_dropped" not in seen
    motor._emit_dialogue.assert_called_once_with(
        "Primera frase limpia.", "direct", web_searched=False
    )
    # Committed exactly once, by the partial exit itself -- not twice.
    assert motor.historial[-1]["content"] == "Primera frase limpia."


def test_a_genuinely_silent_direct_turn_still_reports_turn_dropped(motor):
    """The regression guard for the branch above: when NOTHING aired, the
    owner must still be told, exactly as before this track."""
    seen = []
    motor.ui_callback = lambda event, *a, **k: seen.append(event)
    motor._emit_dialogue = MagicMock()
    _arm_stream(motor, [FakeChunk("", done=True)])

    motor._ejecutar_inferencia("hola", source="direct")

    assert motor._fake_router.jobs == []
    assert "turn_dropped" in seen
    motor._emit_dialogue.assert_not_called()


def test_an_empty_agenda_turn_still_signals_accept_agenda_output(motor):
    """The other branch the streamed-prefix handling must never shadow: an
    agenda source is not stream-eligible, so an empty generation still has to
    reach the ADR-011 recovery ladder through _accept_agenda_output("")."""
    motor._ollama_chat_with_watchdog = MagicMock(return_value={"message": {"content": ""}})
    motor._accept_agenda_output = MagicMock(return_value=True)

    motor._ejecutar_inferencia("temita", source="kira-agenda")

    assert motor._fake_router.jobs == []
    motor._accept_agenda_output.assert_called_once_with("")


# --------------------------------------------- phase 2: streamed owner bundles


def _follower(text, source="direct", priority=0, ts=1.0):
    """A `_priority_queue` tuple: (priority, insertion_ts, text, source)."""
    return (priority, ts, text, source)


def test_bundle_turn_is_stream_eligible(motor):
    """Phase 2 (§10): owner bundles stream like any other owner turn.

    Phase 1 excluded them twice over — the source allow-list AND a
    `_turn_bundle_followers` check — and a bundle is relabelled to
    OWNER_BUNDLE_SOURCE before `_ejecutar_inferencia` ever sees it, so the
    source alone was already doing the excluding.
    """
    motor._ollama_chat_with_watchdog = MagicMock(
        side_effect=AssertionError("a bundle turn must stream in phase 2")
    )
    _arm_stream(motor, [FakeChunk("Primera frase limpia. "), FakeChunk("", done=True)])
    motor._speak_or_submit = MagicMock()

    motor._ejecutar_inferencia(
        "1. pregunta uno\n2. pregunta dos",
        source=OWNER_BUNDLE_SOURCE,
        bundle_followers=[_follower("pregunta dos", ts=2.0)],
    )

    router = motor._fake_router
    assert len(router.jobs) == 1
    job, job_source, priority = router.jobs[0]
    assert job_source == OWNER_BUNDLE_SOURCE
    assert priority == 0  # same D3 priority-0 band as direct/ptt
    assert router.appends == [(job, ["Primera frase limpia."])]
    motor._speak_or_submit.assert_not_called()


def test_streamed_bundle_completing_cleanly_never_requeues(motor):
    """The other side of owner decision 1: a turn that reaches `seal` answered
    what it was going to answer, so the followers stay consumed. Requeuing
    here would double-answer on EVERY bundle, not just on a death."""
    _arm_stream(
        motor,
        [
            FakeChunk("Primera frase limpia. "),
            FakeChunk("Segunda frase tranquila."),
            FakeChunk("", done=True),
        ],
    )
    followers = [_follower("pregunta dos", ts=2.0)]

    motor._ejecutar_inferencia(
        "1. pregunta uno\n2. pregunta dos",
        source=OWNER_BUNDLE_SOURCE,
        bundle_followers=followers,
    )

    router = motor._fake_router
    assert router.sealed == [router.jobs[0][0]]
    assert motor._priority_queue == []
    assert (
        motor.historial[-1]["content"]
        == "Primera frase limpia. Segunda frase tranquila."
    )


def test_streamed_bundle_death_requeues_every_follower_and_emits_the_prefix(
    motor, caplog
):
    """Owner decision 1 (§9, ratified 2026-08-13) and the branch collision it
    exposes.

    A streamed bundle that dies mid-stream has BOTH a spoken prefix AND
    followers. Phase 1's `elif streamed_prefix:` swallowed the requeue branch,
    so the followers died silently — exactly the incident-grade loss
    `_requeue_owner_bundle_followers` exists to prevent. Both must happen: the
    audience heard the head (emit + commit it) and every absorbed question goes
    back with its ORIGINAL tuple, accepting a possible double answer on air.
    """
    followers = [
        _follower("pregunta dos", ts=2.0),
        _follower("pregunta tres", "ptt", ts=3.0),
    ]
    seen = []
    motor.ui_callback = lambda event, *a, **k: seen.append(event)
    motor._emit_dialogue = MagicMock()
    motor._recover_from_stalled_inference = MagicMock()
    _arm_stream(
        motor,
        [
            FakeChunk("Primera frase limpia. "),
            TimeoutError("watchdog_timeout:45.00s"),
        ],
    )

    with caplog.at_level(logging.WARNING, logger="OpenCohost"):
        motor._ejecutar_inferencia(
            "1. pregunta uno\n2. pregunta dos\n3. pregunta tres",
            source=OWNER_BUNDLE_SOURCE,
            bundle_followers=followers,
        )

    # never lost: the ORIGINAL tuples, so TTL and ordering survive
    assert motor._priority_queue == followers
    assert motor._priority_queue[0] is followers[0]
    assert motor._priority_queue[1] is followers[1]
    assert any("[BUNDLE_FAILED]" in r.getMessage() for r in caplog.records)
    # and the head that DID air is emitted and committed, not reported dropped
    motor._emit_dialogue.assert_called_once_with(
        "Primera frase limpia.", OWNER_BUNDLE_SOURCE, web_searched=False
    )
    assert motor.historial[-1]["content"] == "Primera frase limpia."
    assert "turn_dropped" not in seen
    router = motor._fake_router
    assert router.sealed == [router.jobs[0][0]]


def test_streamed_bundle_death_before_any_audio_requeues_with_no_emit(motor):
    """Pre-first-submit the legacy semantics are untouched: nothing aired, so
    the requeue happens alone — no prefix to emit, no double-answer risk."""
    followers = [_follower("pregunta dos", ts=2.0)]
    motor._emit_dialogue = MagicMock()
    motor._recover_from_stalled_inference = MagicMock()
    _arm_stream(motor, [TimeoutError("watchdog_timeout:40.00s")])

    motor._ejecutar_inferencia(
        "1. pregunta uno\n2. pregunta dos",
        source=OWNER_BUNDLE_SOURCE,
        bundle_followers=followers,
    )

    assert motor._fake_router.jobs == []
    assert motor._priority_queue == followers
    motor._emit_dialogue.assert_not_called()
    assert list(motor.historial) == []


def test_non_streamed_bundle_requeues_exactly_as_before(motor, monkeypatch):
    """The buffered path keeps the ORIGINAL invariant: an empty bundle turn
    spends its head and hands every follower back, with nothing emitted."""
    monkeypatch.setattr(llm_engine, "LLM_STREAMING_ENABLED", False)
    motor._ollama_chat_with_watchdog = MagicMock(return_value={"message": {"content": ""}})
    motor._emit_dialogue = MagicMock()
    followers = [_follower("pregunta dos", ts=2.0)]

    motor._ejecutar_inferencia(
        "1. pregunta uno\n2. pregunta dos",
        source=OWNER_BUNDLE_SOURCE,
        bundle_followers=followers,
    )

    assert motor._fake_router.jobs == []
    assert motor._priority_queue == followers
    motor._emit_dialogue.assert_not_called()


# ------------------------------------------- tts-first-chunk-boost-20261015


def _ttfa_message(caplog):
    lines = [r.getMessage() for r in caplog.records if "[STREAM_TTFA]" in r.getMessage()]
    assert len(lines) == 1
    return lines[0]


def test_first_chunk_boost_submits_at_comma_and_logs_soft_boundary(motor, monkeypatch, caplog):
    from opencohost.core.engine import generation_orchestrator as go

    monkeypatch.setattr(go, "TTS_FIRST_CHUNK_BOOST", True)
    _arm_stream(
        motor,
        [
            FakeChunk("Bueno amigos de la audiencia, "),
            FakeChunk("hoy toca hablar de cosas tranquilas, "),
            FakeChunk("con calma. "),
            FakeChunk("", done=True),
        ],
    )

    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        motor._generar_dialogo("hola", source="direct", commit_history=True)

    appends = [chunks for _job, chunks in motor._fake_router.appends]
    assert appends[0] == ["Bueno amigos de la audiencia,"]
    assert appends[1] == ["hoy toca hablar de cosas tranquilas, con calma."]
    msg = _ttfa_message(caplog)
    assert "first_chunk_boundary=soft" in msg
    assert "Bueno" not in msg


def test_first_chunk_boost_off_is_sentence_only(motor, monkeypatch, caplog):
    from opencohost.core.engine import generation_orchestrator as go

    monkeypatch.setattr(go, "TTS_FIRST_CHUNK_BOOST", False)
    _arm_stream(
        motor,
        [FakeChunk("Bueno amigos de la audiencia, hoy toca hablar. "), FakeChunk("", done=True)],
    )

    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        motor._generar_dialogo("hola", source="direct", commit_history=True)

    appends = [chunks for _job, chunks in motor._fake_router.appends]
    assert appends == [["Bueno amigos de la audiencia, hoy toca hablar."]]
    assert "first_chunk_boundary=sentence" in _ttfa_message(caplog)


def test_soft_first_chunk_still_goes_through_the_per_sentence_guard(motor, monkeypatch):
    from opencohost.core.engine import generation_orchestrator as go

    monkeypatch.setattr(go, "TTS_FIRST_CHUNK_BOOST", True)
    _arm_stream(
        motor,
        [FakeChunk("Como *IA* que soy yo, no puedo opinar. "), FakeChunk("", done=True)],
    )
    motor._generar_dialogo("hola", source="direct", commit_history=True)
    # the soft chunk "Como *IA* que soy yo," is guarded like any sentence:
    # nothing violating reached the router
    assert motor._fake_router.appends == []


# ------------------ tts-first-chunk-boost-20261015: correction round (judge)


def _arm_boost(monkeypatch):
    from opencohost.core.engine import generation_orchestrator as go

    monkeypatch.setattr(go, "TTS_FIRST_CHUNK_BOOST", True)
    return go


def _appended(motor):
    return [chunks for _job, chunks in motor._fake_router.appends]


@pytest.mark.parametrize(
    "text, soft_chunk",
    [
        # (a) R3: certainty marker in the soft chunk, outcome object in the rest
        (
            "Te aseguro una cosa muy clara: vas a ganar el sorteo. ",
            "Te aseguro una cosa muy clara:",
        ),
        # (b) R4: 'listo, ya lo hice' straddles the comma
        ("Bueno chat, pues listo, ya lo hice. ", "Bueno chat, pues listo,"),
        # (c) K8: the 'en el sentido biologico' tail is in the continuation
        (
            "No tengo deseos ni sentimientos, en el sentido biologico. ",
            "No tengo deseos ni sentimientos,",
        ),
    ],
    ids=["R3_cooccurrence", "R4_listo_ya_lo_hice", "K8_sentido_biologico"],
)
def test_violation_spanning_a_soft_boundary_is_blocked_before_tts(
    motor, monkeypatch, request, text, soft_chunk
):
    from opencohost.config import warmth as warmth_state

    # K8 only runs at the feeling warmth levels; the others are level-agnostic
    warmth_state.set_active_warmth("in_character")
    request.addfinalizer(lambda: warmth_state.set_active_warmth("cold"))
    _arm_boost(monkeypatch)
    _arm_stream(motor, [FakeChunk(text), FakeChunk("", done=True)])

    motor._generar_dialogo("hola", source="direct", commit_history=True)

    # the lead-in is already on air; the completing chunk must never be spoken
    assert _appended(motor) == [[soft_chunk]]
    assert motor._fake_router.jobs[0][0].sealed is True


def test_continuation_starting_with_confirmado_is_not_a_sentence_initial_false_positive(
    motor, monkeypatch
):
    _arm_boost(monkeypatch)
    _arm_stream(
        motor,
        [
            FakeChunk("Bueno chat esto es lo que pasa, confirmado, el evento sigue en pie. "),
            FakeChunk("", done=True),
        ],
    )

    motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert _appended(motor) == [
        ["Bueno chat esto es lo que pasa,"],
        ["confirmado, el evento sigue en pie."],
    ]


def test_genuine_sentence_initial_confirmado_still_blocks(motor, monkeypatch):
    _arm_boost(monkeypatch)
    _arm_stream(
        motor,
        [FakeChunk("Confirmado, chat gracias por todo amigos. "), FakeChunk("", done=True)],
    )
    motor._generar_dialogo("hola", source="direct", commit_history=True)
    assert _appended(motor) == []


def test_stream_cut_by_length_after_a_soft_chunk_still_seals_the_job(motor, monkeypatch):
    """C2: soft chunk spoken, then done_reason=length with no terminator. The
    trimmed text is empty, so the verdict code never ran: the job stayed open
    and the router starved forever. The empty branch must seal it and commit
    the spoken prefix."""
    _arm_boost(monkeypatch)
    _arm_stream(
        motor,
        [
            FakeChunk("Bueno amigos de la audiencia, "),
            FakeChunk("hoy toca hablar de cosas"),
            FakeChunk("", done=True, done_reason="length"),
        ],
    )

    result = motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert not result
    job = motor._fake_router.jobs[0][0]
    assert job.sealed is True
    assert _appended(motor) == [["Bueno amigos de la audiencia,"]]
    assert motor._streamed_turn_prefix == "Bueno amigos de la audiencia,"


def test_first_sentence_violation_after_a_soft_chunk_truncates_not_retries(
    motor, monkeypatch
):
    """W1 (accepted cost, pinned): once the soft lead-in is on air the turn can
    only truncate; the guard retry nudge / canned fallback is no longer
    reachable for that sentence."""
    _arm_boost(monkeypatch)
    motor._retry_after_guard_block = MagicMock(
        side_effect=AssertionError("retry path must not run after audio is out")
    )
    _arm_stream(
        motor,
        [
            FakeChunk(
                "Bueno, dejame pensarlo un momento, como inteligencia artificial no puedo opinar. "
            ),
            FakeChunk("", done=True),
        ],
    )

    motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert _appended(motor) == [["Bueno, dejame pensarlo un momento,"]]
    assert motor._fake_router.jobs[0][0].sealed is True


@pytest.mark.parametrize(
    "think, boost_flag, expected",
    [
        (True, True, False),
        (False, True, True),
        (None, True, True),
        (False, False, False),
        (None, False, False),
    ],
)
def test_boost_gate_truth_table(monkeypatch, think, boost_flag, expected):
    """W2 (owner decision 2026-10-15, activate-then-measure): the boost is on for
    think=None and think=False, off only when think is truthy."""
    from types import SimpleNamespace

    from opencohost.core.engine import generation_orchestrator as go

    monkeypatch.setattr(go, "TTS_FIRST_CHUNK_BOOST", boost_flag)
    assert go._first_chunk_boost_enabled(SimpleNamespace(think=think)) is expected


def test_boost_is_off_when_the_turn_may_think(motor, monkeypatch, caplog):
    """W2: a turn with think truthy can emit reasoning in content; the sentence
    splitter must then hold the whole first sentence."""
    _arm_boost(monkeypatch)
    motor.current_model = "qwen3:8b"
    monkeypatch.setattr(
        go_mod(), "_first_chunk_boost_enabled", lambda setup: False
    )
    _arm_stream(
        motor,
        [
            FakeChunk("Let me think about this, the user wants a joke</think>"),
            FakeChunk("Hola amigos de la casa, que tal todo. "),
            FakeChunk("", done=True),
        ],
    )

    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert _appended(motor) == [["Hola amigos de la casa, que tal todo."]]
    assert "first_chunk_boundary=sentence" in _ttfa_message(caplog)


def go_mod():
    from opencohost.core.engine import generation_orchestrator as go

    return go


def _rescue_lines(caplog):
    return [r for r in caplog.records if "[THINK_RESCUE]" in r.getMessage()]


def test_think_rescue_logs_spoken_before_no(motor, monkeypatch, caplog):
    _arm_boost(monkeypatch)
    monkeypatch.setattr(go_mod(), "_first_chunk_boost_enabled", lambda setup: False)
    _arm_stream(
        motor,
        [
            FakeChunk("SECRETREASONING the user wants a joke</think>"),
            FakeChunk("Hola amigos de la casa, que tal todo. "),
            FakeChunk("", done=True),
        ],
    )

    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        motor._generar_dialogo("hola", source="direct", commit_history=True)

    lines = _rescue_lines(caplog)
    assert len(lines) == 1
    assert lines[0].levelno == logging.INFO
    msg = lines[0].getMessage()
    assert "source=direct" in msg
    assert "boost=off" in msg
    assert "spoken_before=no" in msg
    assert re.search(r"held_chars=\d+", msg)
    assert "SECRETREASONING" not in msg and "joke" not in msg


def test_think_rescue_logs_spoken_before_yes(motor, monkeypatch, caplog):
    _arm_boost(monkeypatch)
    monkeypatch.setattr(go_mod(), "_first_chunk_boost_enabled", lambda setup: True)
    _arm_stream(
        motor,
        [
            FakeChunk("Bueno amigos de la audiencia, "),
            FakeChunk("SECRETREASONING pensando</think>"),
            FakeChunk("Hola amigos de la casa, que tal todo. "),
            FakeChunk("", done=True),
        ],
    )

    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        motor._generar_dialogo("hola", source="direct", commit_history=True)

    lines = _rescue_lines(caplog)
    assert len(lines) == 1
    msg = lines[0].getMessage()
    assert "spoken_before=yes" in msg
    assert "boost=on" in msg
    assert "SECRETREASONING" not in msg and "audiencia" not in msg


def test_no_think_rescue_line_without_orphan_close(motor, monkeypatch, caplog):
    _arm_boost(monkeypatch)
    _arm_stream(
        motor,
        [FakeChunk("Hola amigos de la casa, que tal todo. "), FakeChunk("", done=True)],
    )
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        motor._generar_dialogo("hola", source="direct", commit_history=True)
    assert _rescue_lines(caplog) == []


def test_tail_chunks_keep_their_real_boundary_kind(motor, monkeypatch, caplog):
    """S2: a soft chunk released from the think-splitter tail must not be
    reported as a sentence boundary."""
    go = _arm_boost(monkeypatch)

    class _TailSplitter:
        def __init__(self, **kwargs):
            pass

        def feed(self, delta):
            return delta, ""

        def flush(self):
            return " hoy toca hablar de cosas tranquilas, y mas. ", ""

        orphan_close_seen = False

    monkeypatch.setattr(go, "ThinkTagStreamSplitter", _TailSplitter)
    _arm_stream(
        motor,
        [FakeChunk("Bueno amigos de la audiencia,"), FakeChunk("", done=True)],
    )

    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert _appended(motor)[0] == ["Bueno amigos de la audiencia,"]
    assert "first_chunk_boundary=soft" in _ttfa_message(caplog)


# ------------------ tts-first-chunk-boost-20261015: round 3 (newline at the soft join)


def test_newline_after_soft_mark_does_not_hide_a_sentence_initial_violation(motor, monkeypatch):
    """C3: a newline after a soft mark must keep R4's sentence-initial
    'confirmado' visible; the soft join used to flatten it into a space."""
    _arm_boost(monkeypatch)
    _arm_stream(
        motor,
        [
            FakeChunk("Esto es lo que hay:\nConfirmado, el sorteo es manana. "),
            FakeChunk("", done=True),
        ],
    )

    motor._generar_dialogo("hola", source="direct", commit_history=True)

    spoken = " ".join(" ".join(c) for c in _appended(motor))
    assert "sorteo" not in spoken


def test_soft_head_is_cleared_when_the_completing_chunk_sanitizes_to_empty(motor, monkeypatch):
    """S1: a leftover soft_head must not prefix the NEXT sentence's guard text."""
    from opencohost.core.engine.generation_orchestrator import StreamAttemptState

    state = StreamAttemptState(source="direct")
    state.soft_head = "Esto es lo que hay"
    monkeypatch.setattr(motor, "_sanitize_for_tts", lambda s: [])
    action = motor._handle_stream_sentence("***", state, type("S", (), {})())
    assert action == "continue"
    assert state.soft_head == ""



# ----------------------------------------------- no_code_output (T1, 20261109)


def test_fenced_block_split_across_sentences_never_reaches_tts(motor):
    """Sentence 1 is clean and goes on air; the opening fence trips the guard,
    and every later sentence of the block is skipped, harmless-looking or not."""
    record = _arm_stream(
        motor,
        [
            FakeChunk("Te lo explico con calma. "),
            FakeChunk("```python\nx = 1\n"),
            FakeChunk("Esto parece inocente. "),
            FakeChunk("Esto tambien. "),
            FakeChunk("```\n"),
            FakeChunk("", done=True),
        ],
    )
    motor._retry_after_guard_block = MagicMock()
    motor._guardrail_fallback_line = MagicMock()

    dialogo = motor._generar_dialogo("dame codigo", source="direct", commit_history=True)

    router = motor._fake_router
    job = router.jobs[0][0]
    assert router.appends == [(job, ["Te lo explico con calma."])]
    assert dialogo == "Te lo explico con calma."
    motor._retry_after_guard_block.assert_not_called()
    assert record["closed"] is True


def test_code_before_the_first_submit_reverts_to_the_buffered_retry(motor):
    _arm_stream(
        motor,
        [FakeChunk("```python\nprint(1)\n"), FakeChunk("Frase inocente. "), FakeChunk("", done=True)],
    )
    motor._retry_after_guard_block = MagicMock(return_value="Te lo explico hablando.")

    dialogo = motor._generar_dialogo("dame codigo", source="direct", commit_history=True)

    assert motor._fake_router.jobs == []
    assert motor._fake_router.appends == []
    motor._retry_after_guard_block.assert_called_once()
    assert "no_code_output" in motor._retry_after_guard_block.call_args.kwargs["guard_reason"]
    assert dialogo == "Te lo explico hablando."


def test_open_fence_state_blocks_a_harmless_looking_sentence(motor):
    """Fence state: once an opening fence was seen, a sentence that alone looks
    harmless is still a hit (unit-level, independent of the guard regexes)."""
    from opencohost.core.engine.generation_orchestrator import StreamAttemptState

    state = StreamAttemptState(source="direct")
    state.in_fence = True
    state.job = object()  # audio already started

    action = motor._generation_orchestrator.handle_stream_sentence(
        "Esto parece inocente.", state, SimpleNamespace(web_searched=False)
    )

    assert action == "truncate"
    assert state.trip is not None and state.trip[0] == "no_code_output"
