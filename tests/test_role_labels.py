"""kira-natural-replies-20261111 A2: per-message role labels.

The current owner turn carries a generic owner role label instead of the
owner's nickname (the strongest name primer), and replayed chat-source user
entries carry a chat role label. Labels depend only on the stored entry, so
the prompt bytes stay stable across assembles.
"""
from __future__ import annotations

import copy

import pytest

from opencohost.core.context import prompt_assembler as pa
from opencohost.core.context.prompt_assembler import PromptContextAssembler
from opencohost.i18n import active as i18n_active

NICK = "Franguh"


class _Bundle:
    def __init__(self, code):
        self.code = code


def _history():
    return [
        {"role": "user", "content": "hola kira", "source": "direct"},
        {"role": "assistant", "content": "Hola, me alegra verte por aqui.", "source": "direct"},
        {"role": "user", "content": "que tal el stream", "source": "chat"},
        {"role": "assistant", "content": "Va muy bien, gracias por preguntar.", "source": "chat"},
    ]


@pytest.fixture(params=["es", "en"])
def lang(request, monkeypatch):
    monkeypatch.setattr(
        pa.personalization,
        "load_personalization",
        lambda locale=None: {"enabled": True, "nickname": NICK},
    )
    monkeypatch.setattr(i18n_active, "get_active_bundle", _make_bundle(request.param))
    return request.param


def _make_bundle(code):
    from opencohost.i18n.contract import TIER_OFFICIAL
    from opencohost.i18n.registry import load_bundle, official_locales_dir

    bundle = load_bundle(official_locales_dir() / code, TIER_OFFICIAL)
    return lambda: bundle


def _assemble(history, source="direct", **kw):
    return PromptContextAssembler(sanitize_history_fn=lambda t: t).assemble(
        "cuentame algo",
        source,
        system_prompt="Eres Kira.",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        history_snapshot=history,
        personalization_enabled=True,
        memorias_enabled=False,
        **kw,
    )


def _users(setup):
    return [m["content"] for m in setup.messages if m["role"] == "user"]


def test_label_helpers_exist_es_en(monkeypatch):
    for code, owner in (("es", "Dueño del stream"), ("en", "Stream owner")):
        monkeypatch.setattr(i18n_active, "get_active_bundle", _make_bundle(code))
        assert i18n_active.owner_role_label() == owner
        assert i18n_active.chat_role_label() == "Chat"
        line = i18n_active.owner_nickname_line(NICK)
        assert owner in line and NICK in line


def test_missing_slot_falls_back_to_builtin_english(monkeypatch):
    monkeypatch.setattr(i18n_active, "get_active_bundle", lambda: _Bundle("xx"))
    assert i18n_active.owner_role_label() == "Stream owner"
    assert i18n_active.chat_role_label() == "Chat"
    assert "Stream owner" in i18n_active.owner_nickname_line(NICK)


@pytest.mark.parametrize("source", sorted(pa.OWNER_POLICY_SOURCES))
def test_current_owner_turn_has_role_label_not_nickname(lang, source):
    setup = _assemble(_history(), source=source)
    last = _users(setup)[-1]
    role = i18n_active.owner_role_label()
    assert last.endswith(f"[{role}]: cuentame algo") or f"[{role}]: cuentame algo" in last
    assert f"[{NICK}]" not in last
    assert NICK not in last


def test_nickname_still_in_system_with_role_mapping(lang):
    setup = _assemble(_history())
    system = setup.messages[0]["content"]
    assert NICK in system
    role = i18n_active.owner_role_label()
    assert f"[{role}]" in system


def test_replayed_chat_entry_is_labelled(lang):
    setup = _assemble(_history())
    replayed = _users(setup)[:-1]
    chat = i18n_active.chat_role_label()
    assert any(u.startswith(f"[{chat}] ") and "que tal el stream" in u for u in replayed)


def test_replayed_owner_entry_gets_owner_label(lang):
    setup = _assemble(_history())
    replayed = _users(setup)[:-1]
    role = i18n_active.owner_role_label()
    assert any(u.startswith(f"[{role}] ") and "hola kira" in u for u in replayed)


def test_stored_history_unchanged(lang):
    history = _history()
    before = copy.deepcopy(history)
    _assemble(history)
    assert history == before


def test_bytes_stable_across_assembles(lang):
    history = _history()
    assert _assemble(history).messages == _assemble(history).messages


def test_viewer_prompt_unlabelled_and_unchanged(lang):
    setup = _assemble(_history(), source="chat")
    chat = i18n_active.chat_role_label()
    role = i18n_active.owner_role_label()
    for u in _users(setup):
        assert not u.startswith(f"[{chat}]")
        assert not u.startswith(f"[{role}]")
    assert _users(setup)[-1] == "cuentame algo"


def test_evicted_pairs_carry_stored_user_text(monkeypatch, lang):
    seen = []

    def _on_evicted(pairs, native_ctx, effective_ctx):
        seen.extend(pairs)

    history = []
    for i in range(8):
        history.append({"role": "user", "content": f"pregunta numero {i} del chat", "source": "chat"})
        history.append({"role": "assistant", "content": f"respuesta numero {i} larga y detallada.", "source": "chat"})
    PromptContextAssembler(sanitize_history_fn=lambda t: t).assemble(
        "cuentame algo",
        "direct",
        system_prompt="Eres Kira.",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        history_snapshot=history,
        personalization_enabled=True,
        memorias_enabled=False,
        native_ctx_resolver=lambda m: 512,
        effective_ctx_resolver=lambda m, n: 512,
        on_evicted_pairs=_on_evicted,
    )
    assert seen, "expected eviction at a tiny context"
    for u, _a in seen:
        assert not u["content"].startswith("[")
