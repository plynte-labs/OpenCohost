"""T4 — i18n coherence gate (warn-only; the profile always wins).

The gate compares the active locale's declared code, its TTS voice language, and
the active profile's persona, emitting NON-BLOCKING warnings on a mismatch. It
never overrides anything (decision #2223).

Detection is deterministic (Option A): the gate does not infer a profile's
language from its prompt text — that is the separate profile-locale-autodetect
track. Today it flags that a custom profile persona is outside locale governance;
once a profile declares an explicit ``locale`` (Option B), the SAME gate compares
languages precisely and the generic notice falls silent. Both paths are covered.
"""
import logging
import queue
from unittest.mock import MagicMock

import pytest

from opencohost.i18n.coherence import (
    BUNDLE_VOICE_MISMATCH,
    PIPER_VOICE_LOCALE_MISMATCH,
    PROFILE_LOCALE_MISMATCH,
    PROFILE_PERSONA_UNGOVERNED,
    CoherenceWarning,
    check_coherence,
    log_coherence,
    primary_subtag,
    voice_language,
)
from opencohost.i18n.contract import LocaleBundle


def _bundle(code: str, voice: str | None = None) -> LocaleBundle:
    data: dict = {"meta": {"code": code}}
    if voice is not None:
        data["tts"] = {"edge_voice": voice}
    return LocaleBundle(code=code, tier="official", data=data)


ES = _bundle("es", "es-MX-DaliaNeural")
EN = _bundle("en", "en-US-AriaNeural")


def _codes(warnings) -> set[str]:
    return {w.code for w in warnings}


# ---------------------------------------------------------------------------
# primary_subtag / voice_language
# ---------------------------------------------------------------------------

class TestSubtagHelpers:
    @pytest.mark.parametrize("tag,expected", [
        ("en-US", "en"),
        ("es-MX-DaliaNeural", "es"),
        ("EN", "en"),
        ("es", "es"),
        ("", ""),
        (None, ""),
    ])
    def test_primary_subtag(self, tag, expected):
        assert primary_subtag(tag) == expected

    def test_voice_language_reads_bundle_voice(self):
        assert voice_language(EN) == "en"
        assert voice_language(ES) == "es"

    def test_voice_language_empty_when_no_voice(self):
        assert voice_language(_bundle("en")) == ""


# ---------------------------------------------------------------------------
# check_coherence — pure, never raises, warn-only
# ---------------------------------------------------------------------------

class TestCheckCoherence:
    def test_coherent_bundle_no_profile_is_silent(self):
        assert check_coherence(ES) == []
        assert check_coherence(EN) == []

    def test_returns_coherence_warning_instances(self):
        warnings = check_coherence(EN, profile_name="Akira", profile_prompt_active=True)
        assert warnings
        assert all(isinstance(w, CoherenceWarning) for w in warnings)

    def test_bundle_voice_locale_mismatch_warns(self):
        # Declared 'en' but Spanish voice — a malformed/community bundle.
        frankenstein = _bundle("en", "es-MX-DaliaNeural")
        assert BUNDLE_VOICE_MISMATCH in _codes(check_coherence(frankenstein))

    def test_no_voice_slot_does_not_warn_about_voice(self):
        assert BUNDLE_VOICE_MISMATCH not in _codes(check_coherence(_bundle("en")))

    # --- Option A (today): no declared locale on the profile ----------------

    def test_profile_prompt_active_without_locale_warns_ungoverned(self):
        warnings = check_coherence(EN, profile_name="Akira", profile_prompt_active=True)
        assert PROFILE_PERSONA_UNGOVERNED in _codes(warnings)

    def test_profile_prompt_inactive_is_silent_on_profile(self):
        warnings = check_coherence(EN, profile_name="Akira", profile_prompt_active=False)
        assert PROFILE_PERSONA_UNGOVERNED not in _codes(warnings)
        assert PROFILE_LOCALE_MISMATCH not in _codes(warnings)

    # --- Option B (future): the profile declares its locale -----------------

    def test_profile_locale_match_is_silent(self):
        warnings = check_coherence(
            EN, profile_name="Akira", profile_prompt_active=True, profile_locale="en-US"
        )
        assert PROFILE_LOCALE_MISMATCH not in _codes(warnings)

    def test_profile_locale_mismatch_warns(self):
        warnings = check_coherence(
            EN, profile_name="Akira", profile_prompt_active=True, profile_locale="es"
        )
        assert PROFILE_LOCALE_MISMATCH in _codes(warnings)

    def test_declared_locale_suppresses_generic_ungoverned_notice(self):
        # When the profile declares its language, the precise check takes over;
        # the generic "ungoverned" notice must not also fire (no double signal).
        matched = check_coherence(
            EN, profile_name="Akira", profile_prompt_active=True, profile_locale="en"
        )
        assert matched == []

        mismatched = check_coherence(
            EN, profile_name="Akira", profile_prompt_active=True, profile_locale="es"
        )
        assert PROFILE_PERSONA_UNGOVERNED not in _codes(mismatched)
        assert PROFILE_LOCALE_MISMATCH in _codes(mismatched)

    # --- P5: offline Piper voice vs active locale ---------------------------

    def test_piper_voice_locale_mismatch_warns(self):
        assert PIPER_VOICE_LOCALE_MISMATCH in _codes(check_coherence(EN, piper_voice_lang="es"))

    def test_piper_voice_locale_match_is_silent(self):
        assert check_coherence(EN, piper_voice_lang="en") == []
        assert check_coherence(ES, piper_voice_lang="es") == []

    def test_piper_voice_lang_none_is_silent(self):
        # No voice info supplied (default) -> never a false mismatch.
        assert PIPER_VOICE_LOCALE_MISMATCH not in _codes(check_coherence(EN))


# ---------------------------------------------------------------------------
# log_coherence — emits non-blocking WARNINGs
# ---------------------------------------------------------------------------

class TestLogCoherence:
    def test_logs_warning_level_with_code(self, caplog):
        with caplog.at_level(logging.WARNING, logger="OpenCohost"):
            returned = log_coherence(EN, profile_name="Akira", profile_prompt_active=True)
        assert returned
        assert any(r.levelno == logging.WARNING for r in caplog.records)
        assert PROFILE_PERSONA_UNGOVERNED in caplog.text

    def test_silent_when_coherent(self, caplog):
        with caplog.at_level(logging.WARNING, logger="OpenCohost"):
            returned = log_coherence(EN)
        assert returned == []
        assert "coherence" not in caplog.text


# ---------------------------------------------------------------------------
# Integration — wired into MotorVocalIA.set_profile (runtime persona swap)
# ---------------------------------------------------------------------------

def _make_motor():
    log_q = queue.Queue()
    from opencohost.core.llm_engine import MotorVocalIA
    motor = MotorVocalIA(log_q, lambda event: None)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.is_ready = True
    return motor


class TestSetProfileWiring:
    @pytest.fixture(autouse=True)
    def _pin_en_locale(self):
        from opencohost.i18n import active
        from opencohost.i18n.startup import resolve_active_bundle
        active.set_active_bundle(resolve_active_bundle(locale="en"))
        yield
        active.reset_active_bundle()

    def test_set_profile_with_custom_prompt_warns_under_en_locale(self, caplog):
        motor = _make_motor()
        with caplog.at_level(logging.WARNING, logger="OpenCohost"):
            motor._dispatch_command("set_profile", {
                "prompt": "Eres Kira, una co-host virtual.",
                "use_system": True,
                "_profile_name": "Akira",
            })
        assert PROFILE_PERSONA_UNGOVERNED in caplog.text

    def test_set_profile_declared_matching_locale_is_silent(self, caplog):
        motor = _make_motor()
        with caplog.at_level(logging.WARNING, logger="OpenCohost"):
            motor._dispatch_command("set_profile", {
                "prompt": "You are Kira, a virtual co-host.",
                "use_system": True,
                "_profile_name": "Akira EN",
                "locale": "en",
            })
        assert "coherence" not in caplog.text

    def test_set_profile_declared_mismatched_locale_warns(self, caplog):
        motor = _make_motor()
        with caplog.at_level(logging.WARNING, logger="OpenCohost"):
            motor._dispatch_command("set_profile", {
                "prompt": "Eres Kira.",
                "use_system": True,
                "_profile_name": "Akira",
                "locale": "es",
            })
        assert PROFILE_LOCALE_MISMATCH in caplog.text


# ---------------------------------------------------------------------------
# P5 — startup coherence check + locale-aware Piper default (engine init)
# ---------------------------------------------------------------------------

class TestPiperVoiceStartupCoherence:
    @pytest.fixture(autouse=True)
    def _pin_en_locale(self):
        from opencohost.i18n import active
        from opencohost.i18n.startup import resolve_active_bundle
        active.set_active_bundle(resolve_active_bundle(locale="en"))
        yield
        active.reset_active_bundle()

    def test_init_picks_english_voice_when_no_persisted_choice(self):
        # No persisted piper_voice.json (isolated by conftest's autouse fixture)
        # under en locale -> the locale-aware default engages.
        motor = _make_motor()
        assert motor._piper_voice_key == "english"

    def test_init_logs_mismatch_when_persisted_choice_disagrees(self, tmp_path, monkeypatch, caplog):
        from opencohost.config import settings as settings_mod

        path = str(tmp_path / "piper_voice.json")
        settings_mod.save_piper_voice("neutral", config_file=path)
        monkeypatch.setattr(settings_mod, "PIPER_VOICE_FILE", path)
        with caplog.at_level(logging.WARNING, logger="OpenCohost"):
            motor = _make_motor()
        assert motor._piper_voice_key == "neutral"  # explicit user pick always wins
        assert PIPER_VOICE_LOCALE_MISMATCH in caplog.text

    def test_init_silent_when_persisted_choice_matches_locale(self, tmp_path, monkeypatch, caplog):
        from opencohost.config import settings as settings_mod

        path = str(tmp_path / "piper_voice.json")
        settings_mod.save_piper_voice("english", config_file=path)
        monkeypatch.setattr(settings_mod, "PIPER_VOICE_FILE", path)
        with caplog.at_level(logging.WARNING, logger="OpenCohost"):
            _make_motor()
        assert "coherence" not in caplog.text
