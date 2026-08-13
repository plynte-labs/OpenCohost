"""History gate for stream turns (tauri_stream_chat_20260812 §3.2 phase 2).

Owner decision Q3: a `source="chat"` (stream/viewer) turn keeps ONLY Kira's own
last replies — the assistant slots of `historial` — and drops the user slots.
Measured basis (logs/opencohost_20260812_194539.log, 42-turn Twitch session):
every chat prompt dragged ~3 user slots of 800-char template boilerplate
(the stored user slot is `_sanitize_history_context`'s 800-char cut of the
~1200-char chat task block, so it carries zero viewer content) plus mixed-in
agenda monologues that made chat replies continue the agenda topic.

These tests assert on the BUILT messages (the exact list handed to
ollama.chat), not on frozenset membership — the sibling gates' comments warn
that membership without a wired site leaves the path dead.
"""
import queue

import pytest

from opencohost.core import llm_engine as engine_mod
from opencohost.core.llm_engine import MotorVocalIA


# ── fixtures ────────────────────────────────────────────────────────────────

# A stand-in for what _commit_history really stores as a chat user slot: the
# first 800 chars of the RF3 task template (pure instruction boilerplate —
# the chat fence and all viewer content fall past the 800-char cut).
_CHAT_BOILERPLATE = ("TAREA: responde al aire como Kira, co-host del stream. " * 20)[:800]

_AGENDA_SENTINEL = "[agenda segura: prompt interno omitido]"
_AGENDA_MONOLOGUE = (
    "Hoy el tema es la arquitectura del proyecto y por que las decisiones "
    "tempranas condicionan todo lo que viene despues. " * 8
)

_DIRECT_QUESTION = "que opinas del rework de la interfaz del stream"
_DIRECT_REPLY = "El rework tiene mas esquinas nuevas que ideas nuevas, pero funciona."
_AGENDA_REPLY = _AGENDA_MONOLOGUE.strip()
_CHAT_REPLY = "Ese chat descubre el agua tibia y lo celebra como un hallazgo historico."


def _bare_motor() -> MotorVocalIA:
    # Hermetic (pattern from test_owner_question_bundling._bare_motor): pin the
    # model and seed both caches so nothing reaches the live ollama.show probe.
    motor = MotorVocalIA(queue.Queue(), lambda event: None)
    motor.current_model = "llama3"
    motor._reasoning_model_cache["llama3"] = False
    motor._model_ctx_limit = {"llama3": 8192}
    motor._resolve_chat_watchdog_timeout = lambda *a, **kw: 30.0
    return motor


def _seed_mixed_history(motor: MotorVocalIA) -> None:
    """Three pairs shaped exactly like _commit_history stores them: a direct
    exchange, an agenda block (sentinel user slot + spoken monologue), and a
    prior chat reaction (boilerplate user slot). Source-mixed on purpose —
    that mix is the observed failure (chat replies continuing agenda topics
    at 22:07:04 / 22:13:52 in the 2026-08-12 log)."""
    pairs = [
        ("direct", _DIRECT_QUESTION, _DIRECT_REPLY),
        ("kira-agenda", _AGENDA_SENTINEL, _AGENDA_REPLY),
        ("chat", _CHAT_BOILERPLATE, _CHAT_REPLY),
    ]
    for source, user_content, asst_content in pairs:
        motor.historial.append(
            {"role": "user", "content": user_content, "source": source, "private": False}
        )
        motor.historial.append(
            {"role": "assistant", "content": asst_content, "source": source, "private": False}
        )


def _build(motor: MotorVocalIA, source: str, contexto: str = "nuevo turno de prueba"):
    return motor._build_generation_request(
        contexto,
        source,
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        watchdog_timeout=None,
    )


@pytest.fixture(autouse=True)
def _hermetic_enrichments(monkeypatch):
    # Keep the enrichment gates quiet so the messages list is exactly
    # system-folded prompt + history: no personalization/memorias disk reads.
    monkeypatch.setattr(engine_mod, "PERSONALIZATION_ENABLED", False)
    monkeypatch.setattr(engine_mod, "MEMORIAS_ENABLED", False)


# ── the gate ────────────────────────────────────────────────────────────────


def test_chat_turn_drops_history_user_slots():
    """The core reduction: a stream turn's prompt must not carry the stored
    user slots — 800 chars each of template boilerplate (direct/chat) or the
    agenda sentinel — only Kira's own replies."""
    motor = _bare_motor()
    _seed_mixed_history(motor)

    setup = _build(motor, "chat")

    non_final_user_msgs = [m for m in setup.messages[:-1] if m["role"] == "user"]
    assert non_final_user_msgs == [], (
        "chat prompt still carries history user slots: "
        f"{[m['content'][:60] for m in non_final_user_msgs]}"
    )
    blob = "\n".join(m["content"] for m in setup.messages)
    assert _CHAT_BOILERPLATE not in blob
    assert _AGENDA_SENTINEL not in blob
    assert _DIRECT_QUESTION not in blob


def test_chat_turn_keeps_assistant_slots_in_order():
    """Owner Q3 is NOT 'no history': Kira's own last replies stay, in order —
    they are what the FIX 2 repetition guard and the model's own
    don't-repeat-yourself signal live on."""
    motor = _bare_motor()
    _seed_mixed_history(motor)

    setup = _build(motor, "chat")

    asst_contents = [m["content"] for m in setup.messages if m["role"] == "assistant"]
    assert asst_contents == [_DIRECT_REPLY, _AGENDA_REPLY, _CHAT_REPLY]
    # Final message is still the current turn's prompt.
    assert setup.messages[-1]["role"] == "user"
    assert "nuevo turno de prueba" in setup.messages[-1]["content"]


def test_direct_turn_keeps_full_history():
    """Regression pin: the gate must not leak onto owner turns — a typed
    follow-up ('y eso por que?') is meaningless without the user slots."""
    motor = _bare_motor()
    _seed_mixed_history(motor)

    setup = _build(motor, "direct")

    blob = "\n".join(m["content"] for m in setup.messages)
    assert _DIRECT_QUESTION in blob
    assert _CHAT_BOILERPLATE in blob
    assert [m["content"] for m in setup.messages if m["role"] == "assistant"] == [
        _DIRECT_REPLY, _AGENDA_REPLY, _CHAT_REPLY,
    ]


def test_agenda_turn_keeps_full_history():
    """Scope pin: the owner ruled on stream turns only. Agenda prompts keep
    the full (still source-mixed) history until an owner decision says
    otherwise — this test exists so widening the gate is a deliberate act."""
    motor = _bare_motor()
    _seed_mixed_history(motor)

    setup = _build(motor, "kira-agenda")

    blob = "\n".join(m["content"] for m in setup.messages)
    assert _AGENDA_SENTINEL in blob
    assert _DIRECT_QUESTION in blob
    assert _CHAT_BOILERPLATE in blob


# ── the FIX 2 coupling ──────────────────────────────────────────────────────


def test_repetition_guard_still_fed_for_chat(monkeypatch):
    """The naive fix (filtering history_snapshot itself) blinds FIX 2, which
    derives recent_outputs from the snapshot's assistant entries. This drives
    the real _finalize_generation with a verbatim repeat of a stored Kira line
    and requires (a) the guard SAW that line and (b) the repeat was suppressed.
    Fails against a snapshot-zeroing implementation; passes only while the
    guard keeps its inputs."""
    motor = _bare_motor()
    _seed_mixed_history(motor)

    seen: dict = {}
    real_detect = engine_mod.detect_repetition

    def spy(candidate, recent, **kw):
        seen["recent"] = list(recent)
        return real_detect(candidate, recent, **kw)

    monkeypatch.setattr(engine_mod, "detect_repetition", spy)

    setup = _build(motor, "chat")
    outcome = engine_mod._GenerationAttemptOutcome(raw_content=_CHAT_REPLY, respuesta=None)
    result = motor._finalize_generation(
        setup,
        outcome,
        "nuevo turno de prueba",
        source="chat",
        commit_history=False,
        log_prefix="LLM",
        history_text=None,
        is_local=True,
        provider_cfg={},
        request_model="llama3",
    )

    assert _CHAT_REPLY in seen.get("recent", []), "guard lost its assistant history"
    assert result != _CHAT_REPLY, "verbatim chat repeat was not suppressed"


# ── the out-of-engine reader ────────────────────────────────────────────────


def test_recent_kira_lines_reads_only_assistant_slots():
    """chat_reaction._recent_kira_lines reads motor.historial from OUTSIDE the
    engine gate. It already implements the owner's Q3 view (assistant slots
    only) — this pins that, so a later 'unification' cannot start leaking user
    slots (owner text, agenda sentinels) into the anti-repeat block."""
    from opencohost.smart_aggregator.chat_reaction import ChatReactionCore

    motor = _bare_motor()
    _seed_mixed_history(motor)
    core = ChatReactionCore(motor)

    block = core._recent_kira_lines(3)

    for reply in (_DIRECT_REPLY, _AGENDA_REPLY, _CHAT_REPLY):
        assert reply[:120] in block
    assert _DIRECT_QUESTION not in block
    assert _AGENDA_SENTINEL not in block
    assert _CHAT_BOILERPLATE[:120] not in block
