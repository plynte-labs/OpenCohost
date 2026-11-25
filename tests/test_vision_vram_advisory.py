"""A4: advisory VRAM check for a local VLM next to a local chat model."""
import pytest

from opencohost.core.vision import vram_advisory as va

GIB = 1024.0  # MiB per GiB


def _assess(**kw):
    base = dict(
        source="local", chat_is_local=True, vlm_tag="qwen3-vl:4b-instruct",
        free_mb=8 * GIB, total_mb=12 * GIB, vlm_loaded=False,
    )
    base.update(kw)
    return va.assess(**base)


def test_plenty_of_free_vram_gives_no_warning():
    out = _assess()
    assert out["vram_warning"] is None
    assert out["vram_needed_gib"] == pytest.approx(3.29 + va.HEADROOM_GIB)
    assert out["vram_free_gib"] == pytest.approx(8.0)
    assert out["vram_total_gib"] == pytest.approx(12.0)


def test_too_little_free_vram_warns_insufficient():
    out = _assess(free_mb=2 * GIB)
    assert out["vram_warning"] == "insufficient"


def test_a_loaded_vlm_counts_back_its_own_footprint():
    # 3 GiB free while the VLM (3.29) is resident: 6.29 free before it loaded.
    assert _assess(free_mb=3 * GIB, vlm_loaded=True)["vram_warning"] is None
    assert _assess(free_mb=3 * GIB, vlm_loaded=False)["vram_warning"] == "insufficient"


def test_unknown_gpu_reports_unknown_not_a_guess():
    out = _assess(free_mb=None, total_mb=None)
    assert out["vram_warning"] == "unknown"
    assert out["vram_free_gib"] is None and out["vram_total_gib"] is None


def test_unknown_vlm_footprint_reports_unknown():
    out = _assess(vlm_tag="some/custom-vlm:latest")
    assert out["vram_warning"] == "unknown"
    assert out["vram_needed_gib"] is None


def test_not_applicable_combinations_carry_no_warning():
    for kw in ({"source": "chat_model"}, {"source": "cloud_model"}, {"chat_is_local": False}):
        out = _assess(free_mb=0.5 * GIB, **kw)
        assert out["vram_warning"] is None
        assert out["vram_needed_gib"] is None


def test_latest_suffix_is_canonicalised():
    assert _assess(vlm_tag="qwen3-vl:4b-instruct:latest")["vram_warning"] is None
