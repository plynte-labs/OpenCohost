"""WU1: EngineHost.start() must wire the editorial cue-card direct provider.

Reproduces the reported bug — the FastAPI EngineHost never sets
`motor.direct_editorial_context_provider`, so an armed cue card never injects
on a typed turn in the Tauri app (the gate at llm_engine.py:1625 reads a
provider that stays None). See design.md D1/D4 and tasks.md WU1.

WU1 covers design test-plan items (a), (d), (e):
  (a) after start(), motor.direct_editorial_context_provider is the bridge's
      resolve_direct_context bound method
  (d) agenda-construction failure still wires the store-only direct path
      (agenda=None: provider set, recorder NOT wired)
  (e) a forced bridge-wiring failure leaves EngineHost functional (start()
      completes, editorial_bridge is None, provider never set)

WU2 covers design test-plan items (b), (c):
  (b) a seeded armed card is resolved by the wired direct provider when a typed
      turn hits the same call path the engine gate uses
  (c) with agenda present, the wired agenda_output_recorder records the accepted
      output AND flips the linked/consumed editorial card to USED
"""

from queue import Queue
from unittest.mock import MagicMock

import opencohost.api.engine_host as engine_host_mod
from opencohost.core.editorial.editorial_cards import (
    EditorialCard,
    EditorialCardStatus,
    EditorialCardStore,
)


def _fake_motor():
    """Mirror tests/test_engine_host_agenda_load.py::_fake_motor.

    direct_editorial_context_provider and agenda_output_recorder default to
    None exactly like the real MotorVocalIA (llm_engine.py:423/426), so an
    assertion of "still None" means the wiring never touched it.
    """
    motor = MagicMock()
    motor.command_queue = Queue()
    motor.current_model = None
    motor.is_processing = False
    motor.is_speaking = False
    motor.direct_editorial_context_provider = None
    motor.direct_editorial_usage_recorder = None
    motor.agenda_output_recorder = None
    return motor


def _base_monkeypatch(monkeypatch, motor, db_path):
    monkeypatch.setattr(engine_host_mod, "MotorVocalIA", lambda *a, **kw: motor)
    monkeypatch.setattr(engine_host_mod, "HealthMonitor", lambda: MagicMock())
    monkeypatch.setattr(engine_host_mod, "ollama", MagicMock())
    monkeypatch.setattr(engine_host_mod, "EDITORIAL_CARDS_DB", str(db_path))


def test_engine_host_start_wires_direct_editorial_provider(tmp_path, monkeypatch):
    # (a) After start(), the motor's direct provider is the bridge's bound
    # resolve_direct_context — not a lambda, not left None.
    motor = _fake_motor()
    _base_monkeypatch(monkeypatch, motor, tmp_path / "cards.db")
    host = engine_host_mod.EngineHost(lock_path=str(tmp_path / "engine.lock"))
    try:
        host.start()
        assert host.editorial_bridge is not None
        provider = host.motor.direct_editorial_context_provider
        bound = host.editorial_bridge.resolve_direct_context
        assert provider.__func__ is bound.__func__
        assert provider.__self__ is host.editorial_bridge
    finally:
        host.stop()


def test_engine_host_start_wires_direct_editorial_usage_recorder(tmp_path, monkeypatch):
    # WU4/D2: after start(), the motor's direct USED trigger is the bridge's
    # bound commit_direct_injection — the seam the engine fires once after a
    # successful direct generation that injected a card block.
    motor = _fake_motor()
    _base_monkeypatch(monkeypatch, motor, tmp_path / "cards.db")
    host = engine_host_mod.EngineHost(lock_path=str(tmp_path / "engine.lock"))
    try:
        host.start()
        assert host.editorial_bridge is not None
        recorder = host.motor.direct_editorial_usage_recorder
        bound = host.editorial_bridge.commit_direct_injection
        assert recorder.__func__ is bound.__func__
        assert recorder.__self__ is host.editorial_bridge
    finally:
        host.stop()


def test_engine_host_start_wires_direct_provider_when_agenda_construction_fails(tmp_path, monkeypatch):
    # (d) Agenda construction fails -> host.agenda stays None, but the
    # store-only direct path must still wire. register_provider/recorder
    # (both touch the controller) must be skipped: agenda_output_recorder
    # stays None (also a forward pin for WU2).
    motor = _fake_motor()
    _base_monkeypatch(monkeypatch, motor, tmp_path / "cards.db")

    def _boom(*_a, **_kw):
        raise RuntimeError("agenda construction failed")

    monkeypatch.setattr(engine_host_mod, "KiraAgendaController", _boom)
    host = engine_host_mod.EngineHost(lock_path=str(tmp_path / "engine.lock"))
    try:
        host.start()
        assert host.agenda is None
        provider = host.motor.direct_editorial_context_provider
        assert provider is not None
        assert callable(provider)
        # Recorder wiring touches the controller — must not run with agenda=None.
        assert host.motor.agenda_output_recorder is None
    finally:
        host.stop()


def test_engine_host_start_survives_editorial_bridge_wiring_failure(tmp_path, monkeypatch):
    # (e) A wiring failure must not brick the host (D4). start() completes,
    # editorial_bridge is None, and the provider was never set to a bridge
    # method (status quo — stays None).
    motor = _fake_motor()
    _base_monkeypatch(monkeypatch, motor, tmp_path / "cards.db")

    def _boom(*_a, **_kw):
        raise RuntimeError("store construction failed")

    monkeypatch.setattr(engine_host_mod, "EditorialCardStore", _boom)
    host = engine_host_mod.EngineHost(lock_path=str(tmp_path / "engine.lock"))
    try:
        host.start()  # must not raise
        assert host.editorial_bridge is None
        assert host.motor.direct_editorial_context_provider is None
    finally:
        host.stop()


# ---------------------------------------------------------------------------
# WU2 helpers + tests (design test-plan items b, c)
# ---------------------------------------------------------------------------

def _seed_armed_card(db_path, *, topic, summary, streamer_take, triggers, single_use: bool = False):
    """Seed one ARMED card into the tmp EDITORIAL_CARDS_DB before start().

    Returns (store, refreshed_card). The store is a separate instance from the
    bridge's own — same file, connection-per-call SQLite, so committed writes
    from the bridge's store are visible here. ``single_use`` opts the seeded
    card into the retire-on-first-use path (D7 pin); default reusable (D1).
    """
    store = EditorialCardStore(str(db_path))
    card = store.upsert(
        EditorialCard(
            topic=topic,
            summary=summary,
            streamer_take=streamer_take,
            triggers=triggers,
            single_use=single_use,
        )
    )
    store.arm(card.id)
    return store, store.get(card.id)


def test_engine_host_typed_turn_injects_armed_editorial_card(tmp_path, monkeypatch):
    # (b) End-to-end: an armed card seeded into the tmp EDITORIAL_CARDS_DB is
    # resolved by the wired direct provider when a typed turn hits the same call
    # path the engine gate uses (motor.direct_editorial_context_provider(query)).
    db_path = tmp_path / "cards.db"
    _seed_armed_card(
        db_path,
        topic="GTA delay confirmed",
        summary="Rockstar pushed the release window again.",
        streamer_take="Quiero debatir si el retraso salva el hype pay-to-win.",
        triggers=["gta", "delay"],
    )
    motor = _fake_motor()
    _base_monkeypatch(monkeypatch, motor, db_path)
    host = engine_host_mod.EngineHost(lock_path=str(tmp_path / "engine.lock"))
    try:
        host.start()
        # Deterministic: silence the background driver so it can't mutate card
        # status (auto-attach) mid-assertion.
        if host._agenda_driver is not None:
            host._agenda_driver.stop()

        provider = host.motor.direct_editorial_context_provider
        block = provider("hey kira what do you think about the gta delay")

        assert block is not None
        assert "<editorial_context>" in block
        assert "pay-to-win" in block
    finally:
        host.stop()


def test_engine_host_recorder_marks_card_used_after_accepted_output(tmp_path, monkeypatch):
    # (c) With agenda present, the recorder wired in the `if self.agenda is not
    # None:` branch records the accepted output AND flips the linked/consumed
    # editorial card to USED (bridge.mark_used_after_successful_generation()).
    db_path = tmp_path / "cards.db"
    store, card = _seed_armed_card(
        db_path,
        topic="Dificultad del juego",
        summary="La comunidad discute si el juego se volvió más fácil.",
        streamer_take="Quiero llevarlo a diseño y accesibilidad.",
        triggers=["dificultad"],
        single_use=True,  # D7 pin: only a single_use card retires to USED here
    )
    motor = _fake_motor()
    _base_monkeypatch(monkeypatch, motor, db_path)
    host = engine_host_mod.EngineHost(lock_path=str(tmp_path / "engine.lock"))
    try:
        host.start()
        assert host.agenda is not None
        # Deterministic: stop the background driver so it cannot advance state.
        host._agenda_driver.stop()
        controller = host.agenda
        bridge = host.editorial_bridge

        topic = controller.add_topic("Dificultad del juego", approved=True)
        controller.queue_topic(topic.id)
        assert bridge.link_card_to_topic(topic.id, card.id) is True
        # Put the controller in the post-generation state mark_used expects.
        controller.active_topic = topic
        topic.editorial_card_consumed = True

        recorder = host.motor.agenda_output_recorder
        assert callable(recorder)
        recorder("Respuesta aceptada de Kira")

        # record_accepted_output ran (effect observed — it lowercases + stores) ...
        assert controller.last_outputs
        assert "respuesta aceptada" in controller.last_outputs[-1]
        # ... and the linked card flipped to USED.
        assert store.get(card.id).status is EditorialCardStatus.USED
    finally:
        host.stop()


def test_engine_host_recorder_keeps_reusable_card_armed_after_accepted_output(tmp_path, monkeypatch):
    # D3/D7 sibling to (c): a reusable card (single_use=False, the default) run
    # through the same recorder path stays ARMED with last_injected_at set and
    # the topic link detached — instead of transitioning to USED.
    db_path = tmp_path / "cards.db"
    store, card = _seed_armed_card(
        db_path,
        topic="Balance del parche",
        summary="El parche ajusta balance y progresión.",
        streamer_take="Quiero compararlo con lo que pedía la comunidad.",
        triggers=["balance"],
        # single_use defaults to False -> reusable
    )
    motor = _fake_motor()
    _base_monkeypatch(monkeypatch, motor, db_path)
    host = engine_host_mod.EngineHost(lock_path=str(tmp_path / "engine.lock"))
    try:
        host.start()
        assert host.agenda is not None
        host._agenda_driver.stop()
        controller = host.agenda
        bridge = host.editorial_bridge

        topic = controller.add_topic("Balance del parche", approved=True)
        controller.queue_topic(topic.id)
        assert bridge.link_card_to_topic(topic.id, card.id) is True
        controller.active_topic = topic
        topic.editorial_card_consumed = True

        recorder = host.motor.agenda_output_recorder
        assert callable(recorder)
        recorder("Respuesta aceptada de Kira")

        assert controller.last_outputs
        assert "respuesta aceptada" in controller.last_outputs[-1]
        # Reusable card stays eligible (D1/D3): ARMED, injected, link detached.
        refreshed = store.get(card.id)
        assert refreshed.status is EditorialCardStatus.ARMED
        assert refreshed.last_injected_at is not None
        assert topic.editorial_card_id is None
    finally:
        host.stop()


# ---------------------------------------------------------------------------
# Finding 5: per-card-mode direct-turn integration — drive provider+recorder
# through the wired EngineHost (same seam the real motor fires) and assert
# the FINAL DATABASE STATE via store.get, for both card modes.
# ---------------------------------------------------------------------------

def test_engine_host_direct_turn_reusable_card_final_state_stays_armed(tmp_path, monkeypatch):
    """Reusable (single_use=False, default): after provider + usage-recorder
    fire for a direct turn, the card's final DB state is ARMED with
    last_injected_at set."""
    db_path = tmp_path / "cards.db"
    store, card = _seed_armed_card(
        db_path,
        topic="GTA delay confirmed",
        summary="Rockstar pushed the release window again.",
        streamer_take="Quiero debatir si el retraso salva el hype pay-to-win.",
        triggers=["gta", "delay"],
        # single_use defaults to False -> reusable
    )
    motor = _fake_motor()
    _base_monkeypatch(monkeypatch, motor, db_path)
    host = engine_host_mod.EngineHost(lock_path=str(tmp_path / "engine.lock"))
    try:
        host.start()
        if host._agenda_driver is not None:
            host._agenda_driver.stop()

        provider = host.motor.direct_editorial_context_provider
        recorder = host.motor.direct_editorial_usage_recorder
        block = provider("hey kira what do you think about the gta delay")
        assert block is not None
        recorder()  # mirrors the engine's post-generation commit (D2)

        refreshed = store.get(card.id)
        assert refreshed.status is EditorialCardStatus.ARMED
        assert refreshed.last_injected_at is not None
    finally:
        host.stop()


def test_engine_host_direct_turn_single_use_card_final_state_flips_used(tmp_path, monkeypatch):
    """single_use=True: after provider + usage-recorder fire for a direct
    turn, the card's final DB state is USED."""
    db_path = tmp_path / "cards.db"
    store, card = _seed_armed_card(
        db_path,
        topic="GTA delay confirmed",
        summary="Rockstar pushed the release window again.",
        streamer_take="Quiero debatir si el retraso salva el hype pay-to-win.",
        triggers=["gta", "delay"],
        single_use=True,
    )
    motor = _fake_motor()
    _base_monkeypatch(monkeypatch, motor, db_path)
    host = engine_host_mod.EngineHost(lock_path=str(tmp_path / "engine.lock"))
    try:
        host.start()
        if host._agenda_driver is not None:
            host._agenda_driver.stop()

        provider = host.motor.direct_editorial_context_provider
        recorder = host.motor.direct_editorial_usage_recorder
        block = provider("hey kira what do you think about the gta delay")
        assert block is not None
        recorder()

        refreshed = store.get(card.id)
        assert refreshed.status is EditorialCardStatus.USED
    finally:
        host.stop()
