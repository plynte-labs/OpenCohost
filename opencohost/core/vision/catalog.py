"""Suggested vision models (vision-on-demand V1.17).

A static catalog the UI offers for download when a model is not installed.
Sizes are the Ollama Q4 downloads. Order is preference order: the first entry
is the default ``VISION_MODEL``.
"""

from __future__ import annotations

SUGGESTED_VISION_MODELS: tuple[dict, ...] = (
    {
        "tag": "qwen3-vl:4b-instruct",
        "display": "Qwen3-VL 4B Instruct",
        "size_gb": 3.3,
        "desc": "Default. Best quality; follows Spanish.",
    },
    {
        "tag": "qwen3-vl:2b-instruct",
        "display": "Qwen3-VL 2B Instruct",
        "size_gb": 1.9,
        "desc": "Light, for gaming. Describes in English.",
    },
)

_GEMMA_NOTE = "Ollama 0.30.11: answered 'Black' for solid colors and hallucinated scenes."


def _broken(tag: str) -> dict:
    return {
        "verdict": "broken",
        "vram_gib": None,
        "describe_s": None,
        "spanish": None,
        "note": _GEMMA_NOTE,
        "measured_on": "2026-10-01",
    }


# Static benchmark results (vision-on-demand V0.1/V0.6), keyed by canonical tag
# (no ``:latest``). Tags without an entry have no benchmark.
VISION_BENCHMARKS: dict[str, dict] = {
    "qwen3-vl:4b-instruct": {
        "verdict": "recommended",
        "vram_gib": 3.29,
        "describe_s": 1.46,
        "spanish": True,
        "note": "Colors 12/12, HUD and text probes perfect; Spanish 8/10.",
        "measured_on": "2026-10-02",
    },
    "qwen3-vl:2b-instruct": {
        "verdict": "light",
        "vram_gib": 1.84,
        "describe_s": 1.34,
        "spanish": False,
        "note": (
            "Same accuracy as 4B on probes; answers Spanish questions in English "
            "(0/10) and ignores the length cap."
        ),
        "measured_on": "2026-10-02",
    },
    "gemma4:e2b": _broken("gemma4:e2b"),
    "gemma4:e4b": _broken("gemma4:e4b"),
    "gemma4:12b": _broken("gemma4:12b"),
}
