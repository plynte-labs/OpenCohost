"""Advisory VRAM check for a local vision model next to a local chat model (A4).

Two independent Ollama processes share one GPU, so a VLM that does not fit
makes the chat model spill to RAM. This only warns; nothing is blocked. Pure:
the caller passes the (cached) GPU numbers, so a request never polls the GPU.
"""
from __future__ import annotations

from typing import Optional

from opencohost.core.vision.catalog import VISION_BENCHMARKS

# Context buffers and fragmentation on top of the measured weights footprint.
HEADROOM_GIB = 0.5
_MIB_PER_GIB = 1024.0


def _canonical(tag: str) -> str:
    tag = str(tag or "")
    return tag[: -len(":latest")] if tag.endswith(":latest") else tag


def _gib(mb: Optional[float]) -> Optional[float]:
    return None if mb is None else round(float(mb) / _MIB_PER_GIB, 2)


def assess(
    *, source: str, chat_is_local: bool, vlm_tag: str,
    free_mb: Optional[float], total_mb: Optional[float], vlm_loaded: bool,
) -> dict:
    """``vram_warning`` is ``"insufficient"``, ``"unknown"`` or None (fits, or not
    applicable). Numbers are GiB, None when unknown."""
    out = {
        "vram_warning": None,
        "vram_needed_gib": None,
        "vram_free_gib": None,
        "vram_total_gib": None,
    }
    if source != "local" or not chat_is_local:
        return out
    footprint = (VISION_BENCHMARKS.get(_canonical(vlm_tag)) or {}).get("vram_gib")
    gpu_known = free_mb is not None and total_mb is not None and total_mb > 0
    if gpu_known:
        out["vram_free_gib"] = _gib(free_mb)
        out["vram_total_gib"] = _gib(total_mb)
    if not isinstance(footprint, (int, float)):
        out["vram_warning"] = "unknown"
        return out
    needed = float(footprint) + HEADROOM_GIB
    out["vram_needed_gib"] = round(needed, 2)
    if not gpu_known:
        out["vram_warning"] = "unknown"
        return out
    available = float(free_mb) / _MIB_PER_GIB + (float(footprint) if vlm_loaded else 0.0)
    if available < needed:
        out["vram_warning"] = "insufficient"
    return out
