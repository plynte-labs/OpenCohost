"""voice-catalog-selection-20261118 T2: Edge-TTS voice catalog (injectable fetch, no network)."""
import pytest

from opencohost.core.speech import edge_voices

RAW = [
    {"ShortName": "es-MX-DaliaNeural", "Locale": "es-MX", "Gender": "Female", "FriendlyName": "Dalia"},
    {"ShortName": "es-AR-TomasNeural", "Locale": "es-AR", "Gender": "Male", "FriendlyName": "Tomas"},
    {"ShortName": "en-US-AriaNeural", "Locale": "en-US", "Gender": "Female", "FriendlyName": "Aria"},
    {"ShortName": "", "Locale": "en-US", "Gender": "Male"},  # malformed, dropped
]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def catalog():
    clock = Clock()
    calls = []

    def fetch():
        calls.append(1)
        return RAW

    cat = edge_voices.EdgeVoiceCatalog(fetch=fetch, ttl_s=100, clock=clock, fallback=lambda lang: ["x-FB-Neural"])
    cat.calls, cat.clock = calls, clock
    return cat


def test_live_then_cache_within_ttl(catalog):
    voices, source = catalog.get("es")
    assert source == "live"
    assert [v["id"] for v in voices] == ["es-AR-TomasNeural", "es-MX-DaliaNeural"]
    assert voices[0] == {"id": "es-AR-TomasNeural", "label": "Tomas", "locale": "es-AR", "gender": "Male"}
    assert catalog.get("en")[1] == "cache"
    assert len(catalog.calls) == 1


def test_refetch_after_ttl(catalog):
    catalog.get("es")
    catalog.clock.now += 101
    assert catalog.get("es")[1] == "live"
    assert len(catalog.calls) == 2


def test_no_lang_returns_all_valid_voices(catalog):
    voices, _ = catalog.get(None)
    assert len(voices) == 3


def test_lang_filter_is_case_and_separator_insensitive(catalog):
    assert {v["id"] for v in catalog.get("EN-us")[0]} == {"en-US-AriaNeural"}


def test_failure_without_cache_uses_fallback():
    def boom():
        raise OSError("offline")

    cat = edge_voices.EdgeVoiceCatalog(fetch=boom, fallback=lambda lang: ["es-MX-DaliaNeural"])
    voices, source = cat.get("es")
    assert source == "fallback"
    assert [v["id"] for v in voices] == ["es-MX-DaliaNeural"]
    assert voices[0]["locale"] == "es-MX"


def test_failure_after_expiry_serves_stale_cache(catalog):
    catalog.get("es")
    catalog.clock.now += 500

    def boom():
        raise TimeoutError

    catalog._fetch = boom
    voices, source = catalog.get("es")
    assert source == "cache" and len(voices) == 2


def test_failed_fetch_is_not_retried_in_a_tight_loop():
    calls = []

    def boom():
        calls.append(1)
        raise OSError

    clock = Clock()
    cat = edge_voices.EdgeVoiceCatalog(fetch=boom, clock=clock, retry_after_s=30, fallback=lambda lang: [])
    cat.get("es")
    cat.get("es")
    assert len(calls) == 1
    clock.now += 31
    cat.get("es")
    assert len(calls) == 2


def test_empty_live_result_counts_as_failure():
    cat = edge_voices.EdgeVoiceCatalog(fetch=lambda: [], fallback=lambda lang: ["a-B-C"])
    assert cat.get("a")[1] == "fallback"


def test_bundle_fallback_reads_locale_manifests():
    ids = edge_voices.bundle_fallback_voices("es")
    assert "es-MX-DaliaNeural" in ids
    assert "en-US-AriaNeural" in edge_voices.bundle_fallback_voices("en")
