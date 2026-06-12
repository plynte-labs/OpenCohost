"""Piper speaking-rate preset selector for the TTS settings section.

Lives outside app_shell so the shell stays within its line budget; app_shell
only calls build_tts_speed_selector() with a dispatch callback.

Presets map UI labels to Piper length_scale values (>1.0 = slower). They
apply to Piper only — Edge-TTS speech rate is not affected.
"""
from __future__ import annotations

from typing import Callable

from opencohost.config.settings import load_tts_speed

SPEED_PRESETS: tuple[tuple[str, float], ...] = (
    ("Rápida", 1.0),
    ("Media", 1.15),
    ("Calma", 1.30),
    ("Lenta", 1.45),
)


def _ctk():
    """Import seam so tests can run headless without customtkinter."""
    import customtkinter
    return customtkinter


def scale_for_label(label: str) -> float | None:
    """Return the length_scale for a preset label, or None if unknown."""
    for preset_label, scale in SPEED_PRESETS:
        if preset_label == label:
            return scale
    return None


def closest_preset_label(scale: float) -> str:
    """Return the preset label closest to an arbitrary persisted scale."""
    return min(SPEED_PRESETS, key=lambda preset: abs(preset[1] - float(scale)))[0]


def build_tts_speed_selector(parent, dispatch: Callable[[float], None]):
    """Build the Piper speed preset selector inside *parent*.

    Creates a caption label and a segmented button with one option per
    preset, preselects the persisted speed, and calls *dispatch* with the
    chosen length_scale whenever the user picks a preset.
    """
    ctk = _ctk()

    def on_select(label: str) -> None:
        scale = scale_for_label(label)
        if scale is not None:
            dispatch(scale)

    ctk.CTkLabel(
        parent,
        text="Velocidad de voz local (Piper). No afecta a Edge-TTS.",
        font=ctk.CTkFont(size=10),
        text_color="#8fa3b8",
        anchor="w",
        justify="left",
        wraplength=400,
    ).pack(fill="x", padx=10, pady=(6, 0))
    selector = ctk.CTkSegmentedButton(
        parent,
        values=[label for label, _scale in SPEED_PRESETS],
        command=on_select,
    )
    selector.set(closest_preset_label(load_tts_speed()))
    selector.pack(fill="x", padx=10, pady=(2, 4))
    return selector
