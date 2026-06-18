"""Tests for the i18n-core bundle registry (discover / load / chain / validate).

T0b of the english_compatibility_i18n track. Exercises the filesystem seam with
tmp_path; one test loads the REAL shipped es bundle to prove the chassis moves.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from opencohost.i18n.contract import (
    SlotNotFound,
    TIER_COMMUNITY,
    TIER_OFFICIAL,
    resolve,
)
from opencohost.i18n.registry import (
    BundleLoadError,
    build_chain,
    discover_bundles,
    load_bundle,
    official_locales_dir,
    validate_bundle,
)


def _write_manifest(
    dir_path: Path, code: str, *, tier="official", fallback=None, extra=None, schema_version=1
):
    dir_path.mkdir(parents=True, exist_ok=True)
    meta = {
        "code": code,
        "display": code,
        "tier": tier,
        "status": "complete",
        "fallback": fallback or [],
    }
    if schema_version is not None:
        meta["schema_version"] = schema_version
    data = {"meta": meta}
    if extra:
        data.update(extra)
    (dir_path / "manifest.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


# --- load_bundle -----------------------------------------------------------

def test_load_bundle_reads_code_tier_data(tmp_path):
    _write_manifest(tmp_path / "es", "es", extra={"tts": {"edge_voice": "es-MX-DaliaNeural"}})
    b = load_bundle(tmp_path / "es", TIER_OFFICIAL)
    assert b.code == "es"
    assert b.tier == TIER_OFFICIAL
    assert resolve(b, "tts.edge_voice") == "es-MX-DaliaNeural"


def test_load_bundle_missing_manifest_raises(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(BundleLoadError):
        load_bundle(tmp_path / "empty", TIER_OFFICIAL)


def test_load_bundle_malformed_yaml_raises(tmp_path):
    d = tmp_path / "es"
    d.mkdir()
    (d / "manifest.yaml").write_text("meta: [unclosed", encoding="utf-8")
    with pytest.raises(BundleLoadError):
        load_bundle(d, TIER_OFFICIAL)


def test_validate_warns_on_missing_schema_version(tmp_path):
    _write_manifest(tmp_path / "es", "es", schema_version=None)
    b = load_bundle(tmp_path / "es", TIER_OFFICIAL)
    res = validate_bundle(b)
    assert res.ok  # missing schema_version is a warning, not an error
    assert any("schema_version" in w for w in res.warnings)


# --- discover_bundles ------------------------------------------------------

def test_discover_finds_bundles_and_skips_non_bundle_dirs(tmp_path):
    _write_manifest(tmp_path / "es", "es")
    _write_manifest(tmp_path / "en", "en")
    (tmp_path / "not_a_bundle").mkdir()  # no manifest → skipped
    found = discover_bundles(tmp_path, TIER_OFFICIAL)
    assert set(found) == {"es", "en"}


def test_discover_missing_dir_returns_empty(tmp_path):
    assert discover_bundles(tmp_path / "nope", TIER_OFFICIAL) == {}


# --- build_chain -----------------------------------------------------------

def test_build_chain_links_fallback(tmp_path):
    _write_manifest(tmp_path / "es", "es", extra={"tts": {"edge_voice": "es-MX-DaliaNeural"}})
    _write_manifest(tmp_path / "en", "en", fallback=["es"])
    bundles = discover_bundles(tmp_path, TIER_OFFICIAL)
    en = build_chain("en", bundles)
    # en lacks tts.edge_voice → resolves through the wired fallback to es
    assert resolve(en, "tts.edge_voice") == "es-MX-DaliaNeural"


def test_build_chain_unknown_locale_raises(tmp_path):
    _write_manifest(tmp_path / "en", "en", fallback=["es"])  # es absent
    bundles = discover_bundles(tmp_path, TIER_OFFICIAL)
    with pytest.raises(BundleLoadError):
        build_chain("en", bundles)


def test_build_chain_security_slot_still_does_not_fall_back(tmp_path):
    _write_manifest(tmp_path / "es", "es", extra={"guardrails": {"banned_phrases": ["x"]}})
    _write_manifest(tmp_path / "en", "en", fallback=["es"])
    bundles = discover_bundles(tmp_path, TIER_OFFICIAL)
    en = build_chain("en", bundles)
    with pytest.raises(SlotNotFound):
        resolve(en, "guardrails.banned_phrases")


# --- validate_bundle -------------------------------------------------------

def test_validate_structural_ok(tmp_path):
    _write_manifest(tmp_path / "es", "es")
    b = load_bundle(tmp_path / "es", TIER_OFFICIAL)
    assert validate_bundle(b).ok


def test_validate_bad_tier_fails(tmp_path):
    _write_manifest(tmp_path / "es", "es")
    b = load_bundle(tmp_path / "es", "bogus")
    res = validate_bundle(b)
    assert not res.ok and res.errors


def test_validate_strict_official_requires_guardrails(tmp_path):
    _write_manifest(tmp_path / "en", "en")  # no guardrails slot
    b = load_bundle(tmp_path / "en", TIER_OFFICIAL)
    assert validate_bundle(b).ok  # structural pass
    assert not validate_bundle(b, strict=True).ok  # but incomplete for official


def test_validate_strict_community_allows_missing_guardrails(tmp_path):
    _write_manifest(tmp_path / "de", "de", tier="community")
    b = load_bundle(tmp_path / "de", TIER_COMMUNITY)
    # community mods may ship without guardrails (at the user's own risk)
    assert validate_bundle(b, strict=True).ok


def test_validate_warns_when_manifest_tier_disagrees_with_source(tmp_path):
    # a community mod claiming "official" to dodge the warning: source wins.
    _write_manifest(tmp_path / "de", "de", tier="official")
    b = load_bundle(tmp_path / "de", TIER_COMMUNITY)
    res = validate_bundle(b)
    assert b.tier == TIER_COMMUNITY  # directory is authoritative
    assert res.warnings


# --- real shipped bundle ---------------------------------------------------

def test_real_es_bundle_loads_and_validates_structurally():
    es_dir = official_locales_dir() / "es"
    assert es_dir.exists(), "shipped es bundle must exist"
    b = load_bundle(es_dir, TIER_OFFICIAL)
    assert b.code == "es"
    assert validate_bundle(b).ok
    # the current Spanish Edge voice is now available through the contract
    assert resolve(b, "tts.edge_voice") == "es-MX-DaliaNeural"
