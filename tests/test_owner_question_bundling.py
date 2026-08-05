"""Step 3 of `interruptible_speech_architecture_20260804` — N owner questions
become ONE request and ONE spoken answer.

Measured problem (design §1, logs/opencohost_20260803_135755.log): with a fixed
service rate of one question per ~108 s median turn, a burst of questions asked
during a long agenda block is answered one at a time and the backlog grows
without bound — the owner eventually heard the answer to a question he had asked
seventeen minutes earlier, at 99.8% engine utilization.

Two rules ship together and neither works alone (design §2, §2c):
  Rule 1 — a draft that already exists is ALWAYS spoken (the owner's "ya pagó el
  pago de la GPU"). A bundle only forms on a pregen cache MISS, which is the
  case where the engine was about to pay for a generation anyway.
  Rule 2 — no NEW single-question draft is started while >= 2 owner questions
  pend. Without it, every arrival during playback drafts the next single
  question, each draft is a Rule-1 hit, and the bundle never forms: the busier
  the session, the less bundling fires.

The bundle is FRAME-LOCAL — synthesized at the pop, consumed in the same frame,
never queued, never pregenerated, no window and no TTL. That single property is
why a question can be neither lost nor answered twice (design §3.4, §7).

Convention mirrors tests/test_direct_turn_preemption.py: a constructed-but-
unstarted motor with `_generar_dialogo`/`_hablar` patched so no Ollama/TTS runs,
and every cross-thread sequence driven by `threading.Event` — never
`time.sleep`.
"""

import logging
import queue
import re
import threading
import time
from unittest.mock import MagicMock

from opencohost.core.llm_engine import MotorVocalIA


def _bare_motor() -> MotorVocalIA:
    # Hermetic (test_direct_turn_preemption.py:_bare_motor): pin the model and
    # seed both caches so nothing reaches the live ollama.show probe.
    motor = MotorVocalIA(queue.Queue(), lambda event: None)
    motor.current_model = "llama3"
    motor._reasoning_model_cache["llama3"] = False
    motor._model_ctx_limit = {"llama3": 8192}
    return motor


def _item(
    payload,
    source="direct",
    *,
    priority=1,
    age=0.0,
    history_text=None,
    submitted_at=None,
    submitted_under_provider=None,
):
    """A 7-tuple in the shape enqueue() stores, aged by `age` seconds.

    `age` also orders the bundle: `_compose_owner_bundle` presents members in
    QUEUE-timestamp order (the order the owner asked), which can differ from the
    priority order that decided which turn runs.
    """
    return (
        priority,
        time.time() - age,
        payload,
        source,
        history_text,
        submitted_at,
        submitted_under_provider,
    )


def _seed_queue(motor, items):
    with motor._pq_lock:
        motor._priority_queue = list(items)


def _fake_generation(motor):
    """Fake the LLM + TTS seams; stop the drain loop after exactly ONE turn.

    `_hablar` setting `_speaking` is what real TTS does, and it makes the
    enclosing `while True` in `_process_priority_queue` return on its next
    iteration — so each test observes one turn plus whatever the bundler chose
    to leave in the queue.
    """
    calls: list = []

    def _gen(
        contexto,
        source="direct",
        *,
        commit_history=True,
        log_prefix="LLM",
        history_text=None,
        watchdog_timeout=None,
    ):
        calls.append({"contexto": contexto, "source": source, "history_text": history_text})
        return "respuesta mezclada"

    def _speak(dialogo, source="direct"):
        motor._speaking = True

    motor._generar_dialogo = _gen
    motor._hablar = _speak
    return calls


def _bundle_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("[BUNDLE]")]


# ══════════════════════════════════════════════════════════════════════════
# T3.1 / T3.2 / T3.11 — the capacity claim itself
# ══════════════════════════════════════════════════════════════════════════


def test_three_owner_questions_become_exactly_one_llm_request():
    """T3.1 + T3.11. The capacity claim (design §1.1): the service rate scales
    with the backlog instead of being fixed at one question per turn.

    T3.11 rides along: a bundle never reaches the agenda guardrail — every one
    of those gates is `source.startswith("kira-agenda")` — so `last_outputs` and
    `skeleton_repetition` stay untouched by an owner turn.
    """
    motor = _bare_motor()
    calls = _fake_generation(motor)
    motor.agenda_output_validator = MagicMock()
    motor.agenda_output_recorder = MagicMock()
    _seed_queue(
        motor,
        [
            _item("que opinas de X", age=3.0),
            _item("y de Y", "ptt", priority=0, age=2.0),
            _item("cual recomiendas", age=1.0),
        ],
    )

    motor._process_priority_queue()

    assert len(calls) == 1, "three questions must cost ONE trip to the model"
    assert calls[0]["source"] == "owner-bundle"
    assert motor._priority_queue == []
    motor.agenda_output_validator.assert_not_called()
    motor.agenda_output_recorder.assert_not_called()


def test_the_one_request_carries_every_question_in_the_order_asked():
    """T3.2. Presentation order is the QUEUE timestamp (chronological — the
    order the owner asked), not the priority order that decided which item was
    popped: the PTT question below is priority 0 yet is presented second."""
    motor = _bare_motor()
    calls = _fake_generation(motor)
    _seed_queue(
        motor,
        [
            _item("que opinas de X", age=3.0),
            _item("y de Y", "ptt", priority=0, age=2.0),
            _item("cual recomiendas", age=1.0),
        ],
    )

    motor._process_priority_queue()

    contexto = calls[0]["contexto"]
    assert contexto.index("que opinas de X") < contexto.index("y de Y") < contexto.index("cual recomiendas")


def test_a_lone_owner_question_is_byte_identical_to_today(caplog):
    """T3.3 (N == 1). The new path activates ONLY when two or more owner
    questions are genuinely waiting — the exact condition that has never
    worked. A single question keeps its own tag and emits no [BUNDLE] line."""
    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor = _bare_motor()
    calls = _fake_generation(motor)
    _seed_queue(motor, [_item("una sola pregunta")])

    motor._process_priority_queue()

    assert [c["source"] for c in calls] == ["direct"]
    assert calls[0]["contexto"] == "una sola pregunta"
    assert _bundle_lines(caplog) == []


# ══════════════════════════════════════════════════════════════════════════
# T3.4 — the privacy pin (hard project rule: no raw viewer chat, anywhere)
# ══════════════════════════════════════════════════════════════════════════


def test_viewer_chat_can_never_enter_an_owner_bundle():
    """T3.4. `owner-bundle` is IN all five privacy frozensets, so its history
    pair is promotable to the digest and its prompt gets personalization,
    memorias, digest and editorial context. That is only safe because the prefix
    scan is structurally incapable of absorbing a non-owner item: a viewer
    `chat` message between two owner questions SPLITS the bundle rather than
    being jumped over (design §5.1, §7 "split loses ordering")."""
    motor = _bare_motor()
    calls = _fake_generation(motor)
    _seed_queue(
        motor,
        [
            _item("pregunta del streamer", age=3.0),
            _item("mensaje de un viewer", "chat", age=2.0),
            _item("segunda del streamer", age=1.0),
        ],
    )

    motor._process_priority_queue()

    assert len(calls) == 1
    assert calls[0]["source"] == "direct", "the scan stops at chat — no bundle this turn"
    assert calls[0]["contexto"] == "pregunta del streamer"
    assert "mensaje de un viewer" not in calls[0]["contexto"]
    # Neither the viewer message nor the second question was consumed.
    assert [it[2] for it in motor._priority_queue] == [
        "mensaje de un viewer",
        "segunda del streamer",
    ]


def test_the_inspector_still_hides_an_owner_bundle_user_slot():
    """§5.2. `_DIGEST_CAPTURE_SOURCES` IN gives the ASSISTANT slot (Kira's own
    on-air words are safe to show), but the inspector's user slot is the exact
    literal `source == "direct"` and stays that way: a bundle recap can contain
    PTT transcription, and `ptt` user slots are already hidden by the same
    fail-closed policy."""
    motor = _bare_motor()
    motor.historial.append({"role": "user", "content": "recap", "source": "owner-bundle"})
    motor.historial.append({"role": "assistant", "content": "respuesta", "source": "owner-bundle"})

    entries = motor.memory_inspector_snapshot()["entries"]

    assert "content" not in entries[-2]
    assert entries[-1]["content"] == "respuesta"


# ══════════════════════════════════════════════════════════════════════════
# T3.5 — Rule 1: paid work is always spoken
# ══════════════════════════════════════════════════════════════════════════


def test_a_cached_draft_for_the_head_suppresses_bundling_for_that_turn():
    """T3.5. Zero GPU re-payment: a draft that exists is never discarded to
    build a bundle. Rule 2 then keeps the remaining questions undrafted, so they
    genuinely bundle at the NEXT boundary instead of chaining (design §4b)."""
    motor = _bare_motor()
    calls = _fake_generation(motor)
    motor._prefetched_agenda = {
        "payload": "Q1",
        "dialogo": "respuesta ya pagada",
        "priority": 1,
        "source": "direct",
        "history_text": None,
        "gen_ms": 12000,
    }
    _seed_queue(motor, [_item("Q1", age=3.0), _item("Q2", age=2.0), _item("Q3", age=1.0)])

    motor._process_priority_queue()

    assert calls == [], "the draft was already paid for — nothing may regenerate"
    assert [it[2] for it in motor._priority_queue] == ["Q2", "Q3"]


# ══════════════════════════════════════════════════════════════════════════
# T3.6 / T3.7 — over cap: SPLIT by deferring in-queue, never drop
# ══════════════════════════════════════════════════════════════════════════


def test_a_burst_over_the_item_cap_defers_the_rest_in_queue():
    """T3.6. The deliberate divergence from `_flush_accumulation`'s drop-oldest
    (design §2a): dropping the host's own question is data loss. The second half
    is never removed from the queue — `_take_owner_bundle_prefix` simply stops
    taking, and the remainder stays sorted for the next bundle."""
    motor = _bare_motor()
    calls = _fake_generation(motor)
    _seed_queue(motor, [_item(f"Q{i}", age=float(10 - i)) for i in range(9)])

    motor._process_priority_queue()

    assert len(calls) == 1
    contexto = calls[0]["contexto"]
    assert all(f"Q{i}" in contexto for i in range(6)), "OWNER_BUNDLE_MAX_ITEMS = 6"
    assert not any(f"Q{i}" in contexto for i in range(6, 9))
    assert [it[2] for it in motor._priority_queue] == ["Q6", "Q7", "Q8"]


def test_the_char_cap_also_defers_instead_of_truncating():
    """T3.6 (char variant). OWNER_BUNDLE_MAX_CHARS = 2000 bounds the prompt so a
    bundle does not trip `context_budget.apply_char_budget` and evict
    conversation history. The split boundary is always a WHOLE question."""
    motor = _bare_motor()
    calls = _fake_generation(motor)
    first, second, third = "a" * 900, "b" * 900, "c" * 900
    _seed_queue(motor, [_item(first, age=3.0), _item(second, age=2.0), _item(third, age=1.0)])

    motor._process_priority_queue()

    contexto = calls[0]["contexto"]
    assert first in contexto and second in contexto
    assert third not in contexto
    assert [it[2] for it in motor._priority_queue] == [third]


def test_a_single_oversized_question_is_taken_alone_and_whole(caplog):
    """T3.7. A question longer than the char cap is taken alone and WHOLE —
    never truncated, never dropped. N == 1, so no bundle and no [BUNDLE] line."""
    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor = _bare_motor()
    calls = _fake_generation(motor)
    huge = "x" * 5000
    _seed_queue(motor, [_item(huge, age=2.0), _item("Q2", age=1.0)])

    motor._process_priority_queue()

    assert calls[0]["contexto"] == huge
    assert calls[0]["source"] == "direct"
    assert _bundle_lines(caplog) == []
    assert [it[2] for it in motor._priority_queue] == ["Q2"]


# ══════════════════════════════════════════════════════════════════════════
# T3.8 / T3.9 — the tag earns its five injections, and commits one clean pair
# ══════════════════════════════════════════════════════════════════════════


def test_all_five_injections_reach_an_owner_bundle_prompt(monkeypatch):
    """T3.8. Asserted on the BUILT messages, not on frozenset membership: the
    `_MEMORIA_INJECT_SOURCES` comment warns its gate has three sites (profile-id
    snapshot, store read, prompt prepend) and missing one leaves the path dead.
    Leaving any of these out would blank Kira's memory and identity precisely
    for the turns where the owner asked the most."""
    from opencohost.core import llm_engine as engine_mod

    motor = _bare_motor()
    monkeypatch.setattr(engine_mod, "PERSONALIZATION_ENABLED", True)
    monkeypatch.setattr(engine_mod, "MEMORIAS_ENABLED", True)
    monkeypatch.setattr(
        engine_mod.personalization,
        "build_injection_block",
        lambda _sanitizer: "<perfil_streamer>datos del streamer</perfil_streamer>",
    )
    motor._current_profile_id = "perfil-1"
    motor._build_memorias_injection_block = (
        lambda _pid, _ctx: "<memorias_guardadas>un recuerdo</memorias_guardadas>"
    )
    motor._memory_digest.append("hablamos de rendimiento")
    motor.direct_editorial_context_provider = lambda _ctx: "<editorial_context>ficha armada</editorial_context>"
    motor._resolve_chat_watchdog_timeout = lambda *a, **kw: 30.0

    setup = motor._build_generation_request(
        "1. que opinas de X\n2. y de Y",
        "owner-bundle",
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        watchdog_timeout=None,
    )

    blob = "\n".join(m["content"] for m in setup.messages)
    assert "<perfil_streamer>" in blob, "_PERSONALIZATION_INJECT_SOURCES"
    assert "<memorias_guardadas>" in blob, "_MEMORIA_INJECT_SOURCES (all three sites)"
    assert "<memoria_de_fondo" in blob, "_DIGEST_INJECT_SOURCES"
    assert "<editorial_context>" in blob, "_EDITORIAL_INJECT_SOURCES"


def test_the_bundle_commits_one_clean_history_pair():
    """T3.9 / OQ-6. What lands in `historial` — and therefore in memoria and the
    digest — is the honest recap built from each member's own `history_text`,
    NEVER the numbered prompt scaffolding. Storing "Respóndelas TODAS" there
    would put a prompt instruction into Kira's long-term memory."""
    motor = _bare_motor()

    def _gen(
        contexto,
        source="direct",
        *,
        commit_history=True,
        log_prefix="LLM",
        history_text=None,
        watchdog_timeout=None,
    ):
        # The real _generar_dialogo tail; the commit is what this test is about.
        motor._commit_history(contexto, "respuesta mezclada", source=source, history_text=history_text)
        return "respuesta mezclada"

    motor._generar_dialogo = _gen
    motor._hablar = lambda dialogo, source="direct": setattr(motor, "_speaking", True)
    _seed_queue(
        motor,
        [
            _item("que opinas de X", age=3.0, history_text="El streamer escribió: que opinas de X"),
            _item("y de Y", "ptt", priority=0, age=2.0, history_text="El streamer dijo: y de Y"),
        ],
    )

    motor._process_priority_queue()

    assert len(motor.historial) == 2, "ONE pair, not one per question"
    user, assistant = motor.historial[-2], motor.historial[-1]
    assert (user["role"], assistant["role"]) == ("user", "assistant")
    assert user["source"] == "owner-bundle" and assistant["source"] == "owner-bundle"
    assert "El streamer escribió: que opinas de X" in user["content"]
    assert "El streamer dijo: y de Y" in user["content"]
    assert "TODAS" not in user["content"], "prompt scaffolding must never reach memory"


# ══════════════════════════════════════════════════════════════════════════
# T3.10 — observability: the owner validates this feature by grepping logs
# ══════════════════════════════════════════════════════════════════════════


def test_the_bundle_log_line_reports_metadata_and_never_question_text(caplog):
    """T3.10 / OQ-8. `deferred > 0` is the split signal — this is how the owner
    proves a split happened. Metadata only, consistent with the project rule
    "Never expose raw chat in LLM prompts, logs, or persistence"."""
    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor = _bare_motor()
    _fake_generation(motor)
    submitted = time.monotonic() - 1.5
    items = [_item("pregunta hablada", "ptt", priority=0, age=9.0, submitted_at=submitted)]
    items += [_item(f"pregunta escrita {i}", age=float(8 - i)) for i in range(8)]
    _seed_queue(motor, items)

    motor._process_priority_queue()

    lines = _bundle_lines(caplog)
    assert len(lines) == 1
    match = re.fullmatch(
        r"\[BUNDLE\] count=6 sources=(\S+) chars=(\d+) oldest_wait_ms=(\d+) deferred=3",
        lines[0],
    )
    assert match, lines[0]
    assert match.group(1) == "direct:5,ptt:1"
    assert int(match.group(3)) >= 1500
    assert "pregunta" not in lines[0]
    # §8.3 grep line 2: the bundle carries the OLDEST member's stamp, so
    # [TURN_LATENCY]'s long form reports the worst real wait in the burst.
    latency = [r.getMessage() for r in caplog.records if "[TURN_LATENCY]" in r.getMessage()]
    assert len(latency) == 1 and "source=owner-bundle" in latency[0]
    assert "queue_wait_ms=" in latency[0]


# ══════════════════════════════════════════════════════════════════════════
# T3.12 / T3.13 — Rule 2, the load-bearing insight (design §2c)
# ══════════════════════════════════════════════════════════════════════════


def _spy_pregen(motor):
    spawned: list = []
    motor.pregenerate = lambda *args, **kwargs: (spawned.append(args), True)[1]
    return spawned


def test_no_single_question_draft_is_spawned_while_two_owner_questions_pend():
    """T3.12. Without this the feature inverts: every arrival during playback
    drafts the next single question, each draft is a Rule-1 hit, and the queue
    drains at one question per ~108 s turn — precisely the measured incident."""
    motor = _bare_motor()
    motor._speaking = True  # under cover of the previous answer's playback
    spawned = _spy_pregen(motor)
    _seed_queue(motor, [_item("Q1", age=2.0), _item("Q2", age=1.0)])

    motor._maybe_trigger_interactive_pregen((1, "Q1", "direct", None))

    assert spawned == [], "a burst the next boundary will bundle must not be drafted piecemeal"


def test_a_lone_owner_question_still_gets_its_draft():
    """T3.12 (inverse). Rule 2 removes spawns, never adds them — and it must not
    remove the ones that pay off. With one question pending there is no bundle
    to supersede the draft, so the WU3 interactive pregen is unchanged."""
    motor = _bare_motor()
    motor._speaking = True
    spawned = _spy_pregen(motor)
    _seed_queue(motor, [_item("Q1")])

    motor._maybe_trigger_interactive_pregen((1, "Q1", "direct", None))

    assert spawned == [("Q1", 1, "direct")]


def test_rule_two_does_not_touch_an_agenda_or_chat_head():
    """T3.13. Rule 2 keys on the HEAD's source. Agenda prefetch gating stays the
    driver's job (`has_pending_priority_before(2)`), and a chat head that will
    genuinely speak next may still draft."""
    motor = _bare_motor()
    motor._speaking = True
    spawned = _spy_pregen(motor)
    _seed_queue(motor, [_item("AG2", "kira-agenda", priority=2), _item("Q1", age=2.0), _item("Q2", age=1.0)])

    motor._maybe_trigger_interactive_pregen((2, "AG2", "kira-agenda", None))
    assert spawned == [("AG2", 2, "kira-agenda")]

    motor._prefetched_agenda = None
    motor._pregen_inflight = None
    spawned.clear()
    _seed_queue(motor, [_item("mensaje viewer", "chat"), _item("Q1", age=2.0), _item("Q2", age=1.0)])

    motor._maybe_trigger_interactive_pregen((1, "mensaje viewer", "chat", None))
    assert spawned == [("mensaje viewer", 1, "chat")]


# ══════════════════════════════════════════════════════════════════════════
# T-conc — no loss, no duplication, under real contention
# ══════════════════════════════════════════════════════════════════════════


def test_a_failed_bundle_answers_its_followers_on_the_next_boundary():
    """T3.14. A bundle that FAILS to generate must cost exactly ONE question —
    the head — the same as a failed single turn cost before bundling.

    The failure class is logged, not theoretical:
    `logs/opencohost_20260617_175453.log` at 18:05:24 — `qwopus` hung, the
    inference watchdog fired at 45.00 s, the engine rolled back to `gemma4:e2b`
    and *queue processing continued*. Every non-committing return
    `_invalidate_pregen_epoch` enumerates (watchdog timeout, exhausted transport
    retries, empty body, guardrail-no-fallback, chat-repetition fallback, the
    outer handler) lands in `_ejecutar_inferencia` as a falsy `dialogo`. The
    bundle is frame-local (§3.4), so the followers exist ONLY as `members` in
    `_process_priority_queue`'s frame: without a recovery hook that same
    incident silently eats up to OWNER_BUNDLE_MAX_ITEMS owner questions — no
    answer, no `Item expirado y omitido`, no re-queue. §7's "a question can be
    neither lost nor double-answered" depends on this.
    """
    motor = _bare_motor()
    calls: list = []

    def _gen(
        contexto,
        source="direct",
        *,
        commit_history=True,
        log_prefix="LLM",
        history_text=None,
        watchdog_timeout=None,
    ):
        calls.append(contexto)
        # First turn dies the way the watchdog kills one; the retry succeeds.
        return "" if len(calls) == 1 else "respuesta mezclada"

    motor._generar_dialogo = _gen
    motor._hablar = lambda dialogo, source="direct": setattr(motor, "_speaking", True)
    _seed_queue(motor, [_item("Q1", age=3.0), _item("Q2", age=2.0), _item("Q3", age=1.0)])

    motor._process_priority_queue()

    assert len(calls) == 2, "the followers of a failed bundle were never retried"
    assert all(q in calls[0] for q in ("Q1", "Q2", "Q3"))
    assert "Q2" in calls[1] and "Q3" in calls[1], "the followers were lost"
    assert "Q1" not in calls[1], "the head is consumed exactly once — never re-answered"
    assert motor._priority_queue == []


def test_the_requeued_followers_are_the_original_queue_tuples(caplog):
    """T3.15. The followers go back as the ORIGINAL tuples, through the same
    lock-and-re-sort discipline `enqueue` uses.

    Reconstructed approximations would reset the TTL clock and drop
    `history_text` / `submitted_at`, so a rescued question would answer with the
    raw prompt in memory and report a fake wait. Re-sorting is what keeps a
    priority-0 PTT follower ahead of an item that arrived while the failed
    generation was running.
    """
    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor = _bare_motor()
    motor._generar_dialogo = lambda *a, **kw: ""
    motor._hablar = MagicMock()
    spoken = _item(
        "pregunta hablada", "ptt", priority=0, age=9.0,
        history_text="El streamer dijo: pregunta hablada",
        submitted_at=time.monotonic() - 9.0,
    )
    written = _item("pregunta escrita", age=5.0, history_text="El streamer escribió: pregunta escrita")
    latecomer = _item("llegó mientras generaba", age=0.0)
    _seed_queue(motor, [latecomer])

    motor._ejecutar_inferencia(
        "1. pregunta hablada\n2. pregunta escrita",
        source="owner-bundle",
        bundle_followers=[spoken, written],
    )

    motor._hablar.assert_not_called()
    assert motor._priority_queue == [spoken, written, latecomer], "not re-sorted"
    assert motor._priority_queue[0] is spoken and motor._priority_queue[1] is written, (
        "re-queued items must be the original tuples, not rebuilt approximations"
    )
    # Metadata only, same rule as [BUNDLE]: the owner validates this by grepping.
    failed = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[BUNDLE_FAILED]")]
    assert failed == ["[BUNDLE_FAILED] requeued=2 sources=direct:1,ptt:1 head_consumed=1"]


def test_a_persistently_failing_model_drains_the_backlog_instead_of_spinning():
    """T3.16. Loop safety. Only the FOLLOWERS come back; the head is burned on
    every failure, exactly as a failed single turn burned it before bundling. So
    the queue shrinks by at least one item per drain iteration and a model that
    never answers empties the backlog instead of spinning on it forever.

    Re-queueing the head too would deadlock the show at the first stuck model.
    """
    motor = _bare_motor()
    calls: list = []

    def _gen(
        contexto,
        source="direct",
        *,
        commit_history=True,
        log_prefix="LLM",
        history_text=None,
        watchdog_timeout=None,
    ):
        calls.append(contexto)
        # Fail the test loudly instead of hanging the suite if the drain spins.
        assert len(calls) <= 10, "the drain loop never terminated — the head was re-queued"
        return ""

    motor._generar_dialogo = _gen
    motor._hablar = MagicMock()
    _seed_queue(motor, [_item(f"Q{i}", age=float(4 - i)) for i in range(4)])

    motor._process_priority_queue()

    assert len(calls) == 4, "one question burned per failure: bundles of 4, 3, 2, 1"
    assert motor._priority_queue == []
    motor._hablar.assert_not_called()


def test_every_question_reaches_exactly_one_bundle_under_concurrent_arrivals():
    """T-conc. The core invariant of Fork A (design §2a): a question exists in
    exactly ONE place — `_priority_queue` — and the pop that reads it removes it
    inside the same `_pq_lock` critical section, so there is no second store to
    fall out of sync.

    Staged with Events, never sleeps: the engine may not start draining until
    the first burst is queued (so a real bundle is guaranteed to form), and the
    second burst may not arrive until the engine is provably inside a
    generation (so arrivals genuinely overlap the drain).
    """
    motor = _bare_motor()
    asked = [f"Q{i:02d}" for i in range(12)]
    answered: list = []
    per_request: list = []
    first_burst_queued = threading.Event()
    engine_in_turn = threading.Event()
    done = threading.Event()

    def _gen(
        contexto,
        source="direct",
        *,
        commit_history=True,
        log_prefix="LLM",
        history_text=None,
        watchdog_timeout=None,
    ):
        found = re.findall(r"\bQ\d\d\b", contexto)
        answered.extend(found)
        per_request.append(len(found))
        engine_in_turn.set()
        return "respuesta"

    motor._generar_dialogo = _gen
    motor._hablar = lambda *a, **kw: None  # keep the drain loop running

    def _produce():
        for payload in asked[:6]:
            motor.enqueue(payload, priority=1, source="direct")
        first_burst_queued.set()
        # The rest arrive while the engine is provably mid-drain.
        assert engine_in_turn.wait(10.0), "the engine never started a turn"
        for payload in asked[6:]:
            motor.enqueue(payload, priority=1, source="direct")
        done.set()

    def _consume():
        assert first_burst_queued.wait(10.0)
        while not (done.is_set() and not motor._priority_queue):
            motor._process_priority_queue()

    producer = threading.Thread(target=_produce, daemon=True)
    engine = threading.Thread(target=_consume, daemon=True)
    producer.start()
    engine.start()
    producer.join(15.0)
    engine.join(15.0)

    assert not producer.is_alive() and not engine.is_alive()
    assert max(per_request) > 1, "the concurrent run never actually bundled"
    assert sorted(answered) == asked, "every question exactly once — no loss, no duplication"
