"""Static vision model benchmarks (vision-on-demand V1.19)."""

from opencohost.core.vision.catalog import SUGGESTED_VISION_MODELS, VISION_BENCHMARKS

_KEYS = {"verdict", "vram_gib", "describe_s", "spanish", "note", "measured_on"}


def test_every_entry_has_the_contract_shape():
    for tag, entry in VISION_BENCHMARKS.items():
        assert set(entry) == _KEYS, tag
        assert entry["verdict"] in {"recommended", "light", "broken"}
        assert entry["measured_on"] in {"2026-10-01", "2026-10-02"}
        assert not tag.endswith(":latest")


def test_measured_values():
    four = VISION_BENCHMARKS["qwen3-vl:4b-instruct"]
    assert (four["verdict"], four["vram_gib"], four["describe_s"], four["spanish"]) == (
        "recommended", 3.29, 1.46, True,
    )
    two = VISION_BENCHMARKS["qwen3-vl:2b-instruct"]
    assert (two["verdict"], two["vram_gib"], two["describe_s"], two["spanish"]) == (
        "light", 1.84, 1.34, False,
    )


def test_gemma4_family_is_broken_with_no_measurements():
    for tag in ("gemma4:e2b", "gemma4:e4b", "gemma4:12b"):
        entry = VISION_BENCHMARKS[tag]
        assert entry["verdict"] == "broken"
        assert entry["vram_gib"] is None and entry["describe_s"] is None
        assert entry["spanish"] is None
        assert "0.30.11" in entry["note"]


def test_every_suggested_model_has_a_benchmark():
    assert all(m["tag"] in VISION_BENCHMARKS for m in SUGGESTED_VISION_MODELS)
