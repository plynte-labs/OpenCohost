"""Strict-TDD tests for the memoria draft-promotion sweep
(memory_promotion_20260725, WU3 + WU4).

ONE LLM call per launch judges the oldest unjudged drafts, rewrites the
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
import time
from unittest.mock import MagicMock

import pytest

import opencohost.core.llm_engine as llm_engine
from opencohost.core.llm_engine import MotorVocalIA
from opencohost.core.memory.memoria_store import MemoriaStore, build_signature


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_motor(monkeypatch, tmp_path, *, profile_id="profile-1"):
    """A motor wired for promotion: memorias on, store at tmp_path, model resident.

    `_provider_config` is pinned LOCAL explicitly — MotorVocalIA's constructor
    calls the real `load_provider_config()`, so without this every gate test
    would silently depend on whichever provider the machine running the suite
    happens to have active.

    `current_model`/`_last_known_good_model` are ALSO pinned explicitly to the
    same "m" as `_loaded_model`: owner decision 2026-08-08 (F16) makes
    `_judge_model()` fall back to `_last_known_good_model or current_model`
    whenever nothing is resident, and both attributes otherwise come from the
    constructor's real `resolve_startup_model()` call — without this pin, any
    test exercising that fallback would silently depend on the local disk
    config of the machine running the suite.
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
    motor._pending_model_switch = None
    motor._awaiting_first_success_after_switch = False
    motor._check_capabilities_reasoning = lambda model: False
    return motor


def _go_cloud(motor, *, model="glm-5.2"):
    """Put the motor on the owner's ACTUAL configuration: a cloud provider, and
    therefore NO resident local model for the whole process (`_check_ollama_service`
    returns before `_prepare_model`, and `_prepare_model` returns without ever
    assigning `_loaded_model`).

    Deliberately leaves `current_model`/`_last_known_good_model` at whatever
    `_make_motor` pinned ("m") — owner decision 2026-08-08 (F16) means the
    judge must fall back to THAT local model, never to `model` (the active
    cloud profile's model), which is why every promotion call in these tests
    keeps requesting "m" even after this call.
    """
    motor._provider_config = {
        "active_provider": "nvidia_nim",
        "profiles": {"nvidia_nim": {"base_url": "https://x/v1", "model": model}},
    }
    motor._loaded_model = None
    return motor


def _store(tmp_path) -> MemoriaStore:
    return MemoriaStore(tmp_path / "memorias.db")


def _row(tmp_path, row_id):
    with sqlite3.connect(str(tmp_path / "memorias.db")) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM memorias WHERE id = ?", (row_id,)).fetchone()


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
    # NOT a gate: the sweep reached the store and found nothing, so the startup
    # latch must arm. A blanket "return zero counts" would report a gate here.
    assert counts["skipped"] == ""


@pytest.mark.parametrize("attr,value", [
    ("_pending_model_switch", "otro-modelo"),
    ("_awaiting_first_success_after_switch", True),
    ("_current_profile_id", None),
])
def test_engine_gates_skip_the_call_entirely(monkeypatch, tmp_path, attr, value):
    """`_make_motor` pins `_provider_config` local precisely so this
    parametrisation cannot silently become "the feature is dead on cloud".

    NOTE: `_loaded_model=None` is NOT parametrized here anymore — see
    test_loaded_model_none_alone_no_longer_gates_the_sweep and
    test_no_local_model_at_all_gates_the_sweep_quietly, which cover its new
    (post owner decision 2026-08-08, F16) behavior instead.
    """
    motor = _make_motor(monkeypatch, tmp_path)
    _seed_draft(_store(tmp_path), "profile-1", "k1", "el streamer juega Silksong los martes")
    setattr(motor, attr, value)
    stub = _Recorder(_decisions({"i": 1, "keep": True, "text": "x"}))

    counts = motor.promote_pending_drafts(chat_callable=stub)

    assert stub.calls == []
    assert counts["considered"] == 0
    # Naming the gate is what stops this from also passing against a sweep that
    # simply does nothing — and is what run()'s latch reads.
    assert counts["skipped"] == {
        "_pending_model_switch": "model_switch_pending",
        "_awaiting_first_success_after_switch": "model_switch_pending",
        "_current_profile_id": "no_profile",
    }[attr]


def test_loaded_model_none_alone_no_longer_gates_the_sweep(monkeypatch, tmp_path):
    """Owner decision 2026-08-08 (F16): `_judge_model()` now falls back to
    `_last_known_good_model or current_model` when nothing is resident, so a
    bare `_loaded_model=None` (e.g. the local warm-up race, or the owner's
    cloud provider being active) no longer blocks the sweep by itself — it
    just requests the fallback local model instead of cold-loading the
    resident one."""
    motor = _make_motor(monkeypatch, tmp_path)
    motor._loaded_model = None
    _seed_draft(_store(tmp_path), "profile-1", "k1", "el streamer juega Silksong los martes")
    stub = _Recorder(_decisions({"i": 1, "keep": True, "text": "x"}))

    counts = motor.promote_pending_drafts(chat_callable=stub)

    assert len(stub.calls) == 1
    assert stub.calls[0]["model"] == "m"
    assert counts["skipped"] == ""


def test_no_local_model_at_all_gates_the_sweep_quietly(monkeypatch, tmp_path):
    """The one case that still gates proactively: no local model name can be
    resolved from ANY of the three sources (fresh install, nothing ever
    configured). Fails open BEFORE spending a token, same contract as every
    other gate."""
    motor = _make_motor(monkeypatch, tmp_path)
    motor._loaded_model = None
    motor._last_known_good_model = None
    motor.current_model = ""
    _seed_draft(_store(tmp_path), "profile-1", "k1", "el streamer juega Silksong los martes")
    stub = _Recorder(_decisions({"i": 1, "keep": True, "text": "x"}))

    counts = motor.promote_pending_drafts(chat_callable=stub)

    assert stub.calls == []
    assert counts["considered"] == 0
    assert counts["skipped"] == "no_local_model"


def test_cloud_provider_sweeps_using_the_local_fallback_model_not_the_cloud_one(monkeypatch, tmp_path):
    """Owner decision 2026-08-08 (F16), superseding the earlier "whatever
    provider is active" decision: the judge is pinned LOCAL always. On cloud
    `_loaded_model` is None for the ENTIRE process (`_check_ollama_service`
    returns before `_prepare_model`, which itself never assigns it), so the
    judge must fall back to `_last_known_good_model`/`current_model` ("m") —
    NEVER to the active cloud profile's model ("glm-5.2", set by `_go_cloud`).
    """
    motor = _go_cloud(_make_motor(monkeypatch, tmp_path))
    store = _store(tmp_path)
    row_id = _seed_draft(store, "profile-1", "k1", "streamer: uso GLM en la nube para el juez")
    judged = "El streamer corre el juez de memorias localmente."
    stub = _Recorder(_decisions({"i": 1, "keep": True, "text": judged}))

    counts = motor.promote_pending_drafts(chat_callable=stub)

    assert len(stub.calls) == 1
    assert stub.calls[0]["model"] == "m"  # the LOCAL fallback, never "glm-5.2"
    assert _row(tmp_path, row_id)["status"] == "promoted"
    assert counts["kept"] == 1


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


def test_reasoning_model_is_detected_from_the_resolved_local_fallback_model(monkeypatch, tmp_path):
    """`_fetch_show`/`_check_capabilities_reasoning` are stubbed to False (older
    Ollama / no probe) here, so the reasoning branch has to come from the name
    heuristic applied to the model the judge ACTUALLY requests — the resolved
    local fallback, even though the active provider is cloud and its profile
    model ("glm-5.2") carries no such marker."""
    motor = _go_cloud(_make_motor(monkeypatch, tmp_path), model="glm-5.2")
    motor._last_known_good_model = "qwen3:8b"
    _seed_draft(_store(tmp_path), "profile-1", "k1", "streamer: uso Qwen3 en local para juzgar")
    stub = _Recorder(_decisions())

    motor.promote_pending_drafts(chat_callable=stub)

    assert len(stub.calls) == 1
    assert stub.calls[0]["model"] == "qwen3:8b"
    assert "num_predict" not in stub.calls[0]["options"]


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


def test_batch_is_capped_at_forty_oldest_first(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path)
    store = _store(tmp_path)
    for i in range(60):
        _seed_draft(store, "profile-1", f"k{i:03d}", f"streamer: contenido numerado {i:03d} sobre juegos")
    stub = _Recorder(_decisions())

    counts = motor.promote_pending_drafts(chat_callable=stub)

    assert counts["considered"] == llm_engine._PROMOTION_DRAFT_BATCH == 40
    prompt = stub.prompt
    # Oldest first: 000..039 are in the prompt, 040+ are not.
    assert "contenido numerado 000" in prompt
    assert "contenido numerado 039" in prompt
    assert "contenido numerado 040" not in prompt
    assert "contenido numerado 059" not in prompt


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
# Failure paths — nothing written, nothing judged, next launch retries
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reply", [
    TimeoutError("watchdog_timeout:25.00s"),
    RuntimeError("provider offline"),
    "",                       # reasoning model that spent the whole budget in <think>
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


def test_operator_refreshing_a_draft_mid_sweep_is_not_rejected_on_stale_text(monkeypatch, tmp_path):
    """The reject mirror of the keep race. `mark_judged` matched on id alone, so
    a draft refreshed with BETTER content during the sweep window was hidden on
    the strength of a judgment of text it no longer held — and stamped judged,
    so nothing ever re-examined it."""
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


def test_a_locked_store_is_reported_as_write_failed_not_as_a_lost_race(monkeypatch, tmp_path):
    """`stale` means "the operator was speaking on this topic during the sweep".
    A fail-open lock counted as `stale` sends the owner tuning judge criteria
    against a phantom."""
    motor = _make_motor(monkeypatch, tmp_path)
    store = _store(tmp_path)
    row_id = _seed_draft(store, "profile-1", "k1", "streamer: uso Ollama con Qwen3 en local")
    real_store = motor._get_memoria_store()

    def locked(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(real_store, "update_row", locked)
    stub = _Recorder(_decisions({"i": 1, "keep": True, "text": "El streamer corre Qwen3 en su maquina."}))

    counts = motor.promote_pending_drafts(chat_callable=stub)

    assert counts["stale"] == 0
    assert counts["reasons"]["write_failed"] == 1
    assert counts["kept"] == 0
    assert _row(tmp_path, row_id)["judged_at"] == ""


def test_kept_counts_rows_written_even_when_the_bookkeeping_stamp_fails(monkeypatch, tmp_path):
    """The counters are the owner's ONLY feedback loop on the judge's criteria.
    `kept=0` on a launch that genuinely rewrote and promoted rows on disk is the
    one observability surface lying about what happened."""
    motor = _make_motor(monkeypatch, tmp_path)
    store = _store(tmp_path)
    row_id = _seed_draft(store, "profile-1", "k1", "streamer: uso Ollama con Qwen3 en local")
    real_store = motor._get_memoria_store()

    def locked(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(real_store, "mark_judged", locked)
    judged = "El streamer corre Qwen3 sobre Ollama en su propia maquina."
    stub = _Recorder(_decisions({"i": 1, "keep": True, "text": judged}))

    counts = motor.promote_pending_drafts(chat_callable=stub)

    assert _row(tmp_path, row_id)["status"] == "promoted"
    assert counts["kept"] == 1
    assert counts["reasons"]["write_failed"] == 1


def test_promote_pending_drafts_never_raises_on_a_store_failure(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path)
    _seed_draft(_store(tmp_path), "profile-1", "k1", "streamer: uso Ollama con Qwen3 en local")

    def boom(*a, **kw):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(motor, "_get_memoria_store", boom)
    assert motor.promote_pending_drafts(chat_callable=_Recorder(_decisions())) == {
        "considered": 0, "kept": 0, "rejected": 0, "stale": 0,
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


# ---------------------------------------------------------------------------
# The reasoning branch and the adaptive budget
# ---------------------------------------------------------------------------

def test_reasoning_model_gets_no_num_predict_cap(monkeypatch, tmp_path):
    """scout_digest's sixth gate would disable promotion FOREVER on Qwen3-class
    models. Drop the token cap instead, the way _generar_dialogo already does."""
    motor = _make_motor(monkeypatch, tmp_path)
    _seed_draft(_store(tmp_path), "profile-1", "k1", "streamer: uso Ollama con Qwen3 en local")
    motor._check_capabilities_reasoning = lambda model: True
    stub = _Recorder(_decisions())

    motor.promote_pending_drafts(chat_callable=stub)

    assert "num_predict" not in stub.calls[0]["options"]


def test_reasoning_detection_uses_the_name_heuristic_not_only_the_capability_probe(
    monkeypatch, tmp_path,
):
    """`_check_capabilities_reasoning` degrades to False on any older Ollama
    whose `show` response carries no `capabilities` field. Detecting reasoning
    with that narrow probe alone caps `num_predict` on a Qwen3-class model, which
    then burns the budget inside <think> and returns empty content — the same 25s
    inference re-paid every launch with zero promotions, forever. The engine's own
    resolver (which `_generar_dialogo` already trusts) ORs in the name heuristic."""
    motor = _make_motor(monkeypatch, tmp_path)
    _seed_draft(_store(tmp_path), "profile-1", "k1", "streamer: uso Ollama con Qwen3 en local")
    motor._loaded_model = "qwen3:8b"
    motor._check_capabilities_reasoning = lambda model: False   # older Ollama
    stub = _Recorder(_decisions())

    motor.promote_pending_drafts(chat_callable=stub)

    assert "num_predict" not in stub.calls[0]["options"]


def test_plain_model_keeps_the_num_predict_cap(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path)
    _seed_draft(_store(tmp_path), "profile-1", "k1", "streamer: uso Ollama con Qwen3 en local")
    stub = _Recorder(_decisions())

    motor.promote_pending_drafts(chat_callable=stub)

    assert stub.calls[0]["options"]["num_predict"] == llm_engine._PROMOTION_NUM_PREDICT


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


def test_empty_content_with_thinking_retries_once_uncapped_and_promotes(monkeypatch, tmp_path):
    """The cloud self-heal `_generar_dialogo` already ships and the judge lacked
    — still exercised here with the active PROVIDER on cloud (`_go_cloud`) to
    prove the self-heal keeps working once the judge itself is pinned local
    (owner decision 2026-08-08, F16): the model requested is the resolved
    local fallback ("m"), never the active profile's "glm-5.2".

    Detection is blind regardless of provider now: the name heuristic knows a
    handful of markers and "m" carries none, so it is classified "not
    reasoning", gets `num_predict`, burns the cap inside <think>, and returns
    empty `content` — the sweep then promotes NOTHING, every launch, forever,
    paying a full local inference each time. The response itself is the
    evidence, so no detection is needed: empty text + non-empty `thinking` +
    a cap in the options means drop the cap and re-issue ONCE.
    """
    motor = _go_cloud(_make_motor(monkeypatch, tmp_path), model="glm-5.2")
    store = _store(tmp_path)
    row_id = _seed_draft(store, "profile-1", "k1", "streamer: uso GLM en la nube para el juez")
    judged = "El streamer corre el juez de memorias localmente."
    calls = []

    def stub(**kwargs):
        # Snapshot: the implementation may pop from the options dict in place,
        # exactly as _generar_dialogo's own self-heal does.
        calls.append({**kwargs, "options": dict(kwargs.get("options") or {})})
        if len(calls) == 1:
            return {"message": {"content": "", "thinking": "razonando largo y tendido"}}
        return {"message": {"content": _decisions({"i": 1, "keep": True, "text": judged})}}

    counts = motor.promote_pending_drafts(chat_callable=stub)

    assert len(calls) == 2
    assert calls[0]["model"] == "m"
    assert calls[0]["options"]["num_predict"] == llm_engine._PROMOTION_NUM_PREDICT
    assert "num_predict" not in calls[1]["options"]
    assert calls[1]["messages"] == calls[0]["messages"]
    assert counts["kept"] == 1
    row = _row(tmp_path, row_id)
    assert row["content"] == judged
    assert row["status"] == "promoted"


def test_the_empty_content_retry_is_one_shot_never_a_loop(monkeypatch, tmp_path):
    motor = _go_cloud(_make_motor(monkeypatch, tmp_path), model="glm-5.2")
    row_id = _seed_draft(
        _store(tmp_path), "profile-1", "k1", "streamer: uso GLM en la nube para el juez",
    )
    calls = []

    def always_thinking(**kwargs):
        calls.append(kwargs)
        return {"message": {"content": "", "thinking": "sigue razonando"}}

    counts = motor.promote_pending_drafts(chat_callable=always_thinking)

    assert len(calls) == 2
    assert counts["kept"] == 0
    assert _row(tmp_path, row_id)["judged_at"] == ""   # unjudged: next launch retries


def test_the_retry_runs_inside_the_first_calls_budget_not_a_fresh_one(monkeypatch, tmp_path):
    """One retry must not double the operator's worst-case wait: the second call
    gets what is LEFT of the sweep's deadline, never a second full budget."""
    motor = _go_cloud(_make_motor(monkeypatch, tmp_path), model="glm-5.2")
    _seed_draft(_store(tmp_path), "profile-1", "k1", "streamer: uso GLM en la nube para el juez")
    budgets = []
    original = motor._ollama_chat_with_watchdog

    def spy(*, timeout, **kwargs):
        budgets.append(timeout)
        return original(timeout=timeout, **kwargs)

    motor._ollama_chat_with_watchdog = spy

    def always_thinking(**kwargs):
        # Long enough to clear the platform clock resolution, so "what is left of
        # the deadline" is measurably smaller than the deadline itself.
        time.sleep(0.05)
        return {"message": {"content": "", "thinking": "sigue razonando"}}

    motor.promote_pending_drafts(chat_callable=always_thinking)

    assert len(budgets) == 2
    assert budgets[1] < budgets[0]


def test_judge_budget_is_derived_from_observed_latency_not_a_constant(monkeypatch, tmp_path):
    from opencohost.config.settings import RETRY_MIN_REMAINING_SECONDS

    motor = _make_motor(monkeypatch, tmp_path)

    # Cold start (the startup trigger's own path): the SAME fallback
    # _pregen_retry_gate_seconds already ships.
    motor._pregen_last_gen_duration = None
    assert motor._judge_timeout_seconds() == RETRY_MIN_REMAINING_SECONDS

    # Cold start on a thinking model: no num_predict cap, so more headroom.
    # Detected by NAME, with the capability probe left saying False — the older
    # Ollama case. The narrow probe alone would give this model a 25s budget.
    motor._loaded_model = "qwen3:8b"
    assert motor._judge_timeout_seconds() == pytest.approx(RETRY_MIN_REMAINING_SECONDS * 3.0)

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


# ---------------------------------------------------------------------------
# WU4 — the startup call site inside run()'s existing idle branch
# ---------------------------------------------------------------------------

class _EmptyQueue:
    """A command_queue that is always empty, then feeds the shutdown sentinel."""

    def __init__(self, idle_ticks):
        self._remaining = idle_ticks

    def get(self, timeout=None):
        if self._remaining > 0:
            self._remaining -= 1
            raise queue.Empty
        return None

    def qsize(self):
        # run()'s idle branch drains control commands (§11 B4 backstop);
        # the drain sizes its bounded pass off qsize(). Always empty here.
        return 0


def _drive_run(motor, idle_ticks):
    motor.command_queue = _EmptyQueue(idle_ticks)
    motor._process_priority_queue = lambda: None
    motor._check_pending_model_switch = lambda: None
    motor._check_ollama_service = lambda: None
    motor._piper = MagicMock()
    motor.run()


def test_startup_sweep_fires_exactly_once_across_many_idle_ticks(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path)
    calls = []

    def sweep(**kw):
        calls.append(kw)
        return {"considered": 0, "kept": 0, "rejected": 0, "stale": 0,
                "unjudged_remaining": 0, "reasons": llm_engine.Counter(), "skipped": ""}

    motor.promote_pending_drafts = sweep

    _drive_run(motor, idle_ticks=25)

    assert len(calls) == 1


def test_a_gated_first_idle_tick_does_not_burn_the_latch(monkeypatch, tmp_path):
    """The latch must be armed on the OUTCOME, not on the attempt.

    On the API surface `EngineHost.start()` calls `motor.start()` and only
    reaches `_seed_startup_profile()` — the sole setter of `_current_profile_id`
    — after building the health monitor, the aggregator, the agenda controller
    (a sqlite read) and the music library. The engine thread's 1.0s idle tick
    routinely wins that race, so the first sweep is gated on a None profile. If
    the latch is set on the attempt, the profile arrives 200ms later and the
    sweep never runs again for the whole process, with no log and no retry.
    """
    motor = _make_motor(monkeypatch, tmp_path)
    motor._current_profile_id = None
    attempts = []

    def sweep(**kw):
        attempts.append(motor._current_profile_id)
        if motor._current_profile_id is None:
            return {"considered": 0, "kept": 0, "rejected": 0, "stale": 0,
                    "unjudged_remaining": 0, "reasons": llm_engine.Counter(),
                    "skipped": "no_profile"}
        return {"considered": 0, "kept": 0, "rejected": 0, "stale": 0,
                "unjudged_remaining": 0, "reasons": llm_engine.Counter(), "skipped": ""}

    motor.promote_pending_drafts = sweep
    ticks = []

    def seed_the_profile_on_the_third_tick():
        ticks.append(1)
        if len(ticks) == 3:
            motor._current_profile_id = "profile-1"

    motor._check_pending_model_switch = seed_the_profile_on_the_third_tick

    motor.command_queue = _EmptyQueue(8)
    motor._process_priority_queue = lambda: None
    motor._check_ollama_service = lambda: None
    motor._piper = MagicMock()
    motor.run()

    assert attempts == [None, None, "profile-1"]


def test_the_gate_reason_is_reported_so_a_dead_sweep_is_observable(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path)
    motor._current_profile_id = None

    assert motor.promote_pending_drafts(chat_callable=_Recorder(_decisions()))["skipped"] == "no_profile"
    assert motor._loaded_model is not None
    motor._current_profile_id = "profile-1"
    assert motor.promote_pending_drafts(chat_callable=_Recorder(_decisions()))["skipped"] == ""


def test_a_raising_sweep_does_not_break_the_engine_loop(monkeypatch, tmp_path):
    motor = _make_motor(monkeypatch, tmp_path)
    seen = []

    def exploding(**kw):
        seen.append(kw)
        raise RuntimeError("judge exploded")

    motor.promote_pending_drafts = exploding
    ticks = []
    motor._check_pending_model_switch = lambda: ticks.append(1)

    motor.command_queue = _EmptyQueue(5)
    motor._process_priority_queue = lambda: None
    motor._check_ollama_service = lambda: None
    motor._piper = MagicMock()
    motor.run()  # must return via the shutdown sentinel, not via the exception

    assert len(seen) == 1
    assert len(ticks) == 5  # the loop kept ticking after the failure
