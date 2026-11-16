"""compact-prompt-profile-20261009 T1: pure prompt profile resolution.

Small local models (about 2B parameters or less) get the compact profile; any
larger, unknown-size or cloud model keeps the full one. The env override
(``OPENCOHOST_PROMPT_PROFILE``) forces a profile for local models only.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from opencohost.core.context import prompt_profile as pp


# --- size parsing -------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("0.8B", 0.8),
        ("800M", 0.8),
        ("270M", 0.27),
        ("1.5b", 1.5),
        ("7B", 7.0),
        ("8.0B", 8.0),
        (" 4.3B ", 4.3),
        ("1,000M", 1.0),
    ],
)
def test_parse_parameter_size(text, expected):
    assert pp.parse_parameter_size(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", [None, "", "unknown", "B", "abc", 3, "-1B", "0B"])
def test_parse_parameter_size_unknown(text):
    assert pp.parse_parameter_size(text) is None


@pytest.mark.parametrize(
    "name, expected",
    [
        ("hf.co/unsloth/Qwen3.5-0.8B-GGUF:Q4_K_M", 0.8),
        ("qwen3:1.7b", 1.7),
        ("deepseek-r1:1.5b", 1.5),
        ("llama3.2:3b", 3.0),
        ("qwen3:4b-instruct", 4.0),
    ],
)
def test_size_from_model_name(name, expected):
    assert pp.size_from_model_name(name) == pytest.approx(expected)


@pytest.mark.parametrize("name", ["llama3", "qwopus", "", None, "mistral:latest", "model-bf16"])
def test_size_from_model_name_unknown(name):
    assert pp.size_from_model_name(name) is None


def test_parameter_size_from_show_dict_and_object():
    assert pp.parameter_size_from_show({"details": {"parameter_size": "0.8B"}}) == "0.8B"
    obj = SimpleNamespace(details=SimpleNamespace(parameter_size="800M"))
    assert pp.parameter_size_from_show(obj) == "800M"
    for junk in (None, {}, {"details": None}, SimpleNamespace(), "x", 5):
        assert pp.parameter_size_from_show(junk) is None


# --- resolve ------------------------------------------------------------------


def test_small_local_model_is_compact():
    d = pp.resolve("x", is_local=True, override="auto", parameter_size="0.8B")
    assert (d.profile, d.model_b, d.reason) == ("compact", 0.8, "auto_small")


def test_threshold_is_inclusive_at_two_billion():
    assert pp.resolve("x", is_local=True, override="auto", parameter_size="2B").profile == "compact"
    assert pp.resolve("x", is_local=True, override="auto", parameter_size="2.1B").profile == "full"


def test_large_local_model_is_full():
    d = pp.resolve("x", is_local=True, override="auto", parameter_size="4.0B")
    assert (d.profile, d.model_b, d.reason) == ("full", 4.0, "auto_large")


def test_details_win_over_name():
    d = pp.resolve("qwen3:1.7b", is_local=True, override="auto", parameter_size="8B")
    assert d.profile == "full" and d.model_b == 8.0


def test_name_fallback_when_details_missing():
    d = pp.resolve("qwen3:1.7b", is_local=True, override="auto", parameter_size=None)
    assert (d.profile, d.model_b, d.reason) == ("compact", 1.7, "auto_small")


def test_unknown_size_is_full():
    d = pp.resolve("llama3", is_local=True, override="auto", parameter_size=None)
    assert (d.profile, d.model_b, d.reason) == ("full", None, "unknown_size")


def test_cloud_is_never_compact():
    for override in ("auto", "compact", "full"):
        d = pp.resolve("tiny:0.5b", is_local=False, override=override, parameter_size="0.5B")
        assert (d.profile, d.reason) == ("full", "cloud")


@pytest.mark.parametrize("override, profile", [("full", "full"), ("compact", "compact")])
def test_env_override_forces_profile(override, profile):
    d = pp.resolve("llama3:8b", is_local=True, override=override, parameter_size="0.8B")
    assert (d.profile, d.reason) == (profile, "env_override")
    d = pp.resolve("llama3", is_local=True, override=override, parameter_size=None)
    assert (d.profile, d.reason) == (profile, "env_override")


@pytest.mark.parametrize("raw", [None, "", "AUTO", " auto ", "bogus", "1"])
def test_normalize_override_defaults_to_auto(raw):
    assert pp.normalize_override(raw) == "auto"


def test_normalize_override_accepts_values_case_insensitively():
    assert pp.normalize_override(" Compact ") == "compact"
    assert pp.normalize_override("FULL") == "full"


def test_resolve_never_raises_on_junk():
    d = pp.resolve(object(), is_local=True, override=object(), parameter_size=object())
    assert d.profile == "full"


def test_log_line_has_no_user_text():
    d = pp.resolve("x", is_local=True, override="auto", parameter_size="0.8B")
    assert d.log_line() == "[PROMPT_PROFILE] profile=compact model_b=0.8 reason=auto_small"
    d = pp.resolve("llama3", is_local=True, override="auto", parameter_size=None)
    assert d.log_line() == "[PROMPT_PROFILE] profile=full model_b=unknown reason=unknown_size"


def test_settings_keys(monkeypatch):
    from opencohost.config import settings

    assert settings.PROMPT_COMPACT_MAX_B == 2.0
    assert settings.PROMPT_PROFILE in ("auto", "full", "compact")
    monkeypatch.setenv("OPENCOHOST_PROMPT_PROFILE", "compact")
    assert settings._resolve_prompt_profile() == "compact"
    monkeypatch.setenv("OPENCOHOST_PROMPT_PROFILE", "nope")
    assert settings._resolve_prompt_profile() == "auto"
    monkeypatch.delenv("OPENCOHOST_PROMPT_PROFILE")
    assert settings._resolve_prompt_profile() == "auto"


# --- memory facts cap ---------------------------------------------------------


def test_cap_memory_block_keeps_tags_and_first_lines():
    block = "<m>\n- a\n- b\n- c\n- d\n</m>"
    assert pp.cap_memory_block(block, 2, "<m>", "</m>") == "<m>\n- a\n- b\n</m>"


def test_cap_memory_block_short_or_empty_unchanged():
    assert pp.cap_memory_block("", 2, "<m>", "</m>") == ""
    block = "<m>\n- a\n</m>"
    assert pp.cap_memory_block(block, 2, "<m>", "</m>") == block


def test_cap_memory_block_without_tags_caps_lines():
    assert pp.cap_memory_block("a\nb\nc", 2, "<m>", "</m>") == "a\nb"
