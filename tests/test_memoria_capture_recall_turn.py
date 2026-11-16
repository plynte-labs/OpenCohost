"""T7 (memory-grounding-and-recall-budget-20261016): user-only drafts on recall turns.

Owner decision M2: on a recall turn (an explicit question about earlier
conversation) the draft is built from the USER side only. Kira's sentence on
such a turn is an answer from (possibly absent) memory, so it must not become
the content, title, signature or stable key of a stored memory. The turn stays
capturable (the deliberate no-veto decision in _build_memoria_draft still
holds). owner-only-drafts (2026-10-18) extends this to every turn.
"""
from __future__ import annotations

import sqlite3

import pytest

import opencohost.core.llm_engine as llm_engine
from opencohost.i18n import active as i18n_active
from tests.test_memorias_capture_engine import _enable_memorias, _make_motor

RECALL_USER = "te acuerdas del tesla model tres que comentamos la sesión pasada"
KIRA_INVENTION = "Claro, tienes un Tesla Model 3 High Performance rojo con ocho mil kilómetros."
NORMAL_USER = "mi cumpleaños es el tres de mayo y trabajo como diseñador"
NORMAL_KIRA = "Anotado, tres de mayo y diseñador, buen oficio."


@pytest.fixture(autouse=True)
def _es_locale():
    from opencohost.i18n.startup import resolve_active_bundle

    i18n_active.set_active_bundle(resolve_active_bundle(locale="es"))
    yield
    i18n_active.reset_active_bundle()


def _draft(motor, user, asst, source="direct"):
    return motor._build_memoria_draft(user, asst, source=source, private=False)


def _motor(monkeypatch, tmp_path):
    motor, _, _ = _make_motor()
    _enable_memorias(monkeypatch, motor, tmp_path)
    return motor


def test_recall_turn_draft_has_no_kira_side(monkeypatch, tmp_path):
    motor = _motor(monkeypatch, tmp_path)

    profile_id, stable_key, title, content, signature = _draft(motor, RECALL_USER, KIRA_INVENTION)

    assert i18n_active.ledger_kira_label() not in content
    assert "→" not in content
    for text in (content, title, signature, stable_key):
        assert "performance" not in text.lower() and "kilometros" not in text.lower()
    assert "tesla" in content.lower()  # the owner's own words are kept


def test_recall_turn_keys_ignore_the_kira_side(monkeypatch, tmp_path):
    motor = _motor(monkeypatch, tmp_path)
    a = _draft(motor, RECALL_USER, KIRA_INVENTION)
    b = _draft(motor, RECALL_USER, "Otra respuesta totalmente distinta sobre motores eléctricos.")
    assert a == b


def test_normal_turn_draft_is_user_only(monkeypatch, tmp_path):
    """owner-only-drafts (2026-10-18): every draft is the owner's words alone.
    Pinned literals: key and signature never carried Kira tokens here, so
    they match the pre-change builder; content drops the Kira segment."""
    motor = _motor(monkeypatch, tmp_path)

    pid, key, title, content, signature = _draft(motor, NORMAL_USER, NORMAL_KIRA)

    assert pid == "profile-1"
    assert key == "profile-1|cumpleanos-es-mayo-mi-trabajo-tres"
    assert title == "mi cumpleanos es"
    assert content == "contexto: mi cumpleaños es el tres de mayo y trabajo como diseñador"
    assert signature == "mi cumpleanos es tres mayo trabajo como disenador"


def test_normal_turn_kira_fabrication_never_stored(monkeypatch, tmp_path):
    """Live 2026-10-17 22:26: on a normal (non-recall) turn Kira invented a CV
    and a web search, and both became drafts. Kira's side must not reach any
    field of the draft, and two different replies give the same draft."""
    motor = _motor(monkeypatch, tmp_path)
    user = "acabo de hacer mi curriculum para buscar trabajo de diseñador"
    a = _draft(motor, user, "Sí, busqué eso por ti y tu currículum tiene un diseño azul precioso.")
    b = _draft(motor, user, "Qué bien, mucha suerte con la búsqueda.")

    assert a == b
    for text in a[1:]:
        assert "azul" not in text.lower() and "busque" not in text.lower()
    assert i18n_active.ledger_kira_label() not in a[3]


def test_ptt_wrapped_recall_question_is_also_user_only(monkeypatch, tmp_path):
    motor = _motor(monkeypatch, tmp_path)
    wrapped = i18n_active.ptt_wrapper().format(text=RECALL_USER)

    content = _draft(motor, wrapped, KIRA_INVENTION, source="ptt")[3]

    assert i18n_active.ledger_kira_label() not in content


def test_explicit_request_to_remember_keeps_its_fact(monkeypatch, tmp_path):
    motor = _motor(monkeypatch, tmp_path)
    user = "quiero que recuerdes que mi cumpleaños es el 3 de mayo"

    content = _draft(motor, user, "Listo, 3 de mayo, no se me olvida.")[3]

    assert "3 de mayo" in content
    assert "no se me olvida" not in content


def test_recall_question_without_enough_user_signal_is_not_captured(monkeypatch, tmp_path):
    motor = _motor(monkeypatch, tmp_path)
    assert _draft(motor, "¿qué recuerdas?", KIRA_INVENTION) is None


def test_commit_path_stores_the_user_only_row(monkeypatch, tmp_path):
    motor = _motor(monkeypatch, tmp_path)

    motor._commit_history(RECALL_USER, KIRA_INVENTION, source="direct")

    conn = sqlite3.connect(str(tmp_path / "memorias.db"))
    try:
        rows = conn.execute("SELECT content, title, signature FROM memorias").fetchall()
    finally:
        conn.close()
    assert rows, "the recall turn stays capturable"
    for row in rows:
        assert "performance" not in " ".join(row).lower()
        assert i18n_active.ledger_kira_label() not in row[0]
