"""Regression suite for English-mode isolation and Spanish drift prevention."""
from __future__ import annotations

import json
import threading
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest

from opencohost.config import settings
from opencohost.core.context.prompt_assembler import PromptContextAssembler
from opencohost.core.profiles import personalization, profiles as prof_mod
from opencohost.core.self_knowledge.texts import t
from opencohost.i18n import active as i18n_active
from opencohost.i18n import state


@pytest.fixture
def isolated_profile_env(tmp_path, monkeypatch):
    """Isolate PROFILES_FILE, LAST_PROFILE_FILE, and PIPER_VOICE_FILE in tmp_path."""
    profiles_file = tmp_path / "perfiles.json"
    last_profile_file = tmp_path / "last_profile.json"
    piper_voice_file = tmp_path / "piper_voice.json"
    monkeypatch.setattr(prof_mod, "PROFILES_FILE", str(profiles_file))
    monkeypatch.setattr(settings, "LAST_PROFILE_FILE", str(last_profile_file))
    monkeypatch.setattr(settings, "PIPER_VOICE_FILE", str(piper_voice_file))
    return SimpleNamespace(
        tmp_path=tmp_path,
        profiles_file=profiles_file,
        last_profile_file=last_profile_file,
        piper_voice_file=piper_voice_file,
    )


def test_en_gender_rule_contains_no_spanish_words():
    gender_en = t("en")["gender"]
    low = gender_en.lower()
    for forbidden in ("yo misma", "lista", "segura", "in spanish", "español"):
        assert forbidden not in low
    assert "feminine" in low
    assert "only to yourself" in low
    assert "never assume the gender" in low
    assert "neutral" in low


def test_set_locale_en_aligns_es_or_untagged_last_profile_to_kira(isolated_profile_env):
    isolated_profile_env.profiles_file.write_text(
        json.dumps(
            {
                "AIKira": {"prompt": "Eres Kira en español.", "use_system": True},
                "Akira": {"prompt": "Eres Kira.", "use_system": True, "locale": "es"},
            }
        ),
        encoding="utf-8",
    )
    settings.save_last_profile("AIKira")

    state.set_locale("en", isolated_profile_env.tmp_path / "locale.json")

    assert settings.load_last_profile() == "Kira"


def test_set_locale_en_falls_back_to_default_en_when_kira_absent(isolated_profile_env, monkeypatch):
    defaults_file = isolated_profile_env.tmp_path / "default_profiles.json"
    defaults_file.write_text(
        json.dumps(
            {
                "Akira": {"prompt": "Eres Kira.", "use_system": True, "locale": "es"},
                "Default EN": {"prompt": "You are Kira.", "use_system": True, "locale": "en"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(prof_mod, "DEFAULT_PROFILES_FILE", str(defaults_file))
    isolated_profile_env.profiles_file.write_text(
        json.dumps({"Akira": {"prompt": "Eres Kira.", "use_system": True, "locale": "es"}}),
        encoding="utf-8",
    )
    settings.save_last_profile("Akira")

    state.set_locale("en", isolated_profile_env.tmp_path / "locale.json")

    assert settings.load_last_profile() == "Default EN"


def test_set_locale_en_preserves_existing_english_profile(isolated_profile_env):
    isolated_profile_env.profiles_file.write_text(
        json.dumps(
            {
                "Default EN": {"prompt": "Custom English Kira.", "use_system": True, "locale": "en"},
                "Kira": {"prompt": "Default English Kira.", "use_system": True, "locale": "en"},
            }
        ),
        encoding="utf-8",
    )
    settings.save_last_profile("Default EN")

    state.set_locale("en", isolated_profile_env.tmp_path / "locale.json")

    assert settings.load_last_profile() == "Default EN"


def test_set_locale_es_aligns_english_last_profile_to_akira(isolated_profile_env):
    isolated_profile_env.profiles_file.write_text(
        json.dumps(
            {
                "Kira": {"prompt": "You are Kira.", "use_system": True, "locale": "en"},
                "Akira": {"prompt": "Eres Kira.", "use_system": True, "locale": "es"},
            }
        ),
        encoding="utf-8",
    )
    settings.save_last_profile("Kira")

    state.set_locale("es", isolated_profile_env.tmp_path / "locale.json")

    assert settings.load_last_profile() == "Akira"


def test_prompt_assembler_forwards_locale_to_personalization(monkeypatch):
    build_calls: list[tuple[str | None, str | None]] = []
    load_calls: list[str | None] = []

    def fake_load(*, locale=None):
        load_calls.append(locale)
        return {
            "enabled": True,
            "nickname": "FranGuh",
            "occupation": "streamer",
            "interests": "games",
            "style": "Direct",
        }

    def fake_build(sanitize_fn, part="all", *, locale=None):
        build_calls.append((part, locale))
        return f"<user_profile>{part}:{locale}</user_profile>"

    monkeypatch.setattr(personalization, "load_personalization", fake_load)
    monkeypatch.setattr(personalization, "build_injection_block", fake_build)

    assembler = PromptContextAssembler(sanitize_history_fn=lambda x: str(x))
    assembler.assemble(
        "who am i?",
        "direct",
        system_prompt="You are Kira.",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="qwopus:9b",
        history_snapshot=[],
        personalization_enabled=True,
        memorias_enabled=False,
        prompt_profile="full",
        lang="en",
    )

    assert build_calls
    assert all(loc == "en" for _, loc in build_calls)
    assert load_calls
    assert all(loc == "en" for loc in load_calls)


def test_prompt_assembler_tolerates_legacy_personalization_mocks(monkeypatch):
    monkeypatch.setattr(
        personalization,
        "load_personalization",
        lambda: {"enabled": True, "nickname": "Tavo", "occupation": "", "interests": "", "style": ""},
    )
    monkeypatch.setattr(
        personalization,
        "build_injection_block",
        lambda sanitize_fn, part="all": f"<legacy>{part}</legacy>",
    )

    assembler = PromptContextAssembler(sanitize_history_fn=lambda x: str(x))
    res = assembler.assemble(
        "hello",
        "direct",
        system_prompt="You are Kira.",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="qwopus:9b",
        history_snapshot=[],
        personalization_enabled=True,
        memorias_enabled=False,
        prompt_profile="full",
        lang="en",
    )

    assert "<legacy>identity</legacy>" in res.messages[0]["content"]


def test_llm_engine_forwards_profile_locale_as_lang(monkeypatch):
    from opencohost.core.llm_engine import MotorVocalIA
    from opencohost.core.context.prompt_assembler import GenerationSetup

    captured: dict = {}

    class FakeAssembler:
        def assemble(self, *args, **kwargs):
            captured.update(kwargs)
            return GenerationSetup(
                messages=[{"role": "user", "content": "hi"}],
                opciones_llm={},
                chat_timeout=30.0,
                max_intentos=1,
                start_llm=0.0,
                native_ctx=4096,
                effective_ctx=4096,
                ctx_evicted=0,
                editorial_block="",
                history_snapshot=[],
            )

    engine = MotorVocalIA.__new__(MotorVocalIA)
    engine._lock = threading.Lock()
    engine._speaking = False
    engine._speech_router = None
    engine._prompt_lock = threading.Lock()
    engine._history_lock = threading.Lock()
    engine.system_prompt = "Eres Kira."
    engine.use_system_role = True
    engine._persona_custom = False
    engine._extended_responses = False
    engine._warmth = "cold"
    engine._owner_gender = "neutral"
    engine._current_profile_name = "default"
    engine._current_profile_locale = None
    engine._current_profile_id = None
    engine._max_turns = 10
    engine._requested_max_turns = 10
    engine._max_turns_cap = 10
    engine._compute_max_turns_cap = lambda: 10
    engine.historial = deque(maxlen=20)
    engine._memory_digest = SimpleNamespace(clear=lambda: None, build_block=lambda **kw: "")
    engine._sanitize_history_context = lambda x: str(x)
    engine._digested_turn_keys = set()
    engine._session_memoria_titles = []
    engine._session_topics_reset = lambda: None
    engine._owner_reflection_reset = lambda: None
    engine.clear_prefetched_agenda = lambda: None
    engine._invalidate_frozen_stash = lambda: None
    engine._collect_flush_drafts = lambda: []
    engine._dispatch_switch_flush = lambda *a, **kw: None
    engine._memory = None
    engine._log = lambda *a, **kw: None
    engine._piper_voice_key = "neutral"
    engine.current_model = "test-model"
    engine._prompt_assembler = FakeAssembler()
    engine._resolve_prompt_profile = lambda model, is_local: SimpleNamespace(profile="full")
    engine._session_topics_block = lambda *a, **kw: ""
    engine._memorias_builder_with_recall = lambda *a, **kw: (None, {})
    engine._bind_active_card_vector = lambda *a, **kw: None
    engine._resolve_reply_level = lambda *a, **kw: None
    engine._clock_kwargs = lambda *a, **kw: {}
    engine._resolve_chat_watchdog_timeout = None
    engine._resolve_reasoning_classification = None
    engine._resolve_reasoning_settings = None
    engine._resolve_model_residency_ratio = None
    engine._resolve_effective_ctx_limit = None
    engine._history_maxlen = lambda n: n * 2

    # Force active bundle to Spanish while switching to an English profile.
    monkeypatch.setattr(
        i18n_active,
        "get_active_bundle",
        lambda: SimpleNamespace(code="es", data={"tts": {"edge_voice": "es-MX-DaliaNeural"}}, fallback=None),
    )

    engine._consume_command(
        (
            "set_profile",
            {
                "_profile_name": "Default EN",
                "prompt": "You are Kira.",
                "use_system": True,
                "locale": "en",
            },
        )
    )
    assert engine._current_profile_locale == "en"

    engine._build_generation_request(
        "hello",
        "direct",
        is_local=True,
        provider_cfg={},
        request_model="test-model",
        watchdog_timeout=30.0,
    )
    assert captured.get("lang") == "en"

    # Switching back to an untagged profile clears _current_profile_locale and falls back to bundle.
    engine._consume_command(
        (
            "set_profile",
            {
                "_profile_name": "Custom",
                "prompt": "Eres Kira.",
                "use_system": True,
            },
        )
    )
    assert engine._current_profile_locale is None
    engine._build_generation_request(
        "hola",
        "direct",
        is_local=True,
        provider_cfg={},
        request_model="test-model",
        watchdog_timeout=30.0,
    )
    assert captured.get("lang") == "es"


def test_default_profiles_en_anchor_english_language():
    defaults_path = (
        Path(__file__).resolve().parent.parent / "opencohost" / "config" / "default_profiles.json"
    )
    data = json.loads(defaults_path.read_text(encoding="utf-8"))
    en_profiles = {name: cfg for name, cfg in data.items() if cfg.get("locale") == "en"}
    assert len(en_profiles) == 7
    for name, cfg in en_profiles.items():
        low = cfg["prompt"].lower()
        assert "always reply in" in low and "english" in low, (
            f"English profile {name!r} is missing explicit English-anchoring rule"
        )
        assert "username" in low or "proper noun" in low, (
            f"English profile {name!r} should guard against non-English usernames/proper nouns"
        )


def test_en_gender_rule_enforces_first_person_not_third_person():
    import re

    low = t("en")["gender"].lower()
    assert "first person" in low
    assert "third person" in low
    assert not re.search(r"\b(?:she|her)\b", low)

