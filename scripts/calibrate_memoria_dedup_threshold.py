"""Offline calibration of memoria's semantic dedup threshold (ADR-060 MiniLM).

memory-paraphrase-contradiction-20260928 F1. Embeds a SYNTHETIC labelled pair
set with the real MiniLM-L12 ONNX model, in-process, through the same
``MiniLMEmbeddingBackend`` the semantic worker hosts (no new dependency), and
reports the cosine distribution per class plus a recommended threshold.

Rule: a lost update is worse than a surviving duplicate, so the recommended
threshold sits strictly above EVERY update and negation pair (plus a margin).
Paraphrase recall at that threshold is reported, not optimised.

Never reads data/*.db or any real memory row. Prints scores and the fixture's
synthetic text only.

Usage:
    python scripts/calibrate_memoria_dedup_threshold.py
    python scripts/calibrate_memoria_dedup_threshold.py --pairs tests/fixtures/memoria_dedup_pairs.json --margin 0.01
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PAIRS = ROOT / "tests" / "fixtures" / "memoria_dedup_pairs.json"
CLASSES = ("paraphrase", "update", "negation", "unrelated_same_subject")
MUST_NOT_DEDUP = ("update", "negation")
RELATED_CLASSES = ("update", "unrelated_same_subject")


def load_pairs(path: Path = DEFAULT_PAIRS) -> list[dict]:
    return list(json.loads(Path(path).read_text(encoding="utf-8"))["pairs"])


def score_pairs(pairs: list[dict], backend=None) -> list[dict]:
    """Cosine per pair from ONE batched embedding pass (vectors are L2-normed)."""
    if backend is None:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from opencohost.core.memory_v5_shadow.semantic import MiniLMEmbeddingBackend

        backend = MiniLMEmbeddingBackend()
    texts = sorted({p["a"] for p in pairs} | {p["b"] for p in pairs})
    vectors = dict(zip(texts, backend.embed_batch(texts)))
    scored = []
    for p in pairs:
        va, vb = vectors[p["a"]], vectors[p["b"]]
        dot = sum(x * y for x, y in zip(va, vb))
        norm = math.sqrt(sum(x * x for x in va)) * math.sqrt(sum(y * y for y in vb))
        scored.append({**p, "score": dot / norm if norm > 1e-9 else 0.0})
    return scored


def summarize(scored: list[dict]) -> dict:
    out = {}
    for cls in CLASSES:
        values = [s["score"] for s in scored if s["cls"] == cls]
        if values:
            out[cls] = {
                "n": len(values),
                "min": min(values),
                "median": statistics.median(values),
                "max": max(values),
            }
    return out


def recommend_threshold(scored: list[dict], margin: float = 0.01) -> tuple[float, float]:
    """(threshold, paraphrase recall). Threshold = the highest update/negation
    score plus *margin*, rounded UP to two decimals."""
    ceiling = max(s["score"] for s in scored if s["cls"] in MUST_NOT_DEDUP)
    threshold = math.ceil(round((ceiling + margin) * 100, 6)) / 100
    paraphrases = [s["score"] for s in scored if s["cls"] == "paraphrase"]
    recall = (
        sum(1 for v in paraphrases if v >= threshold) / len(paraphrases)
        if paraphrases else 0.0
    )
    return threshold, recall


def related_band(scored: list[dict]) -> dict:
    """(min, max) cosine per RELATED class -- the F2 candidate-retrieval band."""
    band = {}
    for cls in RELATED_CLASSES:
        values = [s["score"] for s in scored if s["cls"] == cls]
        if values:
            band[cls] = (min(values), max(values))
    return band


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pairs", type=Path, default=DEFAULT_PAIRS)
    parser.add_argument("--margin", type=float, default=0.01)
    parser.add_argument("--show-pairs", action="store_true",
                        help="print every synthetic pair with its score")
    args = parser.parse_args(argv)

    scored = score_pairs(load_pairs(args.pairs))
    print(f"pairs={len(scored)}")
    print(f"{'class':<24}{'n':>4}{'min':>8}{'median':>8}{'max':>8}")
    for cls, row in summarize(scored).items():
        print(f"{cls:<24}{row['n']:>4}{row['min']:>8.3f}{row['median']:>8.3f}{row['max']:>8.3f}")
    for lang in ("es", "en"):
        sub = [s for s in scored if s["lang"] == lang]
        parts = [f"{c}={r['min']:.3f}/{r['median']:.3f}/{r['max']:.3f}"
                 for c, r in summarize(sub).items()]
        print(f"[{lang}] " + " ".join(parts))
    threshold, recall = recommend_threshold(scored, args.margin)
    print(f"recommended_threshold={threshold:.2f} paraphrase_recall={recall:.3f}")
    for cls, (lo, hi) in related_band(scored).items():
        print(f"related_band {cls}: {lo:.3f}..{hi:.3f}")
    if args.show_pairs:
        for s in sorted(scored, key=lambda s: (s["cls"], -s["score"])):
            print(f"{s['score']:.3f} {s['lang']} {s['cls']:<22} {s['a']} || {s['b']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
