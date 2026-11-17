"""clock-and-compact-polish-20261010 T3: compact-profile history.

Compact owner turns keep the owner's last messages plus only Kira's last
reply, truncated to about 200 characters, inside the 6-message cap. History
from before the most recent model switch is dropped: every committed history
entry carries the ``model`` that was active when it was committed, and a
compact turn keeps only the trailing run of entries from the current model
(entries without the stamp are kept). The full profile is unchanged.
"""
from __future__ import annotations

import pytest

from opencohost.config.settings import OWNER_BUNDLE_SOURCE
from opencohost.core.context import prompt_assembler as pa
from opencohost.core.context import prompt_profile as pp
from opencohost.i18n import active as i18n_active
from tests.test_decision_engine_wiring import _motor
from tests.test_warmth_prompt import PERSONAS, _activate


@pytest.fixture(autouse=True)
def _reset_active():
    i18n_active.reset_active_bundle()
    yield
    i18n_active.reset_active_bundle()


def _pairs(n, model=None, tag="", reply=None):
    out = []
    for i in range(n):
        u = {"role": "user", "content": f"{tag}pregunta {i}", "source": "direct"}
        a = {"role": "assistant", "content": reply or f"{tag}Respuesta {i}. Algo más.", "source": "direct"}
        if model is not None:
            u["model"] = a["model"] = model
        out += [u, a]
    return out


def _assemble(history, source="direct", profile="compact", model="llama3"):
    _activate("es")
    return pa.PromptContextAssembler().assemble(
        "hola", source, system_prompt=PERSONAS["es"], use_system_role=True,
        is_local=True, provider_cfg={}, request_model=model,
        history_snapshot=history, personalization_enabled=False, memorias_enabled=False,
        prompt_profile=profile,
    )


def _hist(setup) -> list[dict]:
    return list(setup.messages)[1:-1]


# --- pure helpers ---------------------------------------------------------------


def test_since_last_switch_keeps_the_trailing_current_model_run():
    history = _pairs(2, "gemma", "g") + _pairs(1, "llama3", "a") + _pairs(1, "gemma", "h") + _pairs(2, "llama3", "b")
    kept = pp.since_last_switch(history, "llama3")
    assert [m["content"] for m in kept] == [m["content"] for m in _pairs(2, "llama3", "b")]


def test_since_last_switch_keeps_unstamped_entries():
    history = _pairs(2)
    assert pp.since_last_switch(history, "llama3") == history
    assert pp.since_last_switch(_pairs(1, "gemma") + _pairs(1), "llama3") == _pairs(1)


def test_since_last_switch_without_a_model_is_unchanged():
    history = _pairs(1, "gemma")
    assert pp.since_last_switch(history, "") == history


def test_truncate_reply():
    assert pp.truncate_reply("corto") == "corto"
    long = "palabra " * 60
    out = pp.truncate_reply(long)
    assert len(out) <= pp.COMPACT_LAST_REPLY_MAX_CHARS + 1
    assert out.endswith("…")
    assert long.startswith(out[:-1])


# --- assembler: compact owner history -------------------------------------------


@pytest.mark.parametrize("source", ["direct", "ptt", OWNER_BUNDLE_SOURCE])
def test_compact_owner_history_is_owner_messages_plus_last_reply(source):
    hist = _hist(_assemble(_pairs(11), source))
    assert len(hist) == pp.COMPACT_HISTORY_MAX_MESSAGES
    assert [m["role"] for m in hist] == ["user"] * 5 + ["assistant"]
    assert [m["content"] for m in hist] == [
        "pregunta 6", "pregunta 7", "pregunta 8", "pregunta 9", "pregunta 10", "Respuesta 10. Algo más.",
    ]


def test_compact_last_reply_is_truncated():
    long_reply = "Esto es una respuesta muy larga de Kira. " * 20
    hist = _hist(_assemble(_pairs(2, reply=long_reply)))
    assert hist[-1]["role"] == "assistant"
    assert len(hist[-1]["content"]) <= pp.COMPACT_LAST_REPLY_MAX_CHARS + 1
    assert [m["role"] for m in hist] == ["user", "user", "assistant"]


def test_compact_drops_history_from_before_the_switch():
    history = _pairs(3, "gemma4:e4b", "rust ") + _pairs(1, "llama3")
    hist = _hist(_assemble(history))
    assert [m["content"] for m in hist] == ["pregunta 0", "Respuesta 0. Algo más."]
    assert all("rust" not in m["content"] for m in hist)


def test_compact_right_after_a_switch_has_no_history():
    assert _hist(_assemble(_pairs(3, "gemma4:e4b"))) == []


def test_compact_chat_history_still_last_six_assistant_after_switch():
    history = _pairs(3, "gemma4:e4b", "g") + _pairs(8, "llama3")
    hist = _hist(_assemble(history, "chat"))
    assert len(hist) == 6 and all(m["role"] == "assistant" for m in hist)
    assert all(not m["content"].startswith("g") for m in hist)


def test_full_profile_ignores_switches_and_stamps():
    stamped = _pairs(3, "gemma4:e4b") + _pairs(2, "llama3")
    plain = _pairs(3) + _pairs(2)
    assert _assemble(stamped, profile="full").messages == _assemble(plain, profile="full").messages
    assert len(_hist(_assemble(stamped, profile="full"))) == 10


# --- engine: history entries carry the model -----------------------------------


def test_committed_history_carries_the_active_model(monkeypatch):
    motor, _ = _motor(monkeypatch)
    motor._generar_dialogo("hola", source="direct", commit_history=True)
    assert [e.get("model") for e in list(motor.historial)[-2:]] == ["llama3", "llama3"]


def test_compact_engine_turn_drops_pre_switch_history(monkeypatch):
    motor, seen = _motor(monkeypatch)
    motor.historial.extend(_pairs(3, "gemma4:e4b", "rust "))
    monkeypatch.setattr(
        motor, "_resolve_prompt_profile",
        lambda model, is_local: pp.ProfileDecision(pp.PROFILE_COMPACT, 1.0, pp.REASON_AUTO_SMALL),
    )
    motor._generar_dialogo("hola", source="direct", commit_history=True)
    sent = seen["messages"][0]
    assert all("rust" not in m["content"] for m in sent)
    assert len(sent) == 2  # system + this turn
