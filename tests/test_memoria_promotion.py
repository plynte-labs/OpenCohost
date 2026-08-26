"""Strict-TDD tests for the memoria draft-promotion sweep
(memory_promotion_20260725, WU3 + WU4).

ONE LLM call per eligible maintenance sweep judges the oldest unjudged drafts, rewrites the
survivors so they stand alone, and promotes them. Every model call here goes
through the SAME injectable seam the Topic Scout tests use
(`_ollama_chat_with_watchdog(chat_callable=...)`), threaded in as
`promote_pending_drafts(chat_callable=...)`: no network, no Ollama, no
transport monkeypatching.

Each verification below is written so that DELETING the feature makes it fail —
no hasattr-guard assertions over a dead call site.
"""

from __future__ import annotations

import json
import logging
import queue
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

import opencohost.core.llm_engine as llm_engine
from opencohost.core.llm_engine import MotorVocalIA
from opencohost.core.memory.memoria_store import MemoriaStore, build_signature
from opencohost.core.memory.promotion_backoff_store import (
    PromotionBackoffStore,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_motor(monkeypatch, tmp_path, *, profile_id="profile-1"):
    """A motor wired for promotion: memorias on, store at tmp_path, model resident.

    `_provider_config` is pinned LOCAL explicitly — MotorVocalIA's constructor
    calls the real `load_provider_config()`, so without this every gate test
    would silently depend on whichever provider the machine running the suite
    happens to have active.

    The fake local ``Client.ps()`` reports the motor's live `_loaded_model`, so
    every judge test proves residency without touching a real Ollama daemon.
    """
    monkeypatch.setattr(llm_engine, "MEMORIAS_ENABLED", True)
    monkeypatch.setattr(llm_engine, "MEMORIAS_DB", str(tmp_path / "memorias.db"))
    motor = MotorVocalIA(queue.Queue(), lambda e: None)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor._provider_config = {"active_provider": "local"}
    motor._cloud_fallback_active = False
    motor._current_profile_id = profile_id
    motor._loaded_model = "m"
    motor.current_model = "m"
    motor._last_known_good_model = "m"
    resident_client = MagicMock()
    resident_client.ps.side_effect = lambda: SimpleNamespace(
        models=[SimpleNamespace(model=motor._loaded_model)]
    )
    motor.ollama.Client.return_value = resident_client
    motor._pending_model_switch = None
    motor._awaiting_first_success_after_switch = False
    motor._check_capabilities_reasoning = lambda model: False
    motor._promotion_wall_clock = lambda: 100
    return motor


def _go_cloud(motor, *, model="glm-5.2"):
    """Activate cloud foreground while leaving an already-resident local model.

    This represents a provider switch after local use. Promotion may use only
    the exact model Ollama still reports resident; it never falls back to a
    configured-but-unloaded model and never follows the cloud profile.
    """
    motor._provider_config = {
        "active_provider": "nvidia_nim",
        "profiles": {"nvidia_nim": {"base_url": "https://x/v1", "model": model}},
    }
    return motor


def _store(tmp_path) -> MemoriaStore:
    return MemoriaStore(tmp_path / "memorias.db")


def _row(tmp_path, row_id):
    with sqlite3.connect(str(tmp_path / "memorias.db")) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM memorias WHERE id = ?", (row_id,)).fetchone()


def _attempt(tmp_path, row_id):
    with sqlite3.connect(str(tmp_path / "memorias.db")) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM memoria_promotion_attempts WHERE memoria_id = ?",
            (row_id,),
        ).fetchone()


def _backoff_path(tmp_path):
    return tmp_path / "memoria_promotion_backoff.db"


def _backoff_state(tmp_path, profile_id="profile-1"):
    return PromotionBackoffStore(_backoff_path(tmp_path)).get_state(profile_id)


def _seed_draft(store, profile_id, key_suffix, content, *, private=False):
    """One draft through the real upsert (revision 1), returning its row id."""
    row_id = store.upsert_draft(
        profile_id, f"{profile_id}|{key_suffix}", f"titulo {key_suffix}", content,
        signature=f"firma {key_suffix}",
    )
    if private:
        with sqlite3.connect(str(store.db_path)) as conn:
            conn.execute("UPDATE memorias SET private = 1 WHERE id = ?", (row_id,))
    return row_id


class _Recorder:
    """A chat stub: records every kwargs it saw and returns a canned reply."""

    def __init__(self, reply):
        self.calls = []
        self._reply = reply

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        reply = self._reply(**kwargs) if callable(self._reply) else self._reply
        if isinstance(reply, BaseException):
            raise reply
        return {"message": {"content": reply}}

    @property
    def prompt(self) -> str:
        return self.calls[-1]["messages"][0]["content"]


def _decisions(*entries) -> str:
    return json.dumps({"decisions": list(entries)})


class _StatusError(RuntimeError):
    def __init__(self, status_code: int):
        super().__init__("bounded test status")
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Gates — the sweep must never spend a token when it cannot act
# ---------------------------------------------------------------------------

def test_zero_drafts_never_calls_the_model(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path)
    stub = _Recorder(_decisions())

    counts = motor.promote_pending_drafts(chat_callable=stub)

    assert stub.calls == []
    assert counts["considered"] == 0
    assert counts["kept"] == 0
    assert counts["rejected"] == 0
    # NOT a gate: the sweep reached the store and found nothing.
    assert counts["skipped"] == ""


@pytest.mark.parametrize("attr,value", [
    ("_pending_model_switch", "otro-modelo"),
    ("_awaiting_first_success_after_switch", True),
    ("_current_profile_id", None),
])
def test_engine_gates_skip_the_call_entirely(monkeypatch, tmp_path, attr, value):
    """`_make_motor` pins `_provider_config` local precisely so this
    parametrisation cannot silently become "the feature is dead on cloud".

    Residency has its own fail-closed tests below.
    """
    motor = _make_motor(monkeypatch, tmp_path)
    row_id = _seed_draft(
        _store(tmp_path), "profile-1", "k1",
        "el streamer juega Silksong los martes",
    )
    setattr(motor, attr, value)
    stub = _Recorder(_decisions({"i": 1, "keep": True, "text": "x"}))

    counts = motor.promote_pending_drafts(chat_callable=stub)

    assert stub.calls == []
    assert counts["considered"] == 0
    # Naming the gate stops this from also passing against a sweep that simply
    # does nothing.
    assert counts["skipped"] == {
        "_pending_model_switch": "model_switch_pending",
        "_awaiting_first_success_after_switch": "model_switch_pending",
        "_current_profile_id": "no_profile",
    }[attr]
    assert _attempt(tmp_path, row_id) is None
    assert _backoff_state(tmp_path) is None


def test_no_loaded_model_gates_without_a_residency_probe(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path)
    motor._loaded_model = None
    row_id = _seed_draft(
        _store(tmp_path), "profile-1", "k1",
        "el streamer juega Silksong los martes",
    )
    stub = _Recorder(_decisions({"i": 1, "keep": True, "text": "x"}))

    counts = motor.promote_pending_drafts(chat_callable=stub)

    assert stub.calls == []
    assert motor.ollama.Client.call_count == 0
    assert counts["skipped"] == "model_not_loaded"
    assert _attempt(tmp_path, row_id) is None
    assert _backoff_state(tmp_path) is None


@pytest.mark.parametrize(
    "response",
    [
        SimpleNamespace(models=[]),
        SimpleNamespace(models=[SimpleNamespace(model="other")]),
        SimpleNamespace(models=[{"model": "m"}]),
        SimpleNamespace(),
    ],
    ids=["absent", "mismatch", "dict-is-malformed", "missing-models"],
)
def test_unconfirmed_residency_never_calls_the_judge(
    monkeypatch, tmp_path, response
):
    motor = _make_motor(monkeypatch, tmp_path)
    client = motor.ollama.Client.return_value
    client.ps.side_effect = None
    client.ps.return_value = response
    row_id = _seed_draft(
        _store(tmp_path), "profile-1", "k1",
        "el streamer juega Silksong los martes",
    )
    stub = _Recorder(_decisions({"i": 1, "keep": True, "text": "x"}))

    counts = motor.promote_pending_drafts(chat_callable=stub)

    assert stub.calls == []
    assert counts["considered"] == 0
    assert counts["skipped"] == "model_not_resident"


def test_failed_residency_probe_never_calls_the_judge(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path)
    motor.ollama.Client.return_value.ps.side_effect = TimeoutError(
        "local ps timeout"
    )
    row_id = _seed_draft(
        _store(tmp_path), "profile-1", "k1",
        "el streamer juega Silksong los martes",
    )
    stub = _Recorder(_decisions({"i": 1, "keep": True, "text": "x"}))

    counts = motor.promote_pending_drafts(chat_callable=stub)

    assert stub.calls == []
    assert counts["skipped"] == "model_not_resident"


def test_exact_resident_model_allows_judge_with_short_local_probe(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path)
    _seed_draft(_store(tmp_path), "profile-1", "k1", "el streamer juega Silksong los martes")
    stub = _Recorder(_decisions({"i": 1, "keep": True, "text": "x"}))

    counts = motor.promote_pending_drafts(chat_callable=stub)

    motor.ollama.Client.assert_called_once_with(
        timeout=llm_engine._PROMOTION_RESIDENCY_TIMEOUT_SECONDS
    )
    motor.ollama.Client.return_value.ps.assert_called_once_with()
    assert len(stub.calls) == 1
    assert stub.calls[0]["model"] == "m"
    assert counts["skipped"] == ""


def test_cloud_provider_without_a_resident_local_model_never_calls_judge(monkeypatch, tmp_path):
    motor = _go_cloud(_make_motor(monkeypatch, tmp_path))
    motor._loaded_model = None
    store = _store(tmp_path)
    row_id = _seed_draft(store, "profile-1", "k1", "streamer: uso GLM en la nube para el juez")
    judged = "El streamer corre el juez de memorias localmente."
    stub = _Recorder(_decisions({"i": 1, "keep": True, "text": judged}))

    counts = motor.promote_pending_drafts(chat_callable=stub)

    assert stub.calls == []
    assert motor.ollama.Client.call_count == 0
    assert _row(tmp_path, row_id)["status"] == "draft"
    assert counts["skipped"] == "model_not_loaded"


def test_the_sweep_announces_a_kept_memoria_to_the_owner(monkeypatch, tmp_path):
    """The owner-facing "Kira guardó una memoria" notice lives HERE, not at
    capture (moved 2026-08-14).

    Capture writes an UNJUDGED draft; this sweep is the first moment anything
    is known to be worth keeping. Announcing at capture told the owner a
    memoria was saved after almost every turn — in their live store 84 of 98
    drafts were later judge-rejected and hidden, so the claim was wrong ~86%
    of the time, separated from the truth by an app restart.

    The event NAME is deliberately unchanged so the Tauri feed needs no edit.
    """
    motor = _make_motor(monkeypatch, tmp_path)
    events: list = []
    motor.ui_callback = lambda status, *a, **k: events.append(status)
    _seed_draft(_store(tmp_path), "profile-1", "k1", "streamer: prefiere synthwave calmo")
    stub = _Recorder(_decisions(
        {"i": 1, "keep": True, "text": "El streamer prefiere synthwave calmo."}
    ))

    counts = motor.promote_pending_drafts(chat_callable=stub)

    assert counts["kept"] == 1
    assert events.count("memoria_captured") == 1


def test_the_sweep_hook_carries_the_kept_count(monkeypatch, tmp_path):
    """Adversarial review 2026-08-14: a sweep keeping 20 memorias rendered the
    same singular "Kira guardó una memoria" as one keeping 1, because
    `ui_callback` reaches `EngineHost._dispatch_motor_event`, which drops extra
    args by design (CTk's concrete callback takes exactly one).

    So the count rides the established dedicated-hook path instead — the same
    shape `on_ctx_pressure_high` and `on_cloud_probe_scheduled` already use.
    The plain event still fires for any surface without the hook.
    """
    motor = _make_motor(monkeypatch, tmp_path)
    payloads: list = []
    motor.on_memoria_promoted = payloads.append
    store = _store(tmp_path)
    for i in range(3):
        _seed_draft(store, "profile-1", f"k{i}", f"streamer: dato numero {i} sobre synthwave")
    stub = _Recorder(_decisions(
        {"i": 1, "keep": True, "text": "El streamer escucha synthwave uno."},
        {"i": 2, "keep": True, "text": "El streamer escucha synthwave dos."},
        {"i": 3, "keep": True, "text": "El streamer escucha synthwave tres."},
    ))

    counts = motor.promote_pending_drafts(chat_callable=stub)

    assert counts["kept"] == 3
    assert payloads == [{"kept": 3}], "one call per sweep, carrying the real count"


def test_a_sweep_that_keeps_nothing_stays_silent(monkeypatch, tmp_path):
    """The other half of the contract: no keep, no notice. Without this the
    move would just relocate the same lie — announcing a sweep that threw
    everything away is no more honest than announcing an unjudged draft."""
    motor = _make_motor(monkeypatch, tmp_path)
    events: list = []
    motor.ui_callback = lambda status, *a, **k: events.append(status)
    _seed_draft(_store(tmp_path), "profile-1", "k1", "streamer: dijo algo vago")
    stub = _Recorder(_decisions({"i": 1, "keep": False, "reason": "vague"}))

    counts = motor.promote_pending_drafts(chat_callable=stub)

    assert counts["kept"] == 0
    assert "memoria_captured" not in events


def test_qwen3_uses_boolean_think_false_on_the_resolved_local_model(monkeypatch, tmp_path):
    """The maintenance controls apply to the local model actually requested,
    even while the foreground provider is cloud."""
    motor = _go_cloud(_make_motor(monkeypatch, tmp_path), model="glm-5.2")
    motor._loaded_model = "qwen3:8b"
    _seed_draft(_store(tmp_path), "profile-1", "k1", "streamer: uso Qwen3 en local para juzgar")
    stub = _Recorder(_decisions())

    motor.promote_pending_drafts(chat_callable=stub)

    assert len(stub.calls) == 1
    assert stub.calls[0]["model"] == "qwen3:8b"
    assert stub.calls[0].get("think") is False
    assert stub.calls[0]["options"]["num_predict"] == 512


def test_memorias_disabled_skips_the_call(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path)
    _seed_draft(_store(tmp_path), "profile-1", "k1", "el streamer juega Silksong los martes")
    monkeypatch.setattr(llm_engine, "MEMORIAS_ENABLED", False)
    stub = _Recorder(_decisions({"i": 1, "keep": True, "text": "x"}))

    counts = motor.promote_pending_drafts(chat_callable=stub)
    assert counts["considered"] == 0
    assert counts["skipped"] == "memorias_disabled"
    assert stub.calls == []


# ---------------------------------------------------------------------------
# Applying decisions
# ---------------------------------------------------------------------------

def test_confident_keep_writes_judged_text_signature_and_promoted_status(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path)
    store = _store(tmp_path)
    row_id = _seed_draft(store, "profile-1", "k1", "streamer: arreglamos eso ayer. Kira: buenisimo")
    judged = "El streamer arreglo el bug de reconexion de Twitch en el pipeline de audio."
    stub = _Recorder(_decisions({"i": 1, "keep": True, "text": judged}))

    counts = motor.promote_pending_drafts(chat_callable=stub)

    row = _row(tmp_path, row_id)
    assert row["content"] == judged
    assert row["status"] == "promoted"
    assert row["judged_at"] != ""
    # The signature is re-derived from the JUDGED text, not the original pair —
    # otherwise a durable row stays indexed on Kira's speculative words.
    assert row["signature"] == build_signature(judged)
    assert row["signature"] != "firma k1"
    assert counts["kept"] == 1
    assert counts["rejected"] == 0


def test_confident_keep_leaves_updated_at_untouched(monkeypatch, tmp_path):
    """`build_recency_lines` ranks the meta-recall block by `updated_at DESC`.
    A keep write that bumps it stamps the OLDEST drafts (the sweep reads oldest
    first) with launch time, so "de que hablamos la sesion pasada" answers with
    three-week-old rows and excludes last session's actual memories. Mirrors the
    assertion the reject path already carries."""
    motor = _make_motor(monkeypatch, tmp_path)
    store = _store(tmp_path)
    row_id = _seed_draft(store, "profile-1", "k1", "streamer: arreglamos eso ayer. Kira: buenisimo")
    before = _row(tmp_path, row_id)
    judged = "El streamer arreglo el bug de reconexion de Twitch en el pipeline de audio."
    stub = _Recorder(_decisions({"i": 1, "keep": True, "text": judged}))

    motor.promote_pending_drafts(chat_callable=stub)

    row = _row(tmp_path, row_id)
    assert row["content"] == judged
    assert row["updated_at"] == before["updated_at"]


def test_uncertain_keep_writes_the_text_but_stays_a_draft(monkeypatch, tmp_path):
    """Owner decision 4: keep and MARK, never discard. The `draft` badge that
    already ships is the marking, at zero schema cost."""
    motor = _make_motor(monkeypatch, tmp_path)
    store = _store(tmp_path)
    row_id = _seed_draft(store, "profile-1", "k1", "streamer: me gusto lo de Luke Oxide")
    judged = "Al streamer le gusto el trabajo de Luke Oxide."
    stub = _Recorder(_decisions({"i": 1, "keep": True, "text": judged, "uncertain": True}))

    counts = motor.promote_pending_drafts(chat_callable=stub)

    row = _row(tmp_path, row_id)
    assert row["content"] == judged
    assert row["status"] == "draft"
    assert row["judged_at"] != ""
    assert row["inactive"] == 0
    assert counts["reasons"]["uncertain_entity"] == 1


def test_reject_hides_and_stamps_without_disturbing_prune_order(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path)
    store = _store(tmp_path)
    row_id = _seed_draft(store, "profile-1", "k1", "streamer: hubo una correccion algo tecnico")
    before = _row(tmp_path, row_id)
    stub = _Recorder(_decisions({"i": 1, "keep": False, "reason": "vague"}))

    counts = motor.promote_pending_drafts(chat_callable=stub)

    row = _row(tmp_path, row_id)
    assert row["status"] == "draft"
    assert row["inactive"] == 1
    assert row["judged_at"] != ""
    # The prune-order guard: rejecting must not push the row into the keep-window.
    assert row["updated_at"] == before["updated_at"]
    assert row["content"] == before["content"]  # never rewritten, never deleted
    assert counts["rejected"] == 1
    assert counts["reasons"]["vague"] == 1


def test_a_second_sweep_never_re_judges_what_the_first_one_judged(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path)
    store = _store(tmp_path)
    _seed_draft(store, "profile-1", "k1", "streamer: uso Ollama con Qwen3 en local")
    _seed_draft(store, "profile-1", "k2", "streamer: hubo una correccion algo tecnico")
    stub = _Recorder(_decisions(
        {"i": 1, "keep": True, "text": "El streamer corre Qwen3 sobre Ollama en su propia maquina."},
        {"i": 2, "keep": False, "reason": "vague"},
    ))

    motor.promote_pending_drafts(chat_callable=stub)
    assert len(stub.calls) == 1

    second = _Recorder(_decisions())
    counts = motor.promote_pending_drafts(chat_callable=second)
    assert second.calls == []
    assert counts["considered"] == 0
    assert counts["unjudged_remaining"] == 0


def test_reason_counter_reports_exactly_what_the_judge_answered(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path)
    store = _store(tmp_path)
    for i, reason in enumerate(("vague", "speculative", "trivial", "transient", "not_attributable")):
        _seed_draft(store, "profile-1", f"k{i}", f"streamer: contenido descartable numero {i} aqui")
    stub = _Recorder(_decisions(
        {"i": 1, "keep": False, "reason": "vague"},
        {"i": 2, "keep": False, "reason": "speculative"},
        {"i": 3, "keep": False, "reason": "trivial"},
        {"i": 4, "keep": False, "reason": "transient"},
        {"i": 5, "keep": False, "reason": "not_attributable"},
    ))

    counts = motor.promote_pending_drafts(chat_callable=stub)

    assert dict(counts["reasons"]) == {
        "vague": 1, "speculative": 1, "trivial": 1, "transient": 1, "not_attributable": 1,
    }
    assert counts["rejected"] == 5
    assert counts["kept"] == 0


# ---------------------------------------------------------------------------
# Batch shaping — arithmetic dedup, batch cap, ordering, privacy
# ---------------------------------------------------------------------------

def test_promoted_rows_are_never_re_sent_to_the_judge(monkeypatch, tmp_path):
    """The only real "never pay twice" guard: list_unjudged_drafts filters
    status='draft' AND judged_at=''. (The design's separate stable_key dedup
    step is absent on purpose — UNIQUE(profile_id, stable_key) makes a draft
    that collides with a durable row an unreachable state; see
    test_promoted_row_is_upsert_immune in tests/test_memoria_store.py.)"""
    motor = _make_motor(monkeypatch, tmp_path)
    store = _store(tmp_path)
    _seed_draft(store, "profile-1", "k1", "streamer: SENTINELA_VIEJA sobre sintetizadores modulares")
    first = _Recorder(_decisions(
        {"i": 1, "keep": True, "text": "El streamer colecciona sintetizadores modulares vintage."}
    ))
    motor.promote_pending_drafts(chat_callable=first)

    _seed_draft(store, "profile-1", "k2", "streamer: SENTINELA_NUEVA sobre mapas pequenos en Dota")
    second = _Recorder(_decisions())
    counts = motor.promote_pending_drafts(chat_callable=second)

    assert counts["considered"] == 1
    assert "SENTINELA_NUEVA" in second.prompt
    assert "SENTINELA_VIEJA" not in second.prompt
    assert "sintetizadores modulares vintage" not in second.prompt


def test_batch_is_capped_at_eight_oldest_first(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path)
    store = _store(tmp_path)
    for i in range(12):
        _seed_draft(store, "profile-1", f"k{i:03d}", f"streamer: contenido numerado {i:03d} sobre juegos")
    stub = _Recorder(_decisions())

    counts = motor.promote_pending_drafts(chat_callable=stub)

    assert counts["considered"] == llm_engine._PROMOTION_DRAFT_BATCH == 8
    prompt = stub.prompt
    # Oldest first: 000..007 are in the prompt, 008+ are not.
    assert "contenido numerado 000" in prompt
    assert "contenido numerado 007" in prompt
    assert "contenido numerado 008" not in prompt
    assert "contenido numerado 011" not in prompt


def test_batch_stops_before_exceeding_five_thousand_draft_characters(
    monkeypatch, tmp_path,
):
    motor = _make_motor(monkeypatch, tmp_path)
    store = _store(tmp_path)
    contents = [f"DRAFT_{i:02d} " + ("x" * 980) for i in range(6)]
    for i, content in enumerate(contents):
        _seed_draft(store, "profile-1", f"k{i:03d}", content)
    stub = _Recorder(_decisions())

    counts = motor.promote_pending_drafts(chat_callable=stub)

    assert counts["considered"] == 5
    assert llm_engine._PROMOTION_DRAFT_CHARS == 5_000
    assert sum(len(content) for content in contents[:5]) <= llm_engine._PROMOTION_DRAFT_CHARS
    for i in range(5):
        assert f"DRAFT_{i:02d}" in stub.prompt
    assert "DRAFT_05" not in stub.prompt


def test_private_rows_never_reach_the_judge(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path)
    store = _store(tmp_path)
    _seed_draft(store, "profile-1", "pub", "streamer: uso teclado mecanico con switches lineales")
    _seed_draft(store, "profile-1", "sec", "streamer: mi direccion secreta es calle falsa 123", private=True)
    stub = _Recorder(_decisions())

    motor.promote_pending_drafts(chat_callable=stub)

    assert "switches lineales" in stub.prompt
    assert "calle falsa" not in stub.prompt


def test_other_profiles_drafts_are_never_sent(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path)
    store = _store(tmp_path)
    _seed_draft(store, "profile-1", "mine", "streamer: uso teclado mecanico con switches lineales")
    _seed_draft(store, "otro-perfil", "theirs", "streamer: contenido de otro perfil distinto")
    stub = _Recorder(_decisions())

    motor.promote_pending_drafts(chat_callable=stub)

    assert "switches lineales" in stub.prompt
    assert "otro perfil distinto" not in stub.prompt


# ---------------------------------------------------------------------------
# Infrastructure failures: draft untouched, profile retry durable
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("failure", "failure_class"),
    [
        (TimeoutError("bounded watchdog"), "judge_watchdog"),
        (ConnectionError("offline"), "model_transport"),
        (httpx.ConnectError("offline"), "model_transport"),
        (_StatusError(404), "model_unavailable"),
        (_StatusError(503), "model_server_error"),
        (RuntimeError("unexpected"), "unexpected_precommit"),
    ],
)
def test_model_infrastructure_failures_back_off_profile_not_draft(
    monkeypatch, tmp_path, failure, failure_class,
):
    motor = _make_motor(monkeypatch, tmp_path)
    row_id = _seed_draft(
        _store(tmp_path), "profile-1", "infra",
        "streamer: uso Ollama con Qwen3 en local",
    )

    counts = motor.promote_pending_drafts(chat_callable=_Recorder(failure))

    state = _backoff_state(tmp_path)
    assert state.failure_count == 1
    assert state.last_failure_at_s == 100
    assert state.next_attempt_at_s == 160
    assert state.last_failure_class == failure_class
    assert _attempt(tmp_path, row_id) is None
    assert _row(tmp_path, row_id)["judged_at"] == ""
    assert counts["kept"] == counts["rejected"] == 0


@pytest.mark.parametrize("reply", ["", "not json", "{}", "{malformed"])
def test_malformed_top_level_records_protocol_backoff_without_draft_charge(
    monkeypatch, tmp_path, reply,
):
    motor = _make_motor(monkeypatch, tmp_path)
    row_id = _seed_draft(
        _store(tmp_path), "profile-1", "protocol",
        "streamer: uso Ollama con Qwen3 en local",
    )

    motor.promote_pending_drafts(chat_callable=_Recorder(reply))

    assert _backoff_state(tmp_path).last_failure_class == "protocol_malformed"
    assert _attempt(tmp_path, row_id) is None


def test_valid_empty_result_charges_missing_draft_at_exact_five_minutes(
    monkeypatch, tmp_path,
):
    motor = _make_motor(monkeypatch, tmp_path)
    row_id = _seed_draft(
        _store(tmp_path), "profile-1", "missing",
        "streamer: uso Ollama con Qwen3 en local",
    )

    counts = motor.promote_pending_drafts(
        chat_callable=_Recorder(_decisions()),
    )

    attempt = _attempt(tmp_path, row_id)
    assert attempt["attempt_count"] == 1
    assert attempt["last_attempt_at_s"] == 100
    assert attempt["next_attempt_at_s"] == 400
    assert attempt["last_failure_code"] == "missing_decision"
    assert attempt["promotion_state"] == "pending"
    assert _backoff_state(tmp_path) is None
    assert counts["decided"] == 0
    assert counts["reasons"]["missing_decision"] == 1


def test_partial_valid_response_charges_only_the_unresolved_current_index(
    monkeypatch, tmp_path,
):
    motor = _make_motor(monkeypatch, tmp_path)
    store = _store(tmp_path)
    first_id = _seed_draft(
        store, "profile-1", "first",
        "streamer: usa Ollama local para mantener privacidad",
    )
    second_id = _seed_draft(
        store, "profile-1", "second",
        "streamer: prefiere sintetizadores modulares analogicos",
    )
    reply = _decisions(
        {
            "i": 1,
            "keep": True,
            "text": "El streamer usa Ollama local para mantener privacidad.",
        },
        {"i": 2, "keep": "invalid"},
    )

    counts = motor.promote_pending_drafts(chat_callable=_Recorder(reply))

    assert _row(tmp_path, first_id)["status"] == "promoted"
    assert _attempt(tmp_path, first_id) is None
    attempt = _attempt(tmp_path, second_id)
    assert attempt["last_failure_code"] == "invalid_decision"
    assert attempt["next_attempt_at_s"] == 400
    assert counts["kept"] == 1
    assert counts["reasons"]["invalid_decision"] == 1


def test_active_profile_backoff_causes_zero_model_calls_until_boundary(
    monkeypatch, tmp_path,
):
    motor = _make_motor(monkeypatch, tmp_path)
    row_id = _seed_draft(
        _store(tmp_path), "profile-1", "active",
        "streamer: usa Ollama local para mantener privacidad",
    )
    motor.promote_pending_drafts(
        chat_callable=_Recorder(ConnectionError("offline")),
    )
    success = _Recorder(_decisions({
        "i": 1,
        "keep": True,
        "text": "El streamer usa Ollama local para mantener privacidad.",
    }))

    motor._promotion_wall_clock = lambda: 159
    blocked = motor.promote_pending_drafts(chat_callable=success)
    assert blocked["skipped"] == "profile_backoff"
    assert success.calls == []

    motor._promotion_wall_clock = lambda: 160
    allowed = motor.promote_pending_drafts(chat_callable=success)
    assert len(success.calls) == 1
    assert allowed["kept"] == 1
    assert _row(tmp_path, row_id)["status"] == "promoted"


def test_cooling_draft_causes_zero_model_calls_until_exact_boundary(
    monkeypatch, tmp_path,
):
    motor = _make_motor(monkeypatch, tmp_path)
    _seed_draft(
        _store(tmp_path), "profile-1", "cooling",
        "streamer: usa Ollama local para mantener privacidad",
    )
    motor.promote_pending_drafts(chat_callable=_Recorder(_decisions()))
    next_reply = _Recorder(_decisions({
        "i": 1,
        "keep": False,
        "reason": "vague",
    }))

    motor._promotion_wall_clock = lambda: 399
    motor.promote_pending_drafts(chat_callable=next_reply)
    assert next_reply.calls == []

    motor._promotion_wall_clock = lambda: 400
    result = motor.promote_pending_drafts(chat_callable=next_reply)
    assert len(next_reply.calls) == 1
    assert result["rejected"] == 1


def test_due_draft_waits_for_later_profile_deadline(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path)
    _seed_draft(
        _store(tmp_path), "profile-1", "later",
        "streamer: usa Ollama local para mantener privacidad",
    )
    motor.promote_pending_drafts(chat_callable=_Recorder(_decisions()))
    backoff = motor._get_promotion_backoff_store()
    state = backoff.record_failure("profile-1", "model_transport", 390)
    assert state.next_attempt_at_s == 450
    next_reply = _Recorder(_decisions({
        "i": 1,
        "keep": False,
        "reason": "vague",
    }))

    motor._promotion_wall_clock = lambda: 400
    motor.promote_pending_drafts(chat_callable=next_reply)
    assert next_reply.calls == []

    motor._promotion_wall_clock = lambda: 450
    result = motor.promote_pending_drafts(chat_callable=next_reply)
    assert len(next_reply.calls) == 1
    assert result["rejected"] == 1
    assert backoff.get_state("profile-1") is None


def test_healthy_no_draft_sweep_resets_due_profile_backoff(
    monkeypatch, tmp_path,
):
    motor = _make_motor(monkeypatch, tmp_path)
    backoff = motor._get_promotion_backoff_store()
    backoff.record_failure("profile-1", "model_transport", 100)
    motor._promotion_wall_clock = lambda: 160
    stub = _Recorder(_decisions())

    motor.promote_pending_drafts(chat_callable=stub)

    assert stub.calls == []
    assert backoff.get_state("profile-1") is None


@pytest.mark.parametrize(
    ("probe", "failure_class"),
    [
        (TimeoutError("ps timeout"), "residency_timeout"),
        (ConnectionError("ps offline"), "residency_offline"),
        (SimpleNamespace(), "residency_malformed"),
        (
            SimpleNamespace(
                models=[SimpleNamespace(model="m"), {}],
            ),
            "residency_malformed",
        ),
    ],
)
def test_residency_infrastructure_failures_back_off_profile_only(
    monkeypatch, tmp_path, probe, failure_class,
):
    motor = _make_motor(monkeypatch, tmp_path)
    row_id = _seed_draft(
        _store(tmp_path), "profile-1", "residency",
        "streamer: usa Ollama local para mantener privacidad",
    )
    client = motor.ollama.Client.return_value
    if isinstance(probe, BaseException):
        client.ps.side_effect = probe
    else:
        client.ps.side_effect = None
        client.ps.return_value = probe
    stub = _Recorder(_decisions())

    counts = motor.promote_pending_drafts(chat_callable=stub)

    assert stub.calls == []
    assert counts["skipped"] == "model_not_resident"
    assert _backoff_state(tmp_path).last_failure_class == failure_class
    assert _attempt(tmp_path, row_id) is None


def test_valid_absent_resident_model_is_a_normal_gate_without_backoff(
    monkeypatch, tmp_path,
):
    motor = _make_motor(monkeypatch, tmp_path)
    _seed_draft(
        _store(tmp_path), "profile-1", "absent",
        "streamer: usa Ollama local para mantener privacidad",
    )
    client = motor.ollama.Client.return_value
    client.ps.side_effect = None
    client.ps.return_value = SimpleNamespace(models=[])

    counts = motor.promote_pending_drafts(
        chat_callable=_Recorder(_decisions()),
    )

    assert counts["skipped"] == "model_not_resident"
    assert _backoff_state(tmp_path) is None


def test_locked_read_records_profile_backoff_without_calling_model(
    monkeypatch, tmp_path,
):
    motor = _make_motor(monkeypatch, tmp_path)
    row_id = _seed_draft(
        _store(tmp_path), "profile-1", "read-lock",
        "streamer: usa Ollama local para mantener privacidad",
    )
    blocker = sqlite3.connect(str(tmp_path / "memorias.db"))
    blocker.execute("BEGIN EXCLUSIVE")
    stub = _Recorder(_decisions())
    try:
        motor.promote_pending_drafts(chat_callable=stub)
    finally:
        blocker.rollback()
        blocker.close()

    assert stub.calls == []
    assert _attempt(tmp_path, row_id) is None
    assert _backoff_state(tmp_path).last_failure_class == "sqlite_read"


def test_locked_atomic_write_rolls_back_and_notifies_only_after_commit(
    monkeypatch, tmp_path,
):
    motor = _make_motor(monkeypatch, tmp_path)
    events = []
    motor.ui_callback = events.append
    row_id = _seed_draft(
        _store(tmp_path), "profile-1", "write-lock",
        "streamer: usa Ollama local para mantener privacidad",
    )
    before = dict(_row(tmp_path, row_id))
    blockers = []

    def lock_then_reply(**_kwargs):
        blocker = sqlite3.connect(
            str(tmp_path / "memorias.db"), check_same_thread=False,
        )
        blocker.execute("BEGIN EXCLUSIVE")
        blockers.append(blocker)
        return _decisions({
            "i": 1,
            "keep": True,
            "text": "El streamer usa Ollama local para mantener privacidad.",
        })

    try:
        counts = motor.promote_pending_drafts(
            chat_callable=_Recorder(lock_then_reply),
        )
    finally:
        for blocker in blockers:
            blocker.rollback()
            blocker.close()

    assert dict(_row(tmp_path, row_id)) == before
    assert _attempt(tmp_path, row_id) is None
    assert counts["kept"] == 0
    assert events == []
    assert _backoff_state(tmp_path).last_failure_class == "sqlite_write"

@pytest.mark.parametrize("reply", [
    TimeoutError("watchdog_timeout:25.00s"),
    RuntimeError("provider offline"),
    "",                       # bounded model response with no visible content
    "Claro, aca va mi analisis del asunto",
    "{malformed",
    "{}",
    '{"decisions": []}',
])
def test_every_failure_leaves_every_draft_untouched_and_unjudged(monkeypatch, tmp_path, reply):
    motor = _make_motor(monkeypatch, tmp_path)
    store = _store(tmp_path)
    row_id = _seed_draft(store, "profile-1", "k1", "streamer: uso Ollama con Qwen3 en local")
    before = _row(tmp_path, row_id)
    stub = _Recorder(reply)

    counts = motor.promote_pending_drafts(chat_callable=stub)

    row = _row(tmp_path, row_id)
    assert row["judged_at"] == ""
    assert row["status"] == "draft"
    assert row["inactive"] == 0
    assert row["content"] == before["content"]
    assert row["updated_at"] == before["updated_at"]
    assert counts["kept"] == 0
    assert counts["rejected"] == 0


def test_operator_speaking_mid_sweep_refuses_the_stale_write(monkeypatch, tmp_path):
    """The read-inference-write race: a fresher capture must never be overwritten
    by a judgment of stale text, and must never be frozen durable."""
    motor = _make_motor(monkeypatch, tmp_path)
    store = _store(tmp_path)
    row_id = _seed_draft(store, "profile-1", "k1", "streamer: algo sobre el parche del juego")

    def racing_reply(**kwargs):
        # The operator returns to the topic while the model is still thinking.
        store.upsert_draft(
            "profile-1", "profile-1|k1", "titulo fresco",
            "streamer: el parche 1.4 de Silksong arreglo el jefe final",
        )
        return _decisions({"i": 1, "keep": True, "text": "El streamer hablo del parche del juego."})

    stub = _Recorder(racing_reply)
    counts = motor.promote_pending_drafts(chat_callable=stub)

    row = _row(tmp_path, row_id)
    assert row["content"] == "streamer: el parche 1.4 de Silksong arreglo el jefe final"
    assert row["status"] == "draft"
    assert row["judged_at"] == ""
    assert row["revision"] == 2
    assert counts["stale"] == 1
    assert counts["reasons"]["stale"] == 1
    assert counts["kept"] == 0


def test_operator_editing_mid_sweep_keeps_his_own_text_and_his_curated_status(monkeypatch, tmp_path):
    """`if_revision` alone cannot see an operator EDIT: `update_row` sets
    status='curated' but never bumps `revision`, so the judge's rewrite of the
    now-stale text would overwrite the operator's own words AND demote the row
    from curated (operator intent) to promoted (machine)."""
    motor = _make_motor(monkeypatch, tmp_path)
    store = _store(tmp_path)
    row_id = _seed_draft(store, "profile-1", "k1", "streamer: algo sobre el parche del juego")

    def racing_reply(**kwargs):
        # POST /api/memoria/update or the inspector panel, mid-inference.
        store.update_row(row_id, title="titulo del operador", content="texto escrito a mano")
        return _decisions({"i": 1, "keep": True, "text": "El streamer hablo del parche del juego."})

    counts = motor.promote_pending_drafts(chat_callable=_Recorder(racing_reply))

    row = _row(tmp_path, row_id)
    assert row["content"] == "texto escrito a mano"
    assert row["status"] == "curated"
    assert row["judged_at"] == ""
    assert counts["kept"] == 0
    assert counts["stale"] == 1


def test_operator_refreshing_a_draft_mid_sweep_is_not_rejected_on_stale_text(
    monkeypatch, tmp_path,
):
    """The reject path revalidates revision before hiding a draft.

    A draft refreshed with better content during the sweep must not be hidden
    or stamped on the strength of a judgment of text it no longer holds.
    """
    motor = _make_motor(monkeypatch, tmp_path)
    store = _store(tmp_path)
    row_id = _seed_draft(store, "profile-1", "k1", "streamer: hubo una correccion algo tecnico")

    def racing_reply(**kwargs):
        store.upsert_draft(
            "profile-1", "profile-1|k1", "titulo fresco",
            "streamer: el parche 1.4 de Silksong arreglo el jefe final",
        )
        return _decisions({"i": 1, "keep": False, "reason": "vague"})

    counts = motor.promote_pending_drafts(chat_callable=_Recorder(racing_reply))

    row = _row(tmp_path, row_id)
    assert row["content"] == "streamer: el parche 1.4 de Silksong arreglo el jefe final"
    assert row["inactive"] == 0
    assert row["judged_at"] == ""
    assert row["revision"] == 2
    assert counts["rejected"] == 0
    assert counts["stale"] == 1


def test_operator_curating_mid_sweep_is_not_rejected_on_the_text_he_replaced(monkeypatch, tmp_path):
    """The reject path's mirror of the operator-EDIT race the keep path already
    survives via `if_status`. An edit (`update_row`) or a pin (`set_flags`) sets
    status='curated' WITHOUT bumping `revision`, so the reject's (id, revision)
    match still hits — and a memory the operator just curated is stamped judged
    and hidden on the strength of a judgment of the text he replaced. There is no
    auto-recovery: `upsert_draft`'s CASE only fires on rows still in draft."""
    motor = _make_motor(monkeypatch, tmp_path)
    store = _store(tmp_path)
    row_id = _seed_draft(store, "profile-1", "k1", "streamer: hubo una correccion algo tecnico")

    def racing_reply(**kwargs):
        # The operator rewrites this exact memory while the model is thinking.
        store.update_row(row_id, title="titulo del operador", content="texto escrito a mano")
        return _decisions({"i": 1, "keep": False, "reason": "vague"})

    counts = motor.promote_pending_drafts(chat_callable=_Recorder(racing_reply))

    row = _row(tmp_path, row_id)
    assert row["content"] == "texto escrito a mano"
    assert row["status"] == "curated"
    assert row["inactive"] == 0
    assert row["judged_at"] == ""
    assert counts["rejected"] == 0
    assert counts["stale"] == 1


def test_operator_muted_draft_is_never_shipped_to_the_provider(monkeypatch, tmp_path):
    """The owner approved sending the draft batch to a CLOUD provider — not rows
    he had explicitly hidden. And a mute that got judged would be un-muted by
    upsert_draft's `judged_at != ''` CASE on the very next capture."""
    motor = _make_motor(monkeypatch, tmp_path)
    store = _store(tmp_path)
    muted_id = _seed_draft(store, "profile-1", "sec", "streamer: SENTINELA_SILENCIADA sobre un tema privado")
    _seed_draft(store, "profile-1", "pub", "streamer: uso teclado mecanico con switches lineales")
    assert store.set_flags(muted_id, inactive=True) is True
    stub = _Recorder(_decisions({"i": 1, "keep": False, "reason": "vague"}))

    counts = motor.promote_pending_drafts(chat_callable=stub)

    assert "SENTINELA_SILENCIADA" not in stub.prompt
    assert "switches lineales" in stub.prompt
    assert counts["considered"] == 1
    # ...and the operator's mute survives the next capture on the same topic.
    store.upsert_draft("profile-1", "profile-1|sec", "titulo nuevo", "streamer: otra vez ese tema privado")
    assert _row(tmp_path, muted_id)["inactive"] == 1


def test_a_locked_atomic_apply_is_profile_failure_not_a_lost_race(
    monkeypatch, tmp_path,
):
    """`stale` means "the operator was speaking on this topic during the sweep".
    A fail-open lock counted as `stale` sends the owner tuning judge criteria
    against a phantom."""
    motor = _make_motor(monkeypatch, tmp_path)
    store = _store(tmp_path)
    row_id = _seed_draft(store, "profile-1", "k1", "streamer: uso Ollama con Qwen3 en local")
    real_store = motor._get_memoria_store()

    def locked(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(real_store, "apply_promotion_batch", locked)
    stub = _Recorder(_decisions({"i": 1, "keep": True, "text": "El streamer corre Qwen3 en su maquina."}))

    counts = motor.promote_pending_drafts(chat_callable=stub)

    assert counts["stale"] == 0
    assert counts["kept"] == 0
    assert _row(tmp_path, row_id)["judged_at"] == ""
    assert _backoff_state(tmp_path).last_failure_class == "sqlite_write"


def test_judgment_update_failure_never_persists_or_counts_a_keep(
    monkeypatch, tmp_path,
):
    """Content, status, and judgment are one transaction, never partial."""
    motor = _make_motor(monkeypatch, tmp_path)
    store = _store(tmp_path)
    row_id = _seed_draft(store, "profile-1", "k1", "streamer: uso Ollama con Qwen3 en local")
    before = dict(_row(tmp_path, row_id))
    with sqlite3.connect(str(tmp_path / "memorias.db")) as conn:
        conn.execute(
            f"""
            CREATE TRIGGER fail_judgment_update
            BEFORE UPDATE OF judged_at ON memorias
            WHEN OLD.id = '{row_id}'
            BEGIN
                SELECT RAISE(ABORT, 'injected judgment failure');
            END
            """
        )
    judged = "El streamer corre Qwen3 sobre Ollama en su propia maquina."
    stub = _Recorder(_decisions({"i": 1, "keep": True, "text": judged}))

    counts = motor.promote_pending_drafts(chat_callable=stub)

    assert dict(_row(tmp_path, row_id)) == before
    assert counts["kept"] == 0
    assert _backoff_state(tmp_path).last_failure_class == "sqlite_write"


def test_promote_pending_drafts_never_raises_on_a_store_failure(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path)
    _seed_draft(_store(tmp_path), "profile-1", "k1", "streamer: uso Ollama con Qwen3 en local")

    def boom(*a, **kw):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(motor, "_get_memoria_store", boom)
    assert motor.promote_pending_drafts(chat_callable=_Recorder(_decisions())) == {
        "considered": 0, "decided": 0, "kept": 0, "rejected": 0, "stale": 0,
        "unjudged_remaining": 0, "reasons": llm_engine.Counter(), "skipped": "",
    }


def test_logs_carry_counts_but_never_memory_text(monkeypatch, tmp_path, caplog):
    """The stable_key is DERIVED here, not hand-written: a synthetic
    'profile-1|k1' key can never carry a sentinel, so the assertion would pass
    over `upsert_draft`'s own `logger.debug("memoria upsert conflict
    stable_key=%s ...")` — which fires on exactly the mid-sweep race path."""
    from opencohost.core.memory.memoria_store import derive_stable_key

    motor = _make_motor(monkeypatch, tmp_path)
    store = _store(tmp_path)
    content = "streamer: SENTINELADRAFT sobre sintetizadores modulares vintage"
    key = derive_stable_key("profile-1", content)
    assert "sentineladraft" in key  # the derived key genuinely carries the sentinel
    store.upsert_draft("profile-1", key, "titulo", content, signature="firma")
    judged = "El streamer SENTINELAJUZGADA colecciona sintetizadores analogicos."

    def racing_reply(**kwargs):
        store.upsert_draft("profile-1", key, "titulo", content + " y osciladores")
        return _decisions({"i": 1, "keep": True, "text": judged})

    with caplog.at_level(logging.DEBUG):
        motor.promote_pending_drafts(chat_callable=_Recorder(racing_reply))

    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "SENTINELADRAFT" not in blob
    assert "sentineladraft" not in blob
    assert "SENTINELAJUZGADA" not in blob


def test_a_part_answered_batch_reports_how_many_the_judge_actually_decided(
    monkeypatch, tmp_path, caplog
):
    """Live sweep 2026-08-14 10:15:52 read `considered=12 kept=3 rejected=0
    remaining=9`. Nine drafts came back with no verdict at all because the
    parser drops every unusable entry — here, a `keep` written as the string
    "yes" instead of a bool. Those rows remain unjudged but now receive bounded
    per-draft retry metadata instead of returning on every launch forever.

    kept+rejected cannot stand in for this: a parsed decision can still lose
    atomic row revalidation and land in neither bucket. `decided` is the
    judge's own answer rate, and the gap against `considered` is the bug.
    """
    motor = _make_motor(monkeypatch, tmp_path)
    store = _store(tmp_path)
    for i in range(3):
        store.upsert_draft(
            "profile-1", f"profile-1|k{i}", f"titulo{i}",
            f"streamer: colecciona sintetizadores modulares del tipo {i}",
            signature="firma",
        )

    reply = _decisions(
        {"i": 1, "keep": True, "text": "El streamer colecciona sintetizadores modulares."},
        {"i": 2, "keep": "yes"},   # not a bool -> skipped without a trace
        {"i": 3, "keep": "true"},  # ditto
    )

    with caplog.at_level(logging.INFO):
        counts = motor.promote_pending_drafts(chat_callable=_Recorder(reply))

    assert counts["considered"] == 3
    assert counts["decided"] == 1
    assert counts["kept"] == 1
    assert counts["rejected"] == 0
    # The two undecided rows are still unjudged, not quietly rejected.
    assert counts["unjudged_remaining"] == 2

    line = [
        r.getMessage() for r in caplog.records
        if "memoria promotion sweep: considered=" in r.getMessage()
    ][-1]
    assert "considered=3 decided=1" in line, line


# ---------------------------------------------------------------------------
# Bounded inference controls and the adaptive watchdog
# ---------------------------------------------------------------------------

def test_maintenance_request_uses_schema_and_deterministic_bounded_options(
    monkeypatch, tmp_path,
):
    motor = _make_motor(monkeypatch, tmp_path)
    _seed_draft(_store(tmp_path), "profile-1", "k1", "streamer: uso Ollama con Qwen3 en local")
    stub = _Recorder(_decisions())

    motor.promote_pending_drafts(chat_callable=stub)

    assert stub.calls[0].get("think") is False
    assert stub.calls[0]["options"] == {"temperature": 0, "num_predict": 512}
    assert isinstance(stub.calls[0].get("format"), dict)
    assert stub.calls[0]["format"].get("title") == "MemoryJudgeResult"


def test_gpt_oss_uses_low_thinking_level_with_the_same_hard_limits(
    monkeypatch, tmp_path,
):
    motor = _make_motor(monkeypatch, tmp_path)
    _seed_draft(_store(tmp_path), "profile-1", "k1", "streamer: uso GPT-OSS en local")
    motor._loaded_model = "gpt-oss:20b"
    stub = _Recorder(_decisions())

    motor.promote_pending_drafts(chat_callable=stub)

    assert stub.calls[0].get("think") == "low"
    assert stub.calls[0]["options"] == {"temperature": 0, "num_predict": 512}


def test_plain_model_keeps_the_num_predict_cap(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path)
    _seed_draft(_store(tmp_path), "profile-1", "k1", "streamer: uso Ollama con Qwen3 en local")
    stub = _Recorder(_decisions())

    motor.promote_pending_drafts(chat_callable=stub)

    assert stub.calls[0].get("think") is False
    assert stub.calls[0]["options"] == {"temperature": 0, "num_predict": 512}


def test_parse_promotion_decisions_rejects_out_of_bounds_index():
    from opencohost.core.llm_engine import _parse_promotion_decisions
    # index 999 is outside batch_len=3
    raw = json.dumps({"decisions": [{"i": 999, "keep": True, "text": "El streamer colecciona vinilos."}]})
    assert _parse_promotion_decisions(raw, 3) == []


def test_parse_promotion_decisions_rejects_every_duplicate_occurrence():
    from opencohost.core.llm_engine import _parse_promotion_decisions
    raw = json.dumps({
        "decisions": [
            {"i": 1, "keep": False, "reason": "vague"},
            {"i": 2, "keep": True, "text": "El streamer programa en Rust."},
            {"i": 1, "keep": True, "text": "segunda respuesta conflictiva"},
        ]
    })
    assert _parse_promotion_decisions(raw, 2) == [
        (2, "El streamer programa en Rust.", False, ""),
    ]


def test_parse_promotion_decisions_rejects_invalid_schema_fail_open():
    from opencohost.core.llm_engine import _parse_promotion_decisions
    assert _parse_promotion_decisions("not json at all", 3) == []
    assert _parse_promotion_decisions(json.dumps({"decisions": "not a list"}), 3) == []
    assert _parse_promotion_decisions(json.dumps({"wrong_key": []}), 3) == []



def test_the_output_cap_stays_local_sized_even_under_a_cloud_provider(monkeypatch, tmp_path):
    """Owner decision 2026-08-08 (F16): the judge transport is pinned local, so
    sizing `num_predict` off the ACTIVE PROVIDER (the old CLOUD_MAX_TOKENS
    branch, sized for the cloud model this call no longer reaches) would size
    the cap for the wrong model. Both configurations must now get the SAME
    local-sized cap, whether or not the active provider happens to be cloud.
    """
    local = _make_motor(monkeypatch, tmp_path, profile_id="profile-1")
    _seed_draft(_store(tmp_path), "profile-1", "k1", "streamer: probando el limite de salida")
    local_stub = _Recorder(_decisions())
    local.promote_pending_drafts(chat_callable=local_stub)

    cloud = _go_cloud(_make_motor(monkeypatch, tmp_path, profile_id="profile-2"))
    _seed_draft(_store(tmp_path), "profile-2", "k2", "streamer: probando el limite de salida")
    cloud_stub = _Recorder(_decisions())
    cloud.promote_pending_drafts(chat_callable=cloud_stub)

    assert local_stub.calls[0]["options"]["num_predict"] == llm_engine._PROMOTION_NUM_PREDICT
    assert cloud_stub.calls[0]["options"]["num_predict"] == llm_engine._PROMOTION_NUM_PREDICT


def test_empty_content_fails_open_without_retrying_uncapped(monkeypatch, tmp_path):
    """With think=False and bounded num_predict, empty content returns 0 promotions
    safely and leaves drafts unjudged without uncapped loops."""
    motor = _go_cloud(_make_motor(monkeypatch, tmp_path), model="glm-5.2")
    store = _store(tmp_path)
    row_id = _seed_draft(store, "profile-1", "k1", "streamer: uso GLM en la nube para el juez")
    calls = []

    def stub(**kwargs):
        calls.append({**kwargs, "options": dict(kwargs.get("options") or {})})
        return {"message": {"content": ""}}

    counts = motor.promote_pending_drafts(chat_callable=stub)

    assert len(calls) == 1
    assert calls[0]["model"] == "m"
    assert calls[0]["options"]["num_predict"] == 512
    assert calls[0]["think"] is False
    assert counts["kept"] == 0
    row = _row(tmp_path, row_id)
    assert row["judged_at"] == ""  # fail-open: unjudged


def test_the_empty_content_is_strictly_single_call_fail_open(monkeypatch, tmp_path):
    motor = _go_cloud(_make_motor(monkeypatch, tmp_path), model="glm-5.2")
    row_id = _seed_draft(
        _store(tmp_path), "profile-1", "k1", "streamer: uso GLM en la nube para el juez",
    )
    calls = []

    def always_thinking(**kwargs):
        calls.append(kwargs)
        return {"message": {"content": "", "thinking": "sigue razonando"}}

    counts = motor.promote_pending_drafts(chat_callable=always_thinking)

    assert len(calls) == 1
    assert counts["kept"] == 0
    # The malformed response leaves the draft unjudged; profile backoff
    # governs its retry.
    assert _row(tmp_path, row_id)["judged_at"] == ""



def test_judge_watchdog_budget_is_finite_and_derived_from_observed_latency(
    monkeypatch, tmp_path,
):
    from opencohost.config.settings import RETRY_MIN_REMAINING_SECONDS

    motor = _make_motor(monkeypatch, tmp_path)

    # Cold start uses the same finite fallback _pregen_retry_gate_seconds ships.
    motor._pregen_last_gen_duration = None
    assert motor._judge_timeout_seconds() == RETRY_MIN_REMAINING_SECONDS

    # Reasoning capability no longer expands the timeout: maintenance always has
    # explicit thinking and output limits.
    motor._loaded_model = "qwen3:8b"
    assert motor._judge_timeout_seconds() == RETRY_MIN_REMAINING_SECONDS

    # Measured: 2x the last completed generation.
    motor._loaded_model = "m"
    motor._pregen_last_gen_duration = 15.0
    assert motor._judge_timeout_seconds() == pytest.approx(30.0)

    # Clamped at both ends — this is what fails if anyone reintroduces a constant.
    motor._pregen_last_gen_duration = 1.0
    assert motor._judge_timeout_seconds() == llm_engine._JUDGE_BUDGET_FLOOR_SECONDS
    motor._pregen_last_gen_duration = 500.0
    assert motor._judge_timeout_seconds() == llm_engine._JUDGE_BUDGET_CEILING_SECONDS


def test_judge_budget_stays_local_adaptive_even_under_a_cloud_provider(monkeypatch, tmp_path):
    """Owner decision 2026-08-08 (F16): the judge transport is pinned local, so
    there is no cloud socket left for CLOUD_CHAT_TIMEOUT to bound — the budget
    must stay the local adaptive one (`_pregen_last_gen_duration *
    _JUDGE_BUDGET_FACTOR`, clamped) even when the active provider is cloud.
    5.0 * 2.0 = 10.0, clamped up to the floor.
    """
    motor = _go_cloud(_make_motor(monkeypatch, tmp_path))
    motor._pregen_last_gen_duration = 5.0

    assert motor._judge_timeout_seconds() == pytest.approx(llm_engine._JUDGE_BUDGET_FLOOR_SECONDS)


def test_cloud_active_provider_never_reaches_a_cloud_client_for_the_judge(monkeypatch, tmp_path):
    """The core F16 contract, exercised through the REAL undelegated transport
    (no `chat_callable` override — every other test in this suite bypasses
    `_ollama_judge_chat` entirely, so none of them actually prove the network
    boundary). With the active provider on cloud (`_go_cloud`), the judge must
    still resolve and request the LOCAL fallback model and must NEVER call the
    cloud client — draft contents (the owner's own conversation excerpts) must
    never reach it.
    """
    from opencohost.core.providers.cloud import cloud_llm_client

    motor = _go_cloud(_make_motor(monkeypatch, tmp_path), model="glm-5.2")
    store = _store(tmp_path)
    row_id = _seed_draft(store, "profile-1", "k1", "streamer: uso GLM en la nube para el juez")
    judged = "El streamer corre el juez de memorias localmente."

    cloud_calls = []
    monkeypatch.setattr(
        cloud_llm_client, "send_chat_completion",
        lambda **kwargs: cloud_calls.append(kwargs) or {"message": {"content": _decisions()}},
    )

    fake_ollama_client = MagicMock()
    fake_ollama_client.chat.return_value = {
        "message": {"content": _decisions({"i": 1, "keep": True, "text": judged})}
    }
    motor._create_ollama_scout_client = lambda *a, **kw: fake_ollama_client

    counts = motor.promote_pending_drafts()

    assert cloud_calls == []  # the network boundary itself: never touched
    assert fake_ollama_client.chat.call_count == 1
    assert fake_ollama_client.chat.call_args.kwargs["model"] == "m"
    assert counts["kept"] == 1
    assert _row(tmp_path, row_id)["status"] == "promoted"


def test_cloud_sweep_still_builds_a_local_ollama_client_for_the_pinned_judge(monkeypatch, tmp_path):
    """Owner decision 2026-08-08 (F16): the judge transport is pinned local even
    when the active provider is cloud, so `_run_promotion_judge` must still
    build the dedicated judge client — the OLD `and self._is_local` gate on
    this build (which used to skip it on cloud, since `_ollama_judge_chat`
    used to route to `_cloud_chat` there instead) is gone."""
    motor = _go_cloud(_make_motor(monkeypatch, tmp_path))
    _seed_draft(_store(tmp_path), "profile-1", "k1", "streamer: uso GLM en la nube para el juez")
    built = []
    motor._create_ollama_scout_client = lambda *a, **kw: built.append(kw) or MagicMock()
    monkeypatch.setattr(
        motor, "_ollama_judge_chat", lambda **kw: {"message": {"content": _decisions()}},
    )

    motor.promote_pending_drafts()

    assert len(built) == 1


def test_scout_client_factory_takes_a_timeout_and_defaults_to_the_scout_one(monkeypatch, tmp_path):
    """The real abort path: the HTTP timeout closes the socket, so Ollama
    cancels the generation and releases the single runner."""
    from opencohost.config.settings import LLM_SCOUT_TIMEOUT

    motor = _make_motor(monkeypatch, tmp_path)
    built = []

    class _FakeOllama:
        def Client(self, **kwargs):
            built.append(kwargs.get("timeout"))
            return MagicMock()

    fake = _FakeOllama()
    motor._create_ollama_scout_client(fake)
    motor._create_ollama_scout_client(fake, timeout=42.0)

    assert built == [LLM_SCOUT_TIMEOUT, 42.0]


def test_the_gate_reason_is_reported_so_a_dead_sweep_is_observable(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path)
    motor._current_profile_id = None

    assert motor.promote_pending_drafts(chat_callable=_Recorder(_decisions()))["skipped"] == "no_profile"
    assert motor._loaded_model is not None
    motor._current_profile_id = "profile-1"
    assert motor.promote_pending_drafts(chat_callable=_Recorder(_decisions()))["skipped"] == ""
