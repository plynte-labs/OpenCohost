"""Tests for slice 3 (capture-engine) — kira_memory_persistence_20260701.

Pins the eviction-capture hook wired into MotorVocalIA._commit_history:
gate chain (R1 provenance, R2 privacy switch, R3 distillation minimum, R4
session-scoped switch), thread/lock invariants (R5 wiring), and the RC-10
accepted agenda-append loss.

B1 note (memoria_quality_20260717): capture now also fires when a pair ENTERS
historial (capture-at-commit), not only at eviction. So eviction-gate tests use
a NON-capturable trigger commit ("hola"/"chau." — user side < 2 significant
tokens) to force the eviction while ensuring only the EVICTED pair under test
can produce a store row. A capturable trigger would add a second, unrelated row.

MEMORIAS_ENABLED defaults True as of slice 8. Tests that exercise
capture/flush/injection MUST monkeypatch `llm_engine.MEMORIAS_ENABLED`
(module-level name lookup, matching the SCOUT_ENABLED precedent in
test_topic_scout.py) AND redirect `llm_engine.MEMORIAS_DB` to a tmp path
(see the `_enable_memorias` helper) — NEVER let the real store be
instantiated against USER_DATA_DIR. Patching opencohost.config.settings
would NOT affect the name already bound into llm_engine's module namespace
at import time.
"""

import queue
import threading
from collections import deque
from unittest.mock import MagicMock

import pytest

import opencohost.core.llm_engine as llm_engine
from opencohost.config.settings import HISTORY_MAX_TURNS
from opencohost.core.memory.memoria_store import MemoriaStore
from opencohost.i18n import active as i18n_active


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_motor():
    """Construct a MotorVocalIA with all heavy I/O mocked out.

    Mirrors tests/test_pipeline_memory.py's _make_motor().
    """
    log_q = queue.Queue()
    ui_events = []

    def ui_cb(event):
        ui_events.append(event)

    motor = llm_engine.MotorVocalIA(log_q, ui_cb)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.is_ready = True
    return motor, log_q, ui_events


def _fill_history_to_max(motor, count=None):
    """Fill historial to full capacity with innocuous, untagged filler pairs.

    Mirrors tests/test_pipeline_memory.py's _fill_history_to_max(). These
    filler entries deliberately have no 'private' key — only historial[0]/[1]
    (overwritten per-test below) matter for the eviction-capture gate chain.
    """
    n = count if count is not None else HISTORY_MAX_TURNS
    for i in range(n):
        motor.historial.append({"role": "user", "content": f"user msg {i}", "source": "direct"})
        motor.historial.append(
            {"role": "assistant", "content": f"assistant reply {i}. This is a sentence.", "source": "direct"}
        )


def _enable_memorias(monkeypatch, motor, tmp_path, profile_id="profile-1"):
    """Flip MEMORIAS_ENABLED on and point the engine's lazy store at tmp_path."""
    monkeypatch.setattr(llm_engine, "MEMORIAS_ENABLED", True)
    monkeypatch.setattr(llm_engine, "MEMORIAS_DB", str(tmp_path / "memorias.db"))
    motor._current_profile_id = profile_id
    return motor


def _store(tmp_path) -> MemoriaStore:
    return MemoriaStore(tmp_path / "memorias.db")


# Vocab calibrated against the same domain-stopword list as slice 2
# (_MEMORIA_DOMAIN_STOPWORDS): "streamer"/"kira"/"dice" are filtered out,
# leaving >=3 significant tokens for eligible-pair tests.
_ELIGIBLE_USER = "streamer prefiere musica synthwave calma"
_ELIGIBLE_ASST = "Kira dice que es genial. Fin."


# ---------------------------------------------------------------------------
# R1 — provenance gate (3.1-3.3)
# ---------------------------------------------------------------------------

def test_eviction_capture_requires_direct_or_ptt_source_fail_closed(monkeypatch, tmp_path):
    """Missing/unrecognized source values on the evicted entry fail closed."""
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    _fill_history_to_max(motor)
    motor.historial[0] = {"role": "user", "content": _ELIGIBLE_USER, "private": False}  # no 'source' key
    motor.historial[1] = {"role": "assistant", "content": _ELIGIBLE_ASST, "private": False}
    motor._commit_history("hola", "chau.", source="direct")  # B1: non-capturable trigger

    assert _store(tmp_path).list_for_profile("profile-1") == []


@pytest.mark.parametrize("bad_source", ["chat", "accumulated", "unknown"])
def test_eviction_capture_skips_non_capturable_sources_chat_accumulated_unknown(monkeypatch, tmp_path, bad_source):
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    _fill_history_to_max(motor)
    motor.historial[0] = {"role": "user", "content": _ELIGIBLE_USER, "source": bad_source, "private": False}
    motor.historial[1] = {"role": "assistant", "content": _ELIGIBLE_ASST, "source": bad_source, "private": False}
    motor._commit_history("hola", "chau.", source="direct")  # B1: non-capturable trigger

    assert _store(tmp_path).list_for_profile("profile-1") == []


def test_eviction_capture_skips_agenda_sentinel_entries(monkeypatch, tmp_path):
    """Agenda-origin pairs (user slot = sentinel) never leak into memorias,
    even when source is tagged 'direct' — content-prefix belt check."""
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    _fill_history_to_max(motor)
    motor.historial[0] = {
        "role": "user",
        "content": "[agenda segura: prompt interno omitido]",
        "source": "direct",
        "private": False,
    }
    motor.historial[1] = {
        "role": "assistant",
        "content": "TOP SECRET AGENDA REPLY that must never leak.",
        "source": "direct",
        "private": False,
    }
    motor._commit_history("hola", "chau.", source="direct")  # B1: non-capturable trigger

    assert _store(tmp_path).list_for_profile("profile-1") == []


# ---------------------------------------------------------------------------
# R4 — session-scoped capture switch (3.4, 3.7)
# ---------------------------------------------------------------------------

def test_eviction_capture_skips_when_memorias_disabled_flag_false(monkeypatch, tmp_path):
    """MEMORIAS_ENABLED defaulted False through slice 7; slice 8 flips the
    production default to True, so this OFF-behavior test now patches it
    explicitly instead of relying on the old default."""
    monkeypatch.setattr(llm_engine, "MEMORIAS_ENABLED", False)
    motor, _, _ = _make_motor()
    motor._current_profile_id = "profile-1"
    # Deliberately NOT calling _enable_memorias — MEMORIAS_ENABLED forced False above.

    _fill_history_to_max(motor)
    motor.historial[0] = {"role": "user", "content": _ELIGIBLE_USER, "source": "direct", "private": False}
    motor.historial[1] = {"role": "assistant", "content": _ELIGIBLE_ASST, "source": "direct", "private": False}
    motor._commit_history("hola", "chau.", source="direct")  # B1: non-capturable trigger

    assert motor._memoria_store is None  # never instantiated — zero cost on the hot path
    assert _store(tmp_path).list_for_profile("profile-1") == []


def test_privacy_switch_is_session_scoped_single_global_toggle():
    """set_memorias_private is a single global flag — never per-profile."""
    motor, _, _ = _make_motor()
    assert motor._memorias_private is False  # default: capturing

    motor.set_memorias_private(True)
    motor._current_profile_id = "profile-A"
    motor._commit_history("hola", "hola de vuelta.", source="direct")
    assert motor.historial[-2]["private"] is True
    assert motor.historial[-1]["private"] is True

    # Switching the active profile does not reset or scope the toggle.
    motor._current_profile_id = "profile-B"
    motor._commit_history("hola de nuevo", "hola otra vez.", source="direct")
    assert motor.historial[-2]["private"] is True
    assert motor.historial[-1]["private"] is True


def test_memorias_private_property_reflects_current_switch_state():
    """Slice 7 UI read: the public memorias_private property mirrors
    _memorias_private without needing UI code to touch a private attr."""
    motor, _, _ = _make_motor()
    assert motor.memorias_private is False

    motor.set_memorias_private(True)
    assert motor.memorias_private is True

    motor.set_memorias_private(False)
    assert motor.memorias_private is False


# ---------------------------------------------------------------------------
# R2 — privacy switch, forward-only (3.5, 3.6)
# ---------------------------------------------------------------------------

def test_eviction_capture_skips_entry_tagged_private_at_append_time(monkeypatch, tmp_path):
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    _fill_history_to_max(motor)
    motor.historial[0] = {"role": "user", "content": _ELIGIBLE_USER, "source": "direct", "private": True}
    motor.historial[1] = {"role": "assistant", "content": _ELIGIBLE_ASST, "source": "direct", "private": True}
    motor._commit_history("hola", "chau.", source="direct")  # B1: non-capturable trigger

    assert _store(tmp_path).list_for_profile("profile-1") == []


def test_reenabling_capture_mid_session_never_retro_captures_private_window(monkeypatch, tmp_path):
    """A pair appended while paused stays uncapturable even after the switch
    is later flipped back to capturing (state-at-event, forward-only)."""
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    motor.set_memorias_private(True)  # pause
    for i in range(HISTORY_MAX_TURNS):
        motor._commit_history(
            f"{_ELIGIBLE_USER} {i}", f"{_ELIGIBLE_ASST} {i}", source="direct",
        )
    motor.set_memorias_private(False)  # resume

    # Evicts the first (paused, private=True) pair above — capture must still skip it.
    # B1: non-capturable trigger so only the evicted paused pair could produce a row.
    motor._commit_history("hola", "chau.", source="direct")

    assert _store(tmp_path).list_for_profile("profile-1") == []


def test_concurrent_privacy_flip_mid_append_tags_both_pair_entries_consistently(monkeypatch):
    """B-S1: set_memorias_private does not hold _history_lock, so a concurrent
    flip landing BETWEEN the user and assistant historial.append() calls must
    not split a pair's tag. The flag is snapshotted ONCE per turn and used for
    both appends — regardless of when the flip lands."""
    motor, _, _ = _make_motor()
    assert motor._memorias_private is False

    # Base collections.deque forbids per-instance attribute assignment, so a
    # thin subclass is swapped in to spy on append() calls (empty at this
    # point — nothing to migrate).
    class _SpyDeque(deque):
        pass

    spy_historial = _SpyDeque(maxlen=motor.historial.maxlen)
    calls = {"n": 0}

    def flipping_append(entry):
        calls["n"] += 1
        if calls["n"] == 1:
            # Simulate a concurrent set_memorias_private(True) call (e.g. from
            # the Tk UI thread) landing right after the user entry is built
            # but before the assistant entry is built.
            motor.set_memorias_private(True)
        return deque.append(spy_historial, entry)

    monkeypatch.setattr(spy_historial, "append", flipping_append)
    motor.historial = spy_historial

    motor._commit_history("hola", "hola de vuelta.", source="direct")

    assert calls["n"] == 2
    assert motor.historial[-2]["private"] == motor.historial[-1]["private"]


# ---------------------------------------------------------------------------
# R3 — auto-draft distillation (3.8, 3.9)
# ---------------------------------------------------------------------------

def test_pair_with_fewer_than_3_significant_tokens_produces_zero_rows(monkeypatch, tmp_path):
    """"kira obs streamer" (0 sig. tokens) + "dice musica" (1 sig. token) -> 1 < 3."""
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    _fill_history_to_max(motor)
    motor.historial[0] = {"role": "user", "content": "kira obs streamer", "source": "direct", "private": False}
    motor.historial[1] = {"role": "assistant", "content": "dice musica", "source": "direct", "private": False}
    motor._commit_history("hola", "chau.", source="direct")  # B1: non-capturable trigger

    assert _store(tmp_path).list_for_profile("profile-1") == []


def test_eligible_pair_meeting_token_minimum_creates_draft_with_nonempty_title_content(monkeypatch, tmp_path):
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    _fill_history_to_max(motor)
    motor.historial[0] = {"role": "user", "content": _ELIGIBLE_USER, "source": "direct", "private": False}
    motor.historial[1] = {"role": "assistant", "content": _ELIGIBLE_ASST, "source": "direct", "private": False}
    motor._commit_history("hola", "chau.", source="direct")  # B1: non-capturable trigger

    rows = _store(tmp_path).list_for_profile("profile-1")
    assert len(rows) == 1
    row = rows[0]
    assert row["profile_id"] == "profile-1"
    assert row["title"] != ""
    assert row["content"] != ""
    assert "contexto:" in row["content"]
    assert "→ Kira:" in row["content"]
    assert len(row["content"]) <= 300


def test_eviction_capture_stores_full_pair_signature(monkeypatch, tmp_path):
    """Candidate 2 (memoria_rag_followups_20260716): the captured draft row
    persists a retrieval signature built from the FULL user+assistant pair
    via build_signature — the scorer's input, wider than the 3-token title."""
    from opencohost.core.memory.memoria_store import build_signature

    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    _fill_history_to_max(motor)
    motor.historial[0] = {"role": "user", "content": _ELIGIBLE_USER, "source": "direct", "private": False}
    motor.historial[1] = {"role": "assistant", "content": _ELIGIBLE_ASST, "source": "direct", "private": False}
    motor._commit_history("hola", "chau.", source="direct")  # B1: non-capturable trigger

    rows = _store(tmp_path).list_for_profile("profile-1")
    assert len(rows) == 1
    expected = build_signature(f"{_ELIGIBLE_USER} {_ELIGIBLE_ASST}")
    assert expected != ""
    assert rows[0]["signature"] == expected


# ---------------------------------------------------------------------------
# C1 content shaping (memoria_quality_20260717) — _build_memoria_content
# ---------------------------------------------------------------------------

def test_content_uses_24word_user_budget_ledger_stays_8():
    """Memoria CONTENT carries a wider user budget (24 words) than the digest
    ledger line (still 8) — the two are decoupled."""
    motor, _, _ = _make_motor()
    user = " ".join(f"palabra{i:02d}" for i in range(1, 26))  # 25 words
    asst = "El streamer prefiere synthwave calmo posta. Fin."

    content = motor._build_memoria_content(user, asst)
    assert "palabra09" in content   # past the 8-word digest budget
    assert "palabra24" in content
    assert "palabra25" not in content  # capped at 24

    ledger = motor._build_ledger_line(user, asst)
    assert "palabra08" in ledger
    assert "palabra09" not in ledger  # ledger stays on the 8-word budget


# ---------------------------------------------------------------------------
# G3 — history-wrapper frame never taxes a content builder's word budget
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "wrapper",
    (i18n_active.typed_history_wrapper, i18n_active.ptt_history_wrapper),
    ids=("typed", "ptt"),
)
def test_wrapped_turn_spends_the_whole_word_budget_on_operator_words(wrapper):
    """Both content builders strip the history-wrapper frame before counting words.

    Judge trace: the operator types "el cargador de config sigue crasheando
    cuando cambio de perfil"; _commit_history stores the WRAPPED text, so the
    8-word ledger line spent 3 words on "El streamer escribió:" and dropped
    "crasheando cuando cambio de perfil" — the entire symptom — from the block
    later injected as <memoria_de_fondo>. The PTT frame is 4 words, so its
    version of the same loss is worse. The frame is provenance METADATA that the
    "contexto: ... → Kira: ..." format already encodes, and stable_key / title /
    signature are ALL derived from stripped text (memoria_store
    _significant_tokens), so leaving it in the body also made the row internally
    inconsistent: honest title, taxed content.
    """
    motor, _, _ = _make_motor()
    user = " ".join(f"palabra{i:02d}" for i in range(1, 26))  # 25 words
    wrapped = wrapper().format(text=user)
    asst = "Eso suena tremendo problema de configuracion. Fin."
    frame = wrapper().split("{text}")[0].strip()

    ledger = motor._build_ledger_line(wrapped, asst)
    assert "palabra08" in ledger        # full 8-word budget spent on the operator
    assert "palabra09" not in ledger
    assert frame not in ledger

    content = motor._build_memoria_content(wrapped, asst)
    assert "palabra24" in content       # full 24-word budget spent on the operator
    assert "palabra25" not in content
    assert frame not in content


def test_kira_side_is_first_contentful_sentence_strips_tic_list():
    """Kira side = first sentence with >=3 significant tokens, AFTER stripping a
    leading tic from the closed list {Mirá vos, / Mirá, / Che, / Posta,}."""
    motor, _, _ = _make_motor()
    user = "streamer prefiere musica synthwave calma"
    asst = "Mirá vos, buenísimo. El streamer juega shooter rápido intenso."

    content = motor._build_memoria_content(user, asst)
    assert "Mirá vos" not in content            # leading tic stripped
    assert "buenísimo" not in content           # low-content first sentence skipped
    assert "juega shooter rápido intenso" in content


def test_user_side_below_2_significant_tokens_skips_capture(monkeypatch, tmp_path):
    """C1 greeting gate: a pair whose USER turn carries < 2 significant tokens
    is never captured, even when Kira's reply is rich (evicted-pair path)."""
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    _fill_history_to_max(motor)
    motor.historial[0] = {"role": "user", "content": "hola kira", "source": "direct", "private": False}
    motor.historial[1] = {
        "role": "assistant",
        "content": "El streamer prefiere synthwave calmo posta. Fin.",
        "source": "direct", "private": False,
    }
    # neutral (non-capturable) trigger evicts the greeting pair above
    motor._commit_history("hola", "chau.", source="direct")

    assert _store(tmp_path).list_for_profile("profile-1") == []


def test_canned_guardrail_fallback_pair_skips_capture(monkeypatch, tmp_path):
    """C1 + D4 interplay: a pair whose Kira side is a canned guardrail-fallback
    line (D4 commits blocked exchanges to history) carries no memory signal and
    must never become a memoria."""
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    fallback = i18n_active.guardrail_fallback_lines()[0]
    _fill_history_to_max(motor)
    motor.historial[0] = {
        "role": "user", "content": "streamer prefiere musica synthwave calma",
        "source": "direct", "private": False,
    }
    motor.historial[1] = {"role": "assistant", "content": fallback, "source": "direct", "private": False}
    motor._commit_history("hola", "chau.", source="direct")

    assert _store(tmp_path).list_for_profile("profile-1") == []


def test_memory_flavored_guardrail_fallback_never_becomes_a_memoria(monkeypatch, tmp_path):
    """D4 + C1 interplay: the new memory-flavored fallback line is committed to
    history by D4 (commit-on-block) but must NEVER itself be captured — it would
    be a self-referential retrieval-poisoning row ("Kira: se me mezclan los
    recuerdos" recalled as a memory about memories)."""
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    memory_fallback = "Se me mezclan los recuerdos, preguntame en un rato."
    assert memory_fallback in i18n_active.guardrail_fallback_lines()  # the new 5th line

    _fill_history_to_max(motor)
    motor.historial[0] = {
        "role": "user", "content": "streamer prefiere musica synthwave calma",
        "source": "direct", "private": False,
    }
    motor.historial[1] = {"role": "assistant", "content": memory_fallback, "source": "direct", "private": False}
    motor._commit_history("hola", "chau.", source="direct")  # B1: non-capturable trigger

    assert _store(tmp_path).list_for_profile("profile-1") == []


def test_capture_fires_at_commit_not_only_at_eviction(monkeypatch, tmp_path):
    """B1 (memoria_quality_20260717): an eligible pair is captured the moment it
    ENTERS historial, not only when it is later evicted. Durability by
    construction — no dependence on a clean shutdown/eviction."""
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    # historial is empty → this commit evicts nothing.
    motor._commit_history(_ELIGIBLE_USER, _ELIGIBLE_ASST, source="direct")

    rows = _store(tmp_path).list_for_profile("profile-1")
    assert len(rows) == 1
    assert rows[0]["content"] != ""


def test_fresh_insert_emits_memoria_captured_ui_callback(monkeypatch, tmp_path):
    """E1 (memoria_quality_20260717): a FRESH memoria capture fires
    ui_callback("memoria_captured") exactly once. Re-committing the same pair
    (same stable_key -> a refresh/revision bump, not a new memoria) emits
    nothing more, so the chat-panel feed never announces a memoria Kira
    already saved."""
    motor, _, ui_events = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    # Empty history -> B1 capture-at-commit, no eviction. A brand-new memoria.
    motor._commit_history(_ELIGIBLE_USER, _ELIGIBLE_ASST, source="direct")
    assert ui_events.count("memoria_captured") == 1
    assert len(_store(tmp_path).list_for_profile("profile-1")) == 1

    # Re-commit the identical pair -> same stable_key -> refresh, not insert.
    motor._commit_history(_ELIGIBLE_USER, _ELIGIBLE_ASST, source="direct")
    assert ui_events.count("memoria_captured") == 1  # still one -- refresh is silent


def test_tic_only_kira_reply_stores_user_side_without_dangling_kira_label():
    """FIX3 (memoria_quality_20260717): when Kira's whole reply collapses to a
    leading tic ("Mirá vos,"), the contentful-sentence extractor yields nothing,
    so the memoria stores the user side ALONE — no dangling "→ Kira:" label."""
    motor, _, _ = _make_motor()
    user = "streamer prefiere musica synthwave calma"
    asst = "Mirá vos,"

    content = motor._build_memoria_content(user, asst)
    assert "synthwave" in content        # user side preserved
    assert "musica" in content
    assert "→ Kira" not in content       # no dangling Kira label
    assert "Kira:" not in content


def test_commit_captured_pair_not_reupserted_at_eviction(monkeypatch, tmp_path):
    """FIX2 (memoria_quality_20260717): a pair captured at commit-time (B1) is
    flagged; the later eviction of that same pair skips the byte-identical
    re-upsert (no revision bump / panel-order flapping)."""
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    upserts = []
    original = MemoriaStore.upsert_draft

    def spy(self, profile_id, stable_key, *args, **kwargs):
        upserts.append(stable_key)
        return original(self, profile_id, stable_key, *args, **kwargs)

    monkeypatch.setattr(MemoriaStore, "upsert_draft", spy)

    # Commit-time capture (B1): one upsert; the pair is flagged in historial.
    motor._commit_history(_ELIGIBLE_USER, _ELIGIBLE_ASST, source="direct")
    assert len(upserts) == 1
    committed_key = upserts[0]
    assert motor.historial[-2].get("mem_captured") is True  # user entry flagged

    # Drive the flagged pair to eviction with non-capturable filler commits.
    for _ in range(HISTORY_MAX_TURNS):
        motor._commit_history("hola", "chau.", source="direct")

    # No second upsert of the committed pair's key — eviction skipped it.
    assert upserts.count(committed_key) == 1


def test_commit_capture_failure_still_captured_at_eviction(monkeypatch, tmp_path):
    """FIX2 belt-and-braces: when the commit-time capture FAILS (fail-open), the
    pair is NOT flagged, so eviction retains its retry role and still captures
    it."""
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    original = MemoriaStore.upsert_draft
    calls = {"n": 0}

    def flaky(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("disk full (once)")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(MemoriaStore, "upsert_draft", flaky)

    # Commit-time capture raises once (fail-open) → pair NOT flagged.
    motor._commit_history(_ELIGIBLE_USER, _ELIGIBLE_ASST, source="direct")
    assert calls["n"] == 1
    assert motor.historial[-2].get("mem_captured") is not True  # not flagged after failure

    # Drive the unflagged pair to eviction → retry capture succeeds → 1 row.
    for _ in range(HISTORY_MAX_TURNS):
        motor._commit_history("hola", "chau.", source="direct")

    rows = _store(tmp_path).list_for_profile("profile-1")
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# R5 wiring — build-under-lock, upsert-after-release (3.10); thread invariant (3.11)
# ---------------------------------------------------------------------------

def test_draft_built_under_history_lock_upsert_called_after_lock_release(monkeypatch, tmp_path):
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    build_lock_state = {}
    capture_lock_state = {}

    original_build = motor._build_memoria_draft

    def spy_build(*args, **kwargs):
        build_lock_state["locked"] = motor._history_lock.locked()
        return original_build(*args, **kwargs)

    original_capture = motor._capture_memoria

    def spy_capture(*args, **kwargs):
        capture_lock_state["locked"] = motor._history_lock.locked()
        return original_capture(*args, **kwargs)

    monkeypatch.setattr(motor, "_build_memoria_draft", spy_build)
    monkeypatch.setattr(motor, "_capture_memoria", spy_capture)

    _fill_history_to_max(motor)
    motor.historial[0] = {"role": "user", "content": _ELIGIBLE_USER, "source": "direct", "private": False}
    motor.historial[1] = {"role": "assistant", "content": _ELIGIBLE_ASST, "source": "direct", "private": False}
    motor._commit_history("hola", "chau.", source="direct")  # B1: non-capturable trigger

    assert build_lock_state["locked"] is True
    assert capture_lock_state["locked"] is False


def test_capture_never_runs_on_tk_main_thread_and_never_with_history_lock_held(monkeypatch, tmp_path):
    """Residual-risk pin: steady-state upsert_draft only ever runs on
    _commit_history's real callers (engine worker loop / agenda speaker
    daemon), never the Tk main thread, and never with _history_lock held."""
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    recorded = {}
    original_upsert = MemoriaStore.upsert_draft

    def spy_upsert(self, *args, **kwargs):
        recorded["thread"] = threading.current_thread()
        recorded["lock_held"] = motor._history_lock.locked()
        return original_upsert(self, *args, **kwargs)

    monkeypatch.setattr(MemoriaStore, "upsert_draft", spy_upsert)

    _fill_history_to_max(motor)
    motor.historial[0] = {"role": "user", "content": _ELIGIBLE_USER, "source": "direct", "private": False}
    motor.historial[1] = {"role": "assistant", "content": _ELIGIBLE_ASST, "source": "direct", "private": False}

    worker = threading.Thread(
        target=motor._commit_history,
        args=("siguiente pregunta", "siguiente respuesta."),
        kwargs={"source": "direct"},
    )
    worker.start()
    worker.join(timeout=5)

    assert "thread" in recorded, "capture never ran"
    assert recorded["thread"] is worker
    assert recorded["thread"] is not threading.main_thread()
    assert recorded["lock_held"] is False


# ---------------------------------------------------------------------------
# RC-10 — agenda-append accepted loss (3.12)
# ---------------------------------------------------------------------------

def test_agenda_append_eviction_loss_is_currently_accepted_behavior(monkeypatch, tmp_path):
    # Si este test falla porque ahora sí captura correctamente, revisar el diseño antes de restaurarlo.
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    _fill_history_to_max(motor)
    motor.historial[0] = {"role": "user", "content": _ELIGIBLE_USER, "source": "direct", "private": False}
    motor.historial[1] = {"role": "assistant", "content": _ELIGIBLE_ASST, "source": "direct", "private": False}

    # An agenda-sourced commit skips the ENTIRE eviction-capture block (guarded
    # by `not source.startswith("kira-agenda")` on the INCOMING source, at the
    # top of _commit_history's critical section), so the otherwise-eligible
    # evicted pair above is silently dropped — accepted v1 loss (RC-10),
    # inherited unmodified from the pre-existing T1 digest eviction gate.
    motor._commit_history("agenda prompt", "respuesta de agenda.", source="kira-agenda")

    assert _store(tmp_path).list_for_profile("profile-1") == []


# ---------------------------------------------------------------------------
# RC-1 integration — no false merge across distinct evicted pairs (3.13)
# ---------------------------------------------------------------------------

def test_two_evicted_pairs_different_intent_shared_vocab_never_merge_into_one_row(monkeypatch, tmp_path):
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)

    _fill_history_to_max(motor)
    motor.historial[0] = {
        "role": "user", "content": "streamer prefiere musica synthwave calma",
        "source": "direct", "private": False,
    }
    motor.historial[1] = {
        "role": "assistant", "content": "Kira dice que es un tema tranquilo. Fin.",
        "source": "direct", "private": False,
    }
    motor._commit_history("hola", "chau.", source="direct")  # B1: non-capturable trigger

    motor.historial[0] = {
        "role": "user", "content": "streamer prefiere juego shooter rapido",
        "source": "direct", "private": False,
    }
    motor.historial[1] = {
        "role": "assistant", "content": "Kira dice que es un juego intenso. Fin.",
        "source": "direct", "private": False,
    }
    motor._commit_history("hola", "chau.", source="direct")  # B1: non-capturable trigger

    rows = _store(tmp_path).list_for_profile("profile-1")
    assert len(rows) == 2
    keys = {row["stable_key"] for row in rows}
    assert len(keys) == 2


# ---------------------------------------------------------------------------
# Lazy-store singleton thread-safety (memoria_rag_followups_20260716,
# 4R correction round) — _get_memoria_store must construct exactly once even
# when the switch-flush worker thread races the main thread's first use.
# ---------------------------------------------------------------------------

def test_get_memoria_store_constructs_exactly_once_under_racing_threads(monkeypatch, tmp_path):
    """Two threads released simultaneously into _get_memoria_store against a
    slow-constructing store: without the lock both pass the None-check and two
    stores are built (deterministic red — the barrier guarantees both threads
    are inside the window while the first construction sleeps). With the lock,
    exactly one construction happens and both threads share the instance."""
    import time

    motor, _, _ = _make_motor()
    monkeypatch.setattr(llm_engine, "MEMORIAS_DB", str(tmp_path / "memorias.db"))

    constructions = []  # list.append is atomic under the GIL

    class SlowStore:
        def __init__(self, db_path):
            constructions.append(db_path)
            time.sleep(0.2)  # hold the check-then-act window open

    monkeypatch.setattr(llm_engine, "MemoriaStore", SlowStore)

    barrier = threading.Barrier(2)
    results = []

    def race():
        barrier.wait()
        results.append(motor._get_memoria_store())

    threads = [threading.Thread(target=race) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(constructions) == 1
    assert len(results) == 2 and results[0] is results[1]
