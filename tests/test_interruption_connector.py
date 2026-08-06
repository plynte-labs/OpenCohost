"""WU5 (agenda_no_dead_air fase 2, design-fase2.md §3 WU5 [v3], D1/D2/D3):
interruption with connector-based return.

Owner-resolved policies:
  D1  PTT-only, position-aware cut ("1B con margen"): typed NEVER cuts; a PTT
      arriving while an AGENDA turn speaks applies zones over speech progress —
      early (<25%) / late (>75%) defer, mid (25-75%) cut iff the remaining
      estimate exceeds CUT_THRESHOLD_SECONDS. Gated behind a host-installed
      flag (default off) so CTK never cuts.
  D2  return-by-default with deterministic skips: after the interruption answer
      plays, Kira returns to the STASHED next-turn agenda draft (connector +
      draft) UNLESS topic changed/closing/stopped, the stash was invalidated
      (profile/model switch), or > RETURN_MAX_DETOUR_TURNS interactive turns
      played since the cut.
  D3  connector: parameterized pool floor + opportunistic generated upgrade.

Convention mirrors test_interactive_pregen.py: a constructed-but-unstarted
motor, `_hablar`/`_generar_dialogo`/`_commit_history` patched so no real
TTS/Ollama runs, and the worker loop driven synchronously.
"""

import logging
import queue
import threading
import time
from types import SimpleNamespace

import pytest

from opencohost.config.settings import (
    CUT_THRESHOLD_SECONDS,
    CUT_ZONE_EARLY,
    CUT_ZONE_LATE,
    FROZEN_STASH_MAX_HOLD_SECONDS,
    RETURN_MAX_DETOUR_TURNS,
)
from opencohost.core.llm_engine import MotorVocalIA


def _bare_motor() -> MotorVocalIA:
    # Hermetic: _generar_dialogo reaches the live ollama.show probe in _fetch_show
    # via TWO callers — _discover_model_ctx and _resolve_reasoning_classification —
    # so both caches are seeded. Bounded now, but still a network call.
    motor = MotorVocalIA(queue.Queue(), lambda event: None)
    motor.current_model = "llama3"
    motor._reasoning_model_cache["llama3"] = False
    motor._model_ctx_limit = {"llama3": 8192}
    return motor


def _wait_until(pred, timeout: float = 2.0, interval: float = 0.005) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return bool(pred())


def _seed_queue(motor, items):
    with motor._pq_lock:
        motor._priority_queue = list(items)


def _speaking_agenda(motor, *, played, total, remaining=None):
    """Model an agenda turn mid-speech with a given progress fraction."""
    motor._ptt_position_cut_enabled = True
    motor._speaking = True
    motor._current_speech_source = "kira-agenda"
    motor._speech_progress = {"total": total, "played": played, "start": time.time() - 5.0, "first_play": None}
    if remaining is not None:
        motor.speech_remaining_estimate = lambda: remaining


def _agenda_draft():
    return {"payload": "AG2", "dialogo": "Siguiente beat de agenda.", "priority": 2, "source": "kira-agenda", "gen_ms": 500}


# ══════════════════════════════════════════════════════════════════════════
# AC5.1 — typed input never cuts
# ══════════════════════════════════════════════════════════════════════════


def test_ac51_seam_off_by_default_never_cuts():
    """The host flag is OFF by default (CTK-unchanged): even mid-zone with a
    long remaining window, the seam must not cut."""
    motor = _bare_motor()
    motor._speaking = True
    motor._current_speech_source = "kira-agenda"
    motor._speech_progress = {"total": 100, "played": 50, "start": time.time(), "first_play": None}
    motor.speech_remaining_estimate = lambda: 999.0
    cut = []
    motor.interrupt_speaking = lambda: cut.append(1)

    assert motor.ptt_position_cut_enabled is False
    assert motor.ptt_interrupt_if_agenda_speaking() == "off"
    assert cut == [], "policy off by default -> never cut (CTK regression guard)"


def test_ac51_never_cuts_a_non_agenda_speech_source():
    """A chat/direct turn speaking is never cut by the PTT seam — only agenda
    speech is a cut candidate (typed answers never get cut)."""
    motor = _bare_motor()
    motor._ptt_position_cut_enabled = True
    motor._speaking = True
    motor._current_speech_source = "chat"
    motor._speech_progress = {"total": 100, "played": 50, "start": time.time(), "first_play": None}
    motor.speech_remaining_estimate = lambda: 999.0
    cut = []
    motor.interrupt_speaking = lambda: cut.append(1)

    assert motor.ptt_interrupt_if_agenda_speaking() == "off"
    assert cut == []


def test_ac51_never_cuts_when_not_speaking():
    motor = _bare_motor()
    motor._ptt_position_cut_enabled = True
    motor._speaking = False
    cut = []
    motor.interrupt_speaking = lambda: cut.append(1)
    assert motor.ptt_interrupt_if_agenda_speaking() == "off"
    assert cut == []


# ══════════════════════════════════════════════════════════════════════════
# AC5.2 — PTT zones (early/late defer, mid margin cut both sides)
# ══════════════════════════════════════════════════════════════════════════


def test_ac52_early_zone_defers():
    motor = _bare_motor()
    motor._prefetched_agenda = _agenda_draft()
    # 10% progress -> early zone -> defer, never cut.
    _speaking_agenda(motor, played=10, total=100, remaining=999.0)
    cut = []
    motor.interrupt_speaking = lambda: cut.append(1)

    assert motor.ptt_interrupt_if_agenda_speaking() == "defer"
    assert cut == []
    assert motor._frozen_stash is None, "a deferred PTT must not freeze the draft"


def test_ac52_late_zone_defers():
    motor = _bare_motor()
    motor._prefetched_agenda = _agenda_draft()
    # 90% progress -> late zone -> defer.
    _speaking_agenda(motor, played=90, total=100, remaining=999.0)
    cut = []
    motor.interrupt_speaking = lambda: cut.append(1)

    assert motor.ptt_interrupt_if_agenda_speaking() == "defer"
    assert cut == []


def test_ac52_mid_zone_cuts_when_remaining_exceeds_threshold():
    motor = _bare_motor()
    motor._prefetched_agenda = _agenda_draft()
    # 50% progress -> mid zone; remaining above the threshold -> CUT.
    _speaking_agenda(motor, played=50, total=100, remaining=CUT_THRESHOLD_SECONDS + 5.0)
    cut = []
    motor.interrupt_speaking = lambda: cut.append(1)

    assert motor.ptt_interrupt_if_agenda_speaking() == "cut"
    assert cut == [1], "mid zone with a long remaining window must cut"
    assert motor._frozen_stash is not None, "a cut must freeze the next-turn agenda draft"
    assert motor._frozen_stash["payload"] == "AG2"
    assert motor._prefetched_agenda is None, "the frozen draft leaves the active pregen slot"


def test_ac52_mid_zone_defers_when_remaining_below_threshold():
    motor = _bare_motor()
    motor._prefetched_agenda = _agenda_draft()
    # 50% progress -> mid zone; remaining BELOW the threshold -> defer (ends soon).
    _speaking_agenda(motor, played=50, total=100, remaining=CUT_THRESHOLD_SECONDS - 5.0)
    cut = []
    motor.interrupt_speaking = lambda: cut.append(1)

    assert motor.ptt_interrupt_if_agenda_speaking() == "defer"
    assert cut == [], "mid zone below the margin defers (the turn ends soon anyway)"
    assert motor._frozen_stash is None


def test_ac52_zone_bounds_come_from_settings():
    # The zone boundaries are owner-tunable settings constants, not magic numbers.
    assert 0.0 < CUT_ZONE_EARLY < CUT_ZONE_LATE < 1.0
    assert CUT_THRESHOLD_SECONDS > 0
    assert RETURN_MAX_DETOUR_TURNS >= 1


def test_ac52_mid_zone_defers_when_remaining_equals_threshold():
    """F8a: the margin rule cuts only when remaining STRICTLY exceeds the
    threshold — equality defers (the turn ends soon anyway)."""
    motor = _bare_motor()
    motor._prefetched_agenda = _agenda_draft()
    _speaking_agenda(motor, played=50, total=100, remaining=CUT_THRESHOLD_SECONDS)
    cut = []
    motor.interrupt_speaking = lambda: cut.append(1)

    assert motor.ptt_interrupt_if_agenda_speaking() == "defer"
    assert cut == [], "remaining == threshold defers (strict > is required to cut)"
    assert motor._frozen_stash is None


def test_ac52_mid_zone_no_draft_to_freeze_defers():
    """F2: a mid-zone PTT with a long remaining window but NOTHING to freeze
    (no next-turn draft cached) must defer, not cut — cutting with nothing to
    return to would lose the beat forever."""
    motor = _bare_motor()
    motor._prefetched_agenda = None  # no next-turn draft to freeze
    _speaking_agenda(motor, played=50, total=100, remaining=CUT_THRESHOLD_SECONDS + 5.0)
    cut = []
    motor.interrupt_speaking = lambda: cut.append(1)

    assert motor.ptt_interrupt_if_agenda_speaking() == "defer"
    assert cut == [], "no draft to freeze -> defer, never cut (the beat is not lost)"
    assert motor._frozen_stash is None


def test_ac52_progress_exactly_at_early_bound_is_mid_zone():
    """F8b: progress == CUT_ZONE_EARLY is INSIDE the mid (cut) band — the defer
    condition is `frac < CUT_ZONE_EARLY`, so the boundary itself cuts. Pinned
    deliberately."""
    motor = _bare_motor()
    motor._prefetched_agenda = _agenda_draft()
    # frac == CUT_ZONE_EARLY exactly.
    played = int(round(CUT_ZONE_EARLY * 100))
    _speaking_agenda(motor, played=played, total=100, remaining=CUT_THRESHOLD_SECONDS + 5.0)
    cut = []
    motor.interrupt_speaking = lambda: cut.append(1)

    assert motor.ptt_interrupt_if_agenda_speaking() == "cut"
    assert cut == [1], "progress exactly at the early bound is in the mid (cut) zone"


def test_ac52_progress_exactly_at_late_bound_is_mid_zone():
    """F8b: progress == CUT_ZONE_LATE is INSIDE the mid (cut) band — the defer
    condition is `frac > CUT_ZONE_LATE`, so the boundary itself cuts. Pinned
    deliberately."""
    motor = _bare_motor()
    motor._prefetched_agenda = _agenda_draft()
    # frac == CUT_ZONE_LATE exactly.
    played = int(round(CUT_ZONE_LATE * 100))
    _speaking_agenda(motor, played=played, total=100, remaining=CUT_THRESHOLD_SECONDS + 5.0)
    cut = []
    motor.interrupt_speaking = lambda: cut.append(1)

    assert motor.ptt_interrupt_if_agenda_speaking() == "cut"
    assert cut == [1], "progress exactly at the late bound is in the mid (cut) zone"


# ══════════════════════════════════════════════════════════════════════════
# AC5.3 — cut -> answer -> connector + resume, exactly once, spoken order
# ══════════════════════════════════════════════════════════════════════════


def test_ac53_cut_then_answer_then_connector_resume_in_spoken_order():
    motor = _bare_motor()
    committed = []
    motor._commit_history = lambda contexto, dialogo, **kw: committed.append((dialogo, kw.get("source")))
    spoke = []
    motor._hablar = lambda texto, source="direct": spoke.append((texto, source))
    motor._record_accepted_agenda_output = lambda d: None

    # 1) An agenda turn is mid-speech with the NEXT draft pregenerated.
    motor._prefetched_agenda = _agenda_draft()
    _speaking_agenda(motor, played=50, total=100, remaining=CUT_THRESHOLD_SECONDS + 5.0)

    # 2) PTT cut -> freeze the agenda draft, interrupt.
    motor._speaking_flag_cut = []
    real_interrupt = motor.interrupt_speaking
    motor.interrupt_speaking = lambda: (real_interrupt(), motor._speaking_flag_cut.append(1))
    assert motor.ptt_interrupt_if_agenda_speaking() == "cut"
    assert motor.has_frozen_stash() is True

    # 3) The interruption answer P plays (interactive). speech ended after the cut.
    motor._speaking = False
    motor._prefetched_agenda = {"payload": "P", "dialogo": "Respuesta PTT.", "priority": 0, "source": "ptt"}
    _seed_queue(motor, [(0, time.time(), "P", "ptt", None)])
    motor._process_priority_queue()

    assert spoke[0] == ("Respuesta PTT.", "ptt")
    assert motor._detour_turns == 1, "the interruption answer counts as one detour turn"
    assert motor.has_frozen_stash() is True, "the frozen agenda draft survives the detour"

    # 4) The RETURN: restore the frozen draft (connector resolved), requeue, pop.
    key = motor.restore_frozen_stash(tema="modelos de lenguaje")
    assert key is not None and key[0] == "AG2"
    _seed_queue(motor, [(2, time.time(), "AG2", "kira-agenda", None)])
    motor._process_priority_queue()

    # The resumed turn was spoken exactly once, connector-prepended.
    resumed = [s for s in spoke if s[1] == "kira-agenda"]
    assert len(resumed) == 1, "the stashed turn resumes exactly once"
    resumed_text = resumed[0][0]
    assert resumed_text.endswith("Siguiente beat de agenda."), "the connector prepends the stashed dialogo"
    assert resumed_text != "Siguiente beat de agenda.", "a connector text was prepended"
    assert motor.has_frozen_stash() is False, "the frozen stash is consumed at the return"

    # History committed in SPOKEN order: the interruption answer BEFORE the resumed turn,
    # and the resumed commit is the single connector+dialogo turn.
    assert committed[0] == ("Respuesta PTT.", "ptt")
    assert committed[-1][1] == "kira-agenda"
    assert committed[-1][0] == resumed_text, "connector+dialogo commit as ONE turn"
    assert len([c for c in committed if c[1] == "kira-agenda"]) == 1, "exactly one resumed commit"


def test_ac53_resumed_boundary_telemetry_labels_draft_resumed(caplog):
    motor = _bare_motor()
    motor._commit_history = lambda *a, **kw: None
    motor._hablar = lambda texto, source="direct": None
    motor._record_accepted_agenda_output = lambda d: None

    motor._prefetched_agenda = _agenda_draft()
    _speaking_agenda(motor, played=50, total=100, remaining=CUT_THRESHOLD_SECONDS + 5.0)
    assert motor.ptt_interrupt_if_agenda_speaking() == "cut"
    motor._speaking = False

    motor.restore_frozen_stash(tema="algo")
    _seed_queue(motor, [(2, time.time(), "AG2", "kira-agenda", None)])

    caplog.set_level(logging.INFO, logger="OpenCohost")
    motor._process_priority_queue()

    lines = [r.getMessage() for r in caplog.records if "Pregen boundary:" in r.getMessage()]
    assert any("draft=resumed" in ln for ln in lines), "the resume boundary is labelled draft=resumed"


# ══════════════════════════════════════════════════════════════════════════
# AC5.4 — each skip condition suppresses the return
# ══════════════════════════════════════════════════════════════════════════


def test_ac54_detour_over_max_suppresses_the_return():
    motor = _bare_motor()
    motor._prefetched_agenda = _agenda_draft()
    _speaking_agenda(motor, played=50, total=100, remaining=CUT_THRESHOLD_SECONDS + 5.0)
    motor.ptt_interrupt_if_agenda_speaking()
    assert motor.has_frozen_stash() is True

    # More than RETURN_MAX_DETOUR_TURNS interactive turns chained since the cut.
    for _ in range(RETURN_MAX_DETOUR_TURNS + 1):
        motor._note_detour_turn("chat")

    assert motor._detour_turns == RETURN_MAX_DETOUR_TURNS + 1
    assert motor.detour_exceeded() is True, "a long interactive chain skips the return"


def test_ac54_within_max_does_not_skip():
    motor = _bare_motor()
    motor._prefetched_agenda = _agenda_draft()
    _speaking_agenda(motor, played=50, total=100, remaining=CUT_THRESHOLD_SECONDS + 5.0)
    motor.ptt_interrupt_if_agenda_speaking()

    motor._note_detour_turn("chat")  # exactly one detour turn

    assert motor._detour_turns == 1
    assert motor.detour_exceeded() is False, "one detour turn is within the budget"


def test_ac54_profile_switch_invalidates_the_frozen_stash():
    motor = _bare_motor()
    motor._prefetched_agenda = _agenda_draft()
    _speaking_agenda(motor, played=50, total=100, remaining=CUT_THRESHOLD_SECONDS + 5.0)
    motor.ptt_interrupt_if_agenda_speaking()
    assert motor.has_frozen_stash() is True

    motor._invalidate_frozen_stash()  # what set_profile / switch_llm_tier call

    assert motor.has_frozen_stash() is False, "an invalidated stash suppresses the return"
    assert motor.restore_frozen_stash(tema="x") is None


def test_ac54_session_stop_invalidates_via_drop_pending_sources():
    motor = _bare_motor()
    motor._prefetched_agenda = _agenda_draft()
    _speaking_agenda(motor, played=50, total=100, remaining=CUT_THRESHOLD_SECONDS + 5.0)
    motor.ptt_interrupt_if_agenda_speaking()
    assert motor.has_frozen_stash() is True

    # Emergency/soft stop drops pending agenda -> the frozen stash must go too.
    motor.drop_pending_sources(("kira-agenda",))

    assert motor.has_frozen_stash() is False, "a session stop suppresses the return"


def test_ac54_discard_resets_detour_counter():
    motor = _bare_motor()
    motor._prefetched_agenda = _agenda_draft()
    _speaking_agenda(motor, played=50, total=100, remaining=CUT_THRESHOLD_SECONDS + 5.0)
    motor.ptt_interrupt_if_agenda_speaking()
    motor._note_detour_turn("chat")
    assert motor._detour_turns == 1

    motor.discard_frozen_stash()

    assert motor.has_frozen_stash() is False
    assert motor._detour_turns == 0, "discarding the stash resets the detour counter"


# ══════════════════════════════════════════════════════════════════════════
# AC5.5 — connector pool floor (upgrade absent) + no immediate repetition
# ══════════════════════════════════════════════════════════════════════════


def test_ac55_pool_floor_plays_when_upgrade_absent():
    motor = _bare_motor()
    motor._prefetched_agenda = _agenda_draft()
    _speaking_agenda(motor, played=50, total=100, remaining=CUT_THRESHOLD_SECONDS + 5.0)
    motor.ptt_interrupt_if_agenda_speaking()
    # No connector upgrade landed -> the floor must fill it.
    assert motor._frozen_stash.get("connector") is None

    key = motor.restore_frozen_stash(tema="inteligencia artificial")

    assert key is not None
    connector = motor._prefetched_agenda.get("connector")
    assert connector, "the pool floor must supply a connector when the upgrade is absent"
    assert "inteligencia artificial" in connector, "the floor template is parameterized with {tema}"


def test_ac55_pool_floor_no_immediate_repetition():
    motor = _bare_motor()
    picks = [motor._pick_connector_floor("tema") for _ in range(4)]
    # No two CONSECUTIVE picks are identical.
    for a, b in zip(picks, picks[1:]):
        assert a != b, "the connector floor must not repeat the same template back-to-back"


def test_ac55_upgrade_used_when_present_and_clean():
    motor = _bare_motor()
    motor._prefetched_agenda = _agenda_draft()
    _speaking_agenda(motor, played=50, total=100, remaining=CUT_THRESHOLD_SECONDS + 5.0)
    motor.ptt_interrupt_if_agenda_speaking()
    # A generated upgrade landed during the answer's TTS.
    with motor._prefetch_lock:
        motor._frozen_stash["connector"] = "Che, retomando lo de antes,"

    motor.restore_frozen_stash(tema="tema ignorado")

    assert motor._prefetched_agenda["connector"] == "Che, retomando lo de antes,", (
        "a ready, clean upgrade is used over the floor"
    )


def test_ac55_i18n_connector_templates_slot_loads():
    from opencohost.i18n import active as i18n_active

    templates = i18n_active.connector_templates()
    assert isinstance(templates, tuple)
    assert len(templates) >= 8, "the connector floor pool ships ~8 templates"
    assert all("{tema}" in t for t in templates), "every floor template carries a {tema} slot"


def test_restore_frozen_stash_bumps_pregen_epoch():
    """F5 (WARNING): the stash re-entering the active slot must bump the pregen
    epoch (same pattern as _commit_history) so an in-flight generation keyed to the
    pre-restore epoch is invalidated and can never overwrite the resumed draft."""
    motor = _bare_motor()
    motor._prefetched_agenda = _agenda_draft()
    _speaking_agenda(motor, played=50, total=100, remaining=CUT_THRESHOLD_SECONDS + 5.0)
    motor.ptt_interrupt_if_agenda_speaking()
    assert motor.has_frozen_stash() is True

    epoch_before = motor._prefetch_epoch
    motor.restore_frozen_stash(tema="algo")

    assert motor._prefetch_epoch == epoch_before + 1, (
        "restoring the frozen stash bumps the pregen epoch (invalidates stale in-flight work)"
    )


# ══════════════════════════════════════════════════════════════════════════
# AC5.6 — connector upgrade never evicts / delays a real pregen
# ══════════════════════════════════════════════════════════════════════════


def test_ac56_upgrade_does_not_evict_a_cached_real_pregen():
    motor = _bare_motor()
    motor._prefetched_agenda = _agenda_draft()
    _speaking_agenda(motor, played=50, total=100, remaining=CUT_THRESHOLD_SECONDS + 5.0)
    motor.ptt_interrupt_if_agenda_speaking()

    # A REAL interactive pregen now occupies the active slot (P's answer).
    real = {"payload": "P", "dialogo": "PD", "priority": 0, "source": "ptt", "gen_ms": 10}
    motor._prefetched_agenda = real
    gen = []
    motor._generar_dialogo = lambda *a, **kw: gen.append(1) or "connector line"

    motor._maybe_generate_connector_upgrade()

    assert motor._prefetched_agenda is real, "the connector upgrade must not evict the real pregen"
    assert gen == [], "the connector upgrade must not even spawn while a real pregen occupies the slot"
    assert motor._frozen_stash.get("connector") is None


def test_ac56_upgrade_does_not_spawn_while_a_pregen_is_inflight():
    motor = _bare_motor()
    motor._prefetched_agenda = _agenda_draft()
    _speaking_agenda(motor, played=50, total=100, remaining=CUT_THRESHOLD_SECONDS + 5.0)
    motor.ptt_interrupt_if_agenda_speaking()

    with motor._prefetch_lock:
        motor._pregen_inflight = {"payload": "P", "source": "ptt", "priority": 0}
    gen = []
    motor._generar_dialogo = lambda *a, **kw: gen.append(1) or "connector line"

    motor._maybe_generate_connector_upgrade()

    assert gen == [], "the connector upgrade must yield to an in-flight real pregen"


def test_ac56_upgrade_worker_skips_when_llm_generating_held():
    """F3 (CRITICAL): the upgrade worker must not run a SECOND concurrent Ollama
    generation. With `_llm_generating` held (a generation already in flight), the
    worker claims occupancy atomically and skips — it must never call the LLM."""
    motor = _bare_motor()
    motor._prefetched_agenda = _agenda_draft()
    _speaking_agenda(motor, played=50, total=100, remaining=CUT_THRESHOLD_SECONDS + 5.0)
    motor.ptt_interrupt_if_agenda_speaking()
    # Slot free, but a generation is already in flight (single-Ollama rule).
    motor._prefetched_agenda = None
    with motor._lock:
        motor._llm_generating = True
    gen = []
    motor._preview_accept_agenda_output = lambda d: True
    motor._generar_dialogo = lambda *a, **kw: gen.append(1) or "connector line"

    motor._maybe_generate_connector_upgrade()

    # The worker runs on a daemon thread; give it a beat to (not) call the LLM.
    time.sleep(0.05)
    assert gen == [], "the upgrade worker must not call the LLM while one is already in flight"
    assert motor._frozen_stash.get("connector") is None, "no connector lands when occupancy is held"
    assert motor.llm_generating is True, "the worker never touched the held occupancy flag"


def test_ac56_upgrade_worker_does_not_stamp_a_fresh_stash():
    """F6 (WARNING): a discard+new-freeze between spawn and completion must not
    land the old connector on the NEW stash — the worker writes only when the
    frozen stash is still the SAME object it was spawned for."""
    motor = _bare_motor()
    motor._prefetched_agenda = _agenda_draft()
    _speaking_agenda(motor, played=50, total=100, remaining=CUT_THRESHOLD_SECONDS + 5.0)
    motor.ptt_interrupt_if_agenda_speaking()
    motor._prefetched_agenda = None
    motor._preview_accept_agenda_output = lambda d: True

    original_stash = motor._frozen_stash

    def _swap_stash_then_return(*a, **kw):
        # Simulate a discard + new freeze happening DURING the worker's generation.
        with motor._prefetch_lock:
            motor._frozen_stash = {"payload": "AG3", "dialogo": "otro beat", "connector": None}
        # R4: the real _generar_dialogo self-brackets `_llm_generating` and is the
        # sole releaser; a faithful double must release it too (the worker's outer
        # finally no longer double-releases).
        with motor._lock:
            motor._llm_generating = False
        return "Bueno, volviendo a lo viejo,"

    motor._generar_dialogo = _swap_stash_then_return

    motor._maybe_generate_connector_upgrade()

    assert _wait_until(lambda: not motor.llm_generating, timeout=1.0)
    time.sleep(0.05)
    assert motor._frozen_stash.get("connector") is None, (
        "the stale connector must not land on the fresh stash"
    )
    assert original_stash.get("connector") is None, "the superseded stash was dropped, never written"


def test_ac56_upgrade_generates_into_frozen_stash_when_slot_is_free():
    motor = _bare_motor()
    motor._prefetched_agenda = _agenda_draft()
    _speaking_agenda(motor, played=50, total=100, remaining=CUT_THRESHOLD_SECONDS + 5.0)
    motor.ptt_interrupt_if_agenda_speaking()
    # Slot free (the answer already popped from cache), GPU free.
    motor._prefetched_agenda = None
    motor._preview_accept_agenda_output = lambda d: True
    motor._generar_dialogo = lambda *a, **kw: "Bueno, volviendo a lo nuestro,"

    motor._maybe_generate_connector_upgrade()

    assert _wait_until(lambda: motor._frozen_stash.get("connector") == "Bueno, volviendo a lo nuestro,"), (
        "with a free slot and GPU, the upgrade writes into the frozen stash connector field"
    )
    assert motor._prefetched_agenda is None, "the upgrade never occupies the real pregen slot"


def test_ac56_upgrade_rejected_by_guardrail_leaves_floor_fallback():
    motor = _bare_motor()
    motor._prefetched_agenda = _agenda_draft()
    _speaking_agenda(motor, played=50, total=100, remaining=CUT_THRESHOLD_SECONDS + 5.0)
    motor.ptt_interrupt_if_agenda_speaking()
    motor._prefetched_agenda = None
    motor._preview_accept_agenda_output = lambda d: False  # rejected

    def _reject(*a, **kw):
        # R4: mimic the real _generar_dialogo's own bracket — it releases the
        # single-runner flag; the worker's outer finally no longer does.
        with motor._lock:
            motor._llm_generating = False
        return "linea rechazada"

    motor._generar_dialogo = _reject

    motor._maybe_generate_connector_upgrade()

    # The worker ran but the guardrail rejected -> no connector stored -> floor at return.
    assert _wait_until(lambda: not motor.llm_generating, timeout=1.0)
    time.sleep(0.05)
    assert motor._frozen_stash.get("connector") is None, "a rejected upgrade never lands -> floor fallback"


def test_upgrade_triggered_at_speaking_start_only_for_non_agenda_turns():
    """The upgrade fires during PLAYBACK (speaking_start), never before the
    turn's own generation — and only for the interruption answer (non-agenda)."""
    motor = _bare_motor()
    calls = []
    motor._maybe_generate_connector_upgrade = lambda: calls.append(1)

    # A non-agenda answer starts playing -> upgrade triggered.
    motor._hablar_impl("", source="ptt")
    assert calls == [1], "the connector upgrade fires at the answer's speaking_start"

    # The resumed agenda turn (or any agenda turn) never triggers the upgrade.
    motor._hablar_impl("", source="kira-agenda")
    assert calls == [1], "an agenda turn must not trigger the connector upgrade"


# ══════════════════════════════════════════════════════════════════════════
# R3 — connector upgrade bounded so it never races the real turn
# ══════════════════════════════════════════════════════════════════════════


def test_r3_spawn_gate_skips_below_remaining_estimate_threshold():
    """R3(a): the cosmetic upgrade must not even spawn when the remaining-playback
    estimate is unknown or below CONNECTOR_UPGRADE_MIN_REMAINING_SECONDS — a short
    window risks the generation outliving playback and racing the real turn."""
    from opencohost.config.settings import CONNECTOR_UPGRADE_MIN_REMAINING_SECONDS

    motor = _bare_motor()
    motor._prefetched_agenda = _agenda_draft()
    _speaking_agenda(motor, played=50, total=100, remaining=CUT_THRESHOLD_SECONDS + 5.0)
    motor.ptt_interrupt_if_agenda_speaking()
    motor._prefetched_agenda = None
    motor._preview_accept_agenda_output = lambda d: True
    gen = []
    motor._generar_dialogo = lambda *a, **kw: gen.append(1) or "connector line"

    # Below-threshold estimate -> no worker, no LLM call.
    motor.speech_remaining_estimate = lambda: CONNECTOR_UPGRADE_MIN_REMAINING_SECONDS - 1.0
    motor._maybe_generate_connector_upgrade()
    time.sleep(0.05)
    assert gen == [], "no upgrade spawns below the remaining-estimate threshold"
    assert motor._frozen_stash.get("connector") is None

    # Unknown estimate (None) -> also skip (the floor stands).
    motor.speech_remaining_estimate = lambda: None
    motor._maybe_generate_connector_upgrade()
    time.sleep(0.05)
    assert gen == [], "no upgrade spawns when the remaining estimate is unknown"
    assert motor._frozen_stash.get("connector") is None


def test_r3_bounded_timeout_abandons_upgrade_without_stall_recovery(monkeypatch):
    """R3(b): a connector generation that exceeds the bound abandons SILENTLY —
    it returns empty (floor stands), releases the occupancy flag, raises nothing,
    and must NEVER trigger the heavyweight stall recovery a real turn's timeout
    does (it is cosmetic)."""
    motor = _bare_motor()
    # Avoid any real ollama.show / capability probe (offline determinism).
    motor._model_ctx_limit = {motor.current_model: 4096}
    monkeypatch.setattr(motor, "_resolve_reasoning_classification", lambda m: False)
    recovered = []
    monkeypatch.setattr(
        motor, "_recover_from_stalled_inference", lambda **kw: recovered.append(1)
    )
    release = threading.Event()

    def _blocking_chat(**kwargs):
        release.wait(2.0)  # outlive the 0.1s watchdog bound
        return {"message": {"content": "late line"}}

    motor._ollama_chat = _blocking_chat

    out = motor._generar_dialogo(
        "hola", source="kira-agenda", commit_history=False, watchdog_timeout=0.1
    )

    assert out == "", "a bounded timeout returns empty -> the pool floor stands"
    assert recovered == [], "a cosmetic connector timeout must NOT trigger stall recovery"
    assert motor.llm_generating is False, "the occupancy flag is released on timeout"
    release.set()


# ══════════════════════════════════════════════════════════════════════════
# R4 — the upgrade worker never double-releases / clobbers the occupancy flag
# ══════════════════════════════════════════════════════════════════════════


def test_r4_worker_exit_does_not_clobber_a_third_party_claim():
    """R4: _generar_dialogo self-brackets `_llm_generating` and is the sole
    releaser once it runs. If a THIRD party claims the runner in the window after
    _generar_dialogo returns, the worker's own exit must NOT clear it (which would
    read False during a live Ollama call and misfire the WU3 GPU-free predicate)."""
    motor = _bare_motor()
    motor._prefetched_agenda = _agenda_draft()
    _speaking_agenda(motor, played=50, total=100, remaining=CUT_THRESHOLD_SECONDS + 5.0)
    motor.ptt_interrupt_if_agenda_speaking()
    motor._prefetched_agenda = None
    motor._preview_accept_agenda_output = lambda d: True

    def _gen_then_third_party_claims(*a, **kw):
        # Mimic _generar_dialogo's own bracket: it clears the flag on completion...
        with motor._lock:
            motor._llm_generating = False
        # ...then a THIRD party claims the single runner in the release window.
        with motor._lock:
            motor._llm_generating = True
        return "connector line"

    motor._generar_dialogo = _gen_then_third_party_claims

    motor._maybe_generate_connector_upgrade()

    assert _wait_until(
        lambda: motor._frozen_stash.get("connector") == "connector line"
    ), "the worker completed and landed its connector"
    time.sleep(0.05)
    assert motor.llm_generating is True, (
        "the worker's exit must not clobber the third party's fresh claim"
    )


# ══════════════════════════════════════════════════════════════════════════
# Driver return orchestration (topic skip, mirror, requeue)
# ══════════════════════════════════════════════════════════════════════════


def _driver_env():
    from opencohost.api.agenda_driver import AgendaDriver, PrefetchState
    from tests.test_agenda_driver import FakeMotor
    from opencohost.smart_aggregator.kira_agenda_controller import AgendaState

    motor = FakeMotor()
    lock = threading.Lock()
    driver = AgendaDriver(get_agenda=lambda: driver._agenda, get_motor=lambda: motor, agenda_lock=lock)
    return driver, motor, PrefetchState, AgendaState


def test_driver_return_requeues_frozen_stash_at_a_clean_boundary():
    driver, motor, PrefetchState, AgendaState = _driver_env()
    topic = SimpleNamespace(id="t1", title="modelos de lenguaje")
    agenda = SimpleNamespace(
        active_topic=topic,
        state=AgendaState.WAITING_SIGNAL,
        start_prefetched_action=lambda action: True,
    )
    driver._agenda = agenda
    action = SimpleNamespace(kind="enqueue", prompt="AG2", source="kira-agenda", priority=2, history_text=None)
    driver._prefetch = PrefetchState(action=action, topic_id="t1")

    motor.has_frozen = True
    motor.frozen_key = ("AG2", "kira-agenda", 2)
    motor.detour_has_started = True  # F1: the interruption answer already committed

    handled = driver._maybe_return_frozen_stash(agenda, motor)

    assert handled is True, "a pending frozen return handles the tick"
    assert motor.restored_tema == "modelos de lenguaje", "the driver passes the live topic title as {tema}"
    assert any(r["payload"] == "AG2" for r in motor.replaced), "the frozen agenda item is requeued for the return"
    assert driver._prefetch is None


def test_driver_return_holds_until_the_interruption_answer_turn_completes():
    """F1 (BLOCKER): the return must NOT fire before the interruption answer is
    spoken. The answer rides the command queue (invisible to the return gates), so
    a speaking_end from the cut itself must HOLD until the detour counter shows at
    least one interactive turn committed."""
    driver, motor, PrefetchState, AgendaState = _driver_env()
    topic = SimpleNamespace(id="t1", title="modelos de lenguaje")
    agenda = SimpleNamespace(
        active_topic=topic,
        state=AgendaState.WAITING_SIGNAL,
        start_prefetched_action=lambda action: True,
    )
    driver._agenda = agenda
    action = SimpleNamespace(kind="enqueue", prompt="AG2", source="kira-agenda", priority=2, history_text=None)
    driver._prefetch = PrefetchState(action=action, topic_id="t1")

    motor.has_frozen = True
    motor.frozen_key = ("AG2", "kira-agenda", 2)
    # No interactive turn has committed yet (the answer is still on the command
    # queue) -> the return must HOLD, not fire.
    motor.detour_has_started = False

    handled = driver._maybe_return_frozen_stash(agenda, motor)

    assert handled is True, "the tick is HELD so no fresh turn generates over the pending return"
    assert motor.replaced == [], "the return must NOT requeue before the answer turn completes"
    assert motor.restored_tema is None, "restore is not called before the answer turn completes"
    assert motor.has_frozen is True, "the stash stays frozen while the answer is pending"

    # The answer turn commits (the engine's detour counter increments) -> now fire.
    motor.detour_has_started = True

    handled = driver._maybe_return_frozen_stash(agenda, motor)

    assert handled is True
    assert any(r["payload"] == "AG2" for r in motor.replaced), "the return fires once the answer turn completed"


def test_driver_return_adoption_failure_leaves_stash_frozen():
    """F7 (WARNING): if adoption fails, the frozen stash must NOT be restored into
    the active slot (which would orphan the draft) — leave it frozen so a later
    nudge retries or a deterministic skip discards it."""
    driver, motor, PrefetchState, AgendaState = _driver_env()
    topic = SimpleNamespace(id="t1", title="modelos de lenguaje")
    agenda = SimpleNamespace(
        active_topic=topic,
        state=AgendaState.WAITING_SIGNAL,
        start_prefetched_action=lambda action: False,  # adoption fails (stale)
    )
    driver._agenda = agenda
    action = SimpleNamespace(kind="enqueue", prompt="AG2", source="kira-agenda", priority=2, history_text=None)
    driver._prefetch = PrefetchState(action=action, topic_id="t1")

    motor.has_frozen = True
    motor.frozen_key = ("AG2", "kira-agenda", 2)
    motor.detour_has_started = True

    handled = driver._maybe_return_frozen_stash(agenda, motor)

    assert handled is True, "a failed adoption HOLDS the tick (no fresh turn over the pending return)"
    assert motor.restored_tema is None, "restore is never called when adoption fails (no orphan in the slot)"
    assert motor.replaced == [], "nothing is requeued on a failed adoption"
    assert motor.has_frozen is True, "the stash stays frozen for a later retry"
    assert motor.discarded is False, "a failed adoption does not discard the stash"


def test_driver_return_topic_changed_skips_and_discards():
    driver, motor, PrefetchState, AgendaState = _driver_env()
    # Topic swapped under us -> D2 topic skip.
    topic = SimpleNamespace(id="t2", title="otro tema")
    agenda = SimpleNamespace(active_topic=topic, state=AgendaState.WAITING_SIGNAL)
    driver._agenda = agenda
    action = SimpleNamespace(kind="enqueue", prompt="AG2", source="kira-agenda", priority=2, history_text=None)
    driver._prefetch = PrefetchState(action=action, topic_id="t1")  # stash pinned to the OLD topic

    motor.has_frozen = True

    handled = driver._maybe_return_frozen_stash(agenda, motor)

    assert handled is False, "a topic change falls through to normal next_action"
    assert motor.discarded is True, "the stale frozen stash is discarded on a topic change"
    assert driver._prefetch is None


def test_driver_no_frozen_stash_is_a_noop():
    driver, motor, PrefetchState, AgendaState = _driver_env()
    agenda = SimpleNamespace(active_topic=SimpleNamespace(id="t1", title="x"), state=AgendaState.WAITING_SIGNAL)
    driver._agenda = agenda
    motor.has_frozen = False

    assert driver._maybe_return_frozen_stash(agenda, motor) is False


def _pending_return_env(state_attr):
    """A driver + fake motor with a frozen return pending, held before the
    interruption answer has committed a detour turn."""
    driver, motor, PrefetchState, AgendaState = _driver_env()
    topic = SimpleNamespace(id="t1", title="modelos de lenguaje")
    agenda = SimpleNamespace(
        active_topic=topic,
        state=getattr(AgendaState, state_attr),
        start_prefetched_action=lambda action: True,
    )
    driver._agenda = agenda
    action = SimpleNamespace(kind="enqueue", prompt="AG2", source="kira-agenda", priority=2, history_text=None)
    driver._prefetch = PrefetchState(action=action, topic_id="t1")
    motor.has_frozen = True
    motor.frozen_key = ("AG2", "kira-agenda", 2)
    motor.detour_has_started = False  # the answer is still pending -> the return HOLDS
    return driver, motor, agenda


def test_driver_hold_within_deadline_holds_the_return():
    """R1: while the interruption answer is still pending and the stash is younger
    than FROZEN_STASH_MAX_HOLD_SECONDS, the return HOLDS (no fresh turn, no discard)."""
    driver, motor, agenda = _pending_return_env("WAITING_SIGNAL")
    motor.frozen_age = FROZEN_STASH_MAX_HOLD_SECONDS - 1.0  # within the deadline

    handled = driver._maybe_return_frozen_stash(agenda, motor)

    assert handled is True, "within the deadline the return still HOLDS"
    assert motor.discarded is False, "the stash is not discarded before the deadline"
    assert motor.replaced == [], "no fresh turn is generated while the return holds"


def test_driver_hold_past_deadline_discards_and_falls_through(caplog):
    """R1: once the stash has been held past FROZEN_STASH_MAX_HOLD_SECONDS with the
    interruption answer never committing a detour turn (lost answer), discard the
    stash and fall through to normal next_action — the agenda must not stay silent
    forever. A code-only INFO line is logged (never dialogue content)."""
    driver, motor, agenda = _pending_return_env("WAITING_SIGNAL")
    motor.frozen_age = FROZEN_STASH_MAX_HOLD_SECONDS + 1.0  # past the deadline

    caplog.set_level(logging.INFO)
    handled = driver._maybe_return_frozen_stash(agenda, motor)

    assert handled is False, "past the deadline the return falls through to next_action"
    assert motor.discarded is True, "the stale stash is discarded via the discard path"
    assert driver._prefetch is None, "the driver mirror is reset with the stash"
    assert any("hold_timeout" in r.getMessage() for r in caplog.records), (
        "a code-only telemetry line is logged"
    )


def test_driver_return_yields_to_paused_needs_operator():
    """R2 (focused): a frozen return must YIELD (return False, no discard) when the
    controller is PAUSED_NEEDS_OPERATOR so the normal tick can reach can_auto_resume
    — else a health pause after a PTT cut could never auto-recover. The stash stays
    frozen and the driver mirror is kept for the later return."""
    driver, motor, agenda = _pending_return_env("PAUSED_NEEDS_OPERATOR")

    handled = driver._maybe_return_frozen_stash(agenda, motor)

    assert handled is False, "PAUSED_NEEDS_OPERATOR yields so the tick reaches auto-resume"
    assert motor.discarded is False, "the stash is NOT discarded — it survives recovery"
    assert motor.has_frozen is True
    assert driver._prefetch is not None, "the driver mirror is kept for the later return"


def test_driver_return_lost_restore_after_adoption_enqueues_fresh_turn(caplog):
    """R5 (F7 orphan race): if _invalidate_frozen_stash lands between the top
    has_frozen_stash() check and restore() — AFTER adoption already advanced the
    controller to GENERATING — restore() returns None. The driver must enqueue a
    FRESH turn for the adopted action (regenerate from scratch) instead of leaving
    the GENERATING controller with no turn enqueued (stuck)."""
    driver, motor, agenda = _pending_return_env("WAITING_SIGNAL")
    motor.detour_has_started = True  # the answer committed -> the return may fire
    # A concurrent invalidation lands between adoption and restore: restore() lost
    # the draft even though has_frozen_stash() said True at the top of the tick.
    motor.restore_frozen_stash = lambda tema="": None

    caplog.set_level(logging.INFO)
    handled = driver._maybe_return_frozen_stash(agenda, motor)

    assert handled is True, "a lost restore after adoption still handles the tick"
    assert any(r["payload"] == "AG2" for r in motor.replaced), (
        "a fresh turn is enqueued for the adopted action (never stuck)"
    )
    assert driver._prefetch is None, "the driver mirror is cleared after the fallback enqueue"
    assert any("restore_lost_fallback" in r.getMessage() for r in caplog.records), (
        "a code-only telemetry line is logged"
    )


# ══════════════════════════════════════════════════════════════════════════
# ptt_session precheck hook (the cut trigger surface) + engine_host flag
# ══════════════════════════════════════════════════════════════════════════


def test_ptt_controller_fires_flush_precheck_before_dispatch():
    from opencohost.api.ptt_session import PttController

    order = []

    class _Disp:
        def dispatch(self, *a, **kw):
            order.append("dispatch")

    ctrl = PttController(
        "ws://x",
        _Disp(),
        SimpleNamespace(record=lambda *a, **kw: None),
        on_flush_precheck=lambda: order.append("precheck"),
    )
    ctrl._dispatch("hola kira que tal")

    assert order == ["precheck", "dispatch"], "the cut precheck fires BEFORE the turn is dispatched"


def test_ptt_controller_precheck_none_is_a_safe_noop():
    from opencohost.api.ptt_session import PttController

    dispatched = []

    class _Disp:
        def dispatch(self, *a, **kw):
            dispatched.append(a)

    ctrl = PttController("ws://x", _Disp(), SimpleNamespace(record=lambda *a, **kw: None))
    ctrl._dispatch("hola kira que tal")  # no precheck wired

    assert dispatched, "with no precheck the flush still dispatches the turn (typed/CTK parity)"


def test_ptt_controller_precheck_failure_never_blocks_dispatch():
    from opencohost.api.ptt_session import PttController

    dispatched = []

    class _Disp:
        def dispatch(self, *a, **kw):
            dispatched.append(a)

    def _boom():
        raise RuntimeError("boom")

    ctrl = PttController(
        "ws://x", _Disp(), SimpleNamespace(record=lambda *a, **kw: None), on_flush_precheck=_boom
    )
    ctrl._dispatch("hola kira que tal")

    assert dispatched, "a raising precheck must never block the turn from dispatching"


def test_main_wires_ptt_precheck_to_engine_bound_method():
    """F8c: the PttController must receive the engine's real
    ptt_interrupt_if_agenda_speaking bound method via main.py's getattr chain —
    cheap smoke against a real engine instance, no server spin-up."""
    from opencohost.api.ptt_session import PttController

    motor = _bare_motor()
    host = SimpleNamespace(motor=motor)
    # Replicate main.py's exact wiring idiom.
    precheck = getattr(getattr(host, "motor", None), "ptt_interrupt_if_agenda_speaking", None)

    class _Disp:
        def dispatch(self, *a, **kw):
            pass

    ctrl = PttController(
        "ws://x", _Disp(), SimpleNamespace(record=lambda *a, **kw: None), on_flush_precheck=precheck
    )

    assert ctrl._on_flush_precheck is not None, "the precheck resolved to a real callable"
    assert ctrl._on_flush_precheck == motor.ptt_interrupt_if_agenda_speaking, (
        "the controller received the engine's own bound cut method"
    )
    # The default engine flag is OFF, so the bound method is a safe no-op ("off").
    assert ctrl._on_flush_precheck() == "off"


def test_main_wires_the_press_hook_to_the_real_pause():
    """Step 3 (interruptible_speech_architecture_20260804 §5.1 resolution
    3c): a getattr typo here would silently disarm the whole feature with
    every OTHER test still green -- main.py must resolve `on_press_precheck`
    to `motor.pause_speech_for_ptt` and `on_release` to
    `motor.resume_speech_after_ptt`, mirroring
    test_main_wires_ptt_precheck_to_engine_bound_method above. A minimal
    host double without `motor` must still construct (getattr fallback)."""
    # Closure (vacuous-test finding, 2026-08-05): the old version REPLICATED
    # main.py's getattr idiom inline and asserted on its own local copy — a
    # typo in main.py itself stayed green while silently disarming the whole
    # PTT interruption path. This imports main's REAL wiring function.
    from opencohost.api.main import _ptt_controller_hooks
    from opencohost.api.ptt_session import PttController

    motor = _bare_motor()
    host = SimpleNamespace(motor=motor)
    hooks = _ptt_controller_hooks(host)

    assert hooks["on_press_precheck"] == motor.pause_speech_for_ptt, (
        "main.py must resolve on_press_precheck to the engine's bound pause"
    )
    assert hooks["on_release"] == motor.resume_speech_after_ptt, (
        "main.py must resolve on_release to the engine's bound resume"
    )
    assert hooks["motor"] is motor

    class _Disp:
        def dispatch(self, *a, **kw):
            pass

    ctrl = PttController(
        "ws://x", _Disp(), SimpleNamespace(record=lambda *a, **kw: None),
        on_press_precheck=hooks["on_press_precheck"],
        on_release=hooks["on_release"],
    )
    # The default engine flag is OFF (step-3 kill switch) -- both are safe
    # no-ops with no router built.
    ctrl._on_press_precheck()
    ctrl._on_release()
    assert motor._speech_router is None, "the kill switch being off must build no router"

    # A minimal host double without `motor` must still resolve (to None) and
    # construct — never break app startup.
    bare_hooks = _ptt_controller_hooks(SimpleNamespace())
    assert bare_hooks["on_press_precheck"] is None
    assert bare_hooks["on_release"] is None
    assert bare_hooks["motor"] is None
    PttController(
        "ws://x", _Disp(), SimpleNamespace(record=lambda *a, **kw: None),
        on_press_precheck=bare_hooks["on_press_precheck"],
        on_release=bare_hooks["on_release"],
    )


def test_flush_seam_is_unwired_only_when_the_stack_is_armed():
    """Judge closure (2026-08-05, MAJOR, judge B): with the speech stack
    armed, BOTH seams fired for one press — PTT_DOWN suspended the agenda
    turn losslessly, PTT_UP resumed it as filler, then grace expiry ran the
    WU5 flush precheck whose "cut" is a BARE `interrupt_speaking()`:
    reconcile saw a cut with no pause request and DISCARDED the resumed
    tail (probe: 3 of 4 fragments lost on the ordinary single-press turn).
    The press seam plus D3 preemption when the answer submits supersede the
    flush-time position cut, so main.py wires `on_flush_precheck` only
    while the stack is NOT armed — either switch off keeps the legacy
    wiring byte-identical."""
    from opencohost.api.main import _ptt_controller_hooks

    motor = _bare_motor()
    host = SimpleNamespace(motor=motor)

    # Both switches off (CTK / full revert): legacy wiring, untouched.
    hooks = _ptt_controller_hooks(host)
    assert hooks["on_flush_precheck"] == motor.ptt_interrupt_if_agenda_speaking

    # Router without step 3: the flush seam is still the only cut there is.
    motor._speech_router_enabled = True
    hooks = _ptt_controller_hooks(host)
    assert hooks["on_flush_precheck"] == motor.ptt_interrupt_if_agenda_speaking

    # Stack armed (both switches): the press seam supersedes the flush cut.
    motor._speech_interrupt_enabled = True
    hooks = _ptt_controller_hooks(host)
    assert hooks["on_flush_precheck"] is None, (
        "an armed stack must unwire the WU5 flush cut — it discards the "
        "resumed filler the press seam just preserved"
    )
    # The rest of the wiring is untouched by the gate.
    assert hooks["on_press_precheck"] == motor.pause_speech_for_ptt
    assert hooks["on_release"] == motor.resume_speech_after_ptt
    assert hooks["motor"] is motor
