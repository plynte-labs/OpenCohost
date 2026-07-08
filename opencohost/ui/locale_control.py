"""CTK "Idioma" language control (kira_bilingual_e2e_20260705, P7 config
surface — D6: next-boot only, no engine dispatch, no hot-swap).

Extracted out of app_shell.py (kira_bilingual_e2e_20260705 verify-fix) to
respect that module's hard line-count budget (tests/test_integration.py::
test_app_shell_line_count_under_1500). Unlike the personalization_panel
precedent (e099184), no existing panel class already owns the parent frame
here (frame_tts_memory is built directly in app_shell), so this module owns
the Idioma widgets, callbacks, and restart-required notice standalone and is
wired in with a single `mount()` call — nothing outside this module reads
the resulting widgets.
"""
from __future__ import annotations

from typing import Any, Dict

import customtkinter as ctk

from opencohost.i18n import active as i18n_active
from opencohost.i18n import state as i18n_state
from opencohost.i18n.startup import load_registry as i18n_load_registry

RESTART_BANNER = "Se aplicará en el próximo inicio de OpenCohost."


def available_bundles() -> Dict[str, str]:
    """Return {display_name: code} for every discovered locale bundle
    (reuses the same registry discovery as GET /api/i18n, zero new state)."""
    registry = i18n_load_registry()
    return {
        str((bundle.data.get("meta") or {}).get("display", code)): code
        for code, bundle in sorted(registry.items())
    }


def on_change(idioma_labels: Dict[str, str], lbl_idioma_restart: Any, label: str) -> None:
    """Persist the selected locale for the NEXT start (D6: next-boot
    only). NO engine dispatch — there is nothing to hot-swap; the running
    process keeps the bundle it loaded at startup."""
    code = idioma_labels.get(label)
    if not code:
        return
    i18n_state.set_locale(code)
    active_code = i18n_active.get_active_bundle().code
    lbl_idioma_restart.configure(
        text=RESTART_BANNER if code != active_code else ""
    )


def mount(parent_frame: ctk.CTkFrame) -> None:
    """Build the Idioma card into `parent_frame` (same widgets, order, and
    callbacks P7 shipped in app_shell.py)."""
    frame_idioma = ctk.CTkFrame(parent_frame, fg_color="#101923", corner_radius=10)
    frame_idioma.pack(fill="x", padx=10, pady=(0, 10))
    ctk.CTkLabel(frame_idioma, text="Idioma", font=ctk.CTkFont(size=13, weight="bold"), anchor="w").pack(fill="x", padx=10, pady=(10, 2))
    idioma_labels = available_bundles()
    _idioma_persisted = i18n_state.get_locale()
    _idioma_active = i18n_active.get_active_bundle().code
    _idioma_current_label = next(
        (lbl for lbl, code in idioma_labels.items() if code == _idioma_persisted),
        next(iter(idioma_labels), ""),
    )
    combo_idioma = ctk.CTkOptionMenu(
        frame_idioma, values=list(idioma_labels.keys()),
        command=lambda label: on_change(idioma_labels, lbl_idioma_restart, label),
    )
    combo_idioma.pack(fill="x", padx=10, pady=(0, 4))
    if _idioma_current_label:
        combo_idioma.set(_idioma_current_label)
    lbl_idioma_restart = ctk.CTkLabel(
        frame_idioma,
        text=(RESTART_BANNER if _idioma_persisted != _idioma_active else ""),
        font=ctk.CTkFont(size=10), text_color="#e0b64a", anchor="w", justify="left", wraplength=400,
    )
    lbl_idioma_restart.pack(fill="x", padx=10, pady=(0, 10))
