"""F1 (memory-paraphrase-contradiction-20260928): calibration of
``_DEDUP_SEMANTIC_THRESHOLD`` against a synthetic labelled pair set.

The fixture and the pure scoring helpers are always tested. The real-model
check runs the MiniLM-L12 ONNX embedder in-process (the same
``MiniLMEmbeddingBackend`` the semantic worker hosts) and is skipped when the
model artifacts are not on disk. Synthetic text only -- no real memory rows.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from opencohost.core.memory.memoria_store import _DEDUP_SEMANTIC_THRESHOLD

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "memoria_dedup_pairs.json"
SCRIPT = REPO_ROOT / "scripts" / "calibrate_memoria_dedup_threshold.py"
MODEL_DIR = REPO_ROOT / "modelos_f5" / "minilm_l12_onnx"

requires_minilm_onnx = pytest.mark.skipif(
    not ((MODEL_DIR / "model.onnx").is_file() and (MODEL_DIR / "tokenizer.json").is_file()),
    reason="MiniLM ONNX model artifacts not present on disk",
)


def _load_script():
    spec = importlib.util.spec_from_file_location("calibrate_memoria_dedup_threshold", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixture_has_enough_labelled_pairs_in_both_languages():
    pairs = json.loads(FIXTURE.read_text(encoding="utf-8"))["pairs"]
    assert len(pairs) >= 40
    classes = {"paraphrase", "update", "negation", "unrelated_same_subject"}
    for lang in ("es", "en"):
        seen = {p["cls"] for p in pairs if p["lang"] == lang}
        assert seen == classes, lang
    for p in pairs:
        assert p["cls"] in classes
        assert p["a"].strip() and p["b"].strip() and p["a"] != p["b"]


def test_summarize_reports_min_median_max_per_class():
    calib = _load_script()
    scored = [
        {"cls": "paraphrase", "lang": "es", "score": 0.9},
        {"cls": "paraphrase", "lang": "en", "score": 0.7},
        {"cls": "paraphrase", "lang": "en", "score": 0.8},
        {"cls": "update", "lang": "es", "score": 0.6},
    ]
    summary = calib.summarize(scored)
    assert summary["paraphrase"] == {"n": 3, "min": 0.7, "median": 0.8, "max": 0.9}
    assert summary["update"] == {"n": 1, "min": 0.6, "median": 0.6, "max": 0.6}


def test_recommend_threshold_keeps_every_update_and_negation_below_it():
    calib = _load_script()
    scored = [
        {"cls": "paraphrase", "lang": "es", "score": 0.97},
        {"cls": "paraphrase", "lang": "es", "score": 0.93},
        {"cls": "paraphrase", "lang": "en", "score": 0.80},
        {"cls": "update", "lang": "es", "score": 0.912},
        {"cls": "negation", "lang": "en", "score": 0.88},
        {"cls": "unrelated_same_subject", "lang": "en", "score": 0.40},
    ]
    threshold, recall = calib.recommend_threshold(scored, margin=0.01)
    # Strictly above the highest update/negation score plus the margin,
    # rounded UP to two decimals: 0.912 + 0.01 = 0.922 -> 0.93.
    assert threshold == pytest.approx(0.93)
    assert all(s["score"] < threshold for s in scored if s["cls"] in ("update", "negation"))
    assert recall == pytest.approx(2 / 3)


def test_related_band_spans_update_and_same_subject_below_threshold():
    calib = _load_script()
    scored = [
        {"cls": "update", "lang": "es", "score": 0.85},
        {"cls": "update", "lang": "es", "score": 0.70},
        {"cls": "unrelated_same_subject", "lang": "en", "score": 0.45},
        {"cls": "negation", "lang": "en", "score": 0.95},
    ]
    assert calib.related_band(scored) == {
        "update": (0.70, 0.85),
        "unrelated_same_subject": (0.45, 0.45),
    }


def test_calibrated_numeric_update_score_is_not_a_semantic_duplicate(tmp_path):
    """Model-free guard for the calibration result: the highest update score
    measured on MiniLM-L12 ("24 anos" vs "25 anos", 0.955) must NOT dedup,
    so the shipped threshold stays correct where the ONNX model is absent."""
    from opencohost.core.memory.memoria_store import MemoriaStore

    store = MemoriaStore(tmp_path / "memorias.db")
    assert store._is_near_duplicate("x", "y", similarity_fn=lambda a, b: 0.955) is False
    assert store._is_near_duplicate("x", "y", similarity_fn=lambda a, b: 0.98) is True


@requires_minilm_onnx
def test_real_minilm_keeps_every_update_and_negation_below_the_shipped_threshold():
    calib = _load_script()
    scored = calib.score_pairs(calib.load_pairs(FIXTURE))
    losses = [s for s in scored if s["cls"] in ("update", "negation")
              and s["score"] >= _DEDUP_SEMANTIC_THRESHOLD]
    # A lost update is worse than a surviving duplicate: none may dedup.
    assert losses == []
    # The threshold must still catch at least one paraphrase, or F1 is inert.
    assert any(s["score"] >= _DEDUP_SEMANTIC_THRESHOLD
               for s in scored if s["cls"] == "paraphrase")
