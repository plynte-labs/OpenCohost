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


def test_cloud_turn_is_not_stream_eligible(motor):
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
    motor._ollama_chat_streaming = MagicMock(
        side_effect=AssertionError("cloud transport must never stream in phase 1")
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
    motor._emit_dialogue.assert_called_once_with("Primera frase limpia.", "direct")
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
        "Primera frase limpia.", OWNER_BUNDLE_SOURCE
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
