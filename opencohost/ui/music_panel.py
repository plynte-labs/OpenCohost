"""Music bed controls for mood-tagged stream loops."""

from __future__ import annotations

from typing import Any, Callable, Optional

import customtkinter as ctk

from opencohost.core.music_library import KNOWN_MOODS, MusicTrack


class MusicPanel:
    """Operator UI for importing, tagging and previewing music beds."""

    def __init__(
        self,
        *,
        on_import: Optional[Callable[[str], None]] = None,
        on_play_mood: Optional[Callable[[str], None]] = None,
        on_stop: Optional[Callable[[], None]] = None,
        on_cleanup_missing: Optional[Callable[[], None]] = None,
        on_delete_track: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._on_import = on_import or (lambda mood: None)
        self._on_play_mood = on_play_mood or (lambda mood: None)
        self._on_stop = on_stop or (lambda: None)
        self._on_cleanup_missing = on_cleanup_missing or (lambda: None)
        self._on_delete_track = on_delete_track or (lambda track_id: None)
        self._listed_tracks: list[MusicTrack] = []
        self._widgets: dict[str, Any] = {}

    def build(self, parent: Any) -> Any:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(0, weight=1)
        root = ctk.CTkScrollableFrame(parent, fg_color="#0f151c", corner_radius=18)
        root.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        root.grid_columnconfigure(0, weight=1)

        hero = ctk.CTkFrame(root, fg_color="#162232", corner_radius=18)
        hero.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 8))
        hero.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(hero, text="Música / Mood", font=ctk.CTkFont(size=24, weight="bold"), anchor="w").grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 2))
        ctk.CTkLabel(
            hero,
            text="Loops locales por estado emocional. El usuario etiqueta; el sistema valida, hace fade, ducking y fallback sin cortar el flow salvo necesidad.",
            text_color="#a9bdd3",
            anchor="w",
            justify="left",
            wraplength=760,
        ).grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 14))

        import_box = ctk.CTkFrame(root, fg_color="#151d26", corner_radius=16)
        import_box.grid(row=1, column=0, sticky="ew", padx=12, pady=8)
        import_box.grid_columnconfigure(0, weight=1)
        import_box.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(import_box, text="Importar track", font=ctk.CTkFont(size=16, weight="bold"), anchor="w").grid(row=0, column=0, columnspan=2, sticky="ew", padx=14, pady=(12, 4))
        combo = ctk.CTkOptionMenu(import_box, values=[m.capitalize() for m in KNOWN_MOODS])
        combo.set("Normal")
        combo.grid(row=1, column=0, sticky="ew", padx=(14, 6), pady=(4, 12))
        self._widgets["combo_mood"] = combo
        ctk.CTkButton(import_box, text="Subir .mp3/.wav", command=self._dispatch_import, fg_color="#2f5f8f").grid(row=1, column=1, sticky="ew", padx=(6, 14), pady=(4, 12))

        controls = ctk.CTkFrame(root, fg_color="#151d26", corner_radius=16)
        controls.grid(row=2, column=0, sticky="ew", padx=12, pady=8)
        for col in range(4):
            controls.grid_columnconfigure(col, weight=1)
        ctk.CTkLabel(controls, text="Prueba rápida", font=ctk.CTkFont(size=16, weight="bold"), anchor="w").grid(row=0, column=0, columnspan=4, sticky="ew", padx=14, pady=(12, 4))
        for idx, mood in enumerate(KNOWN_MOODS[:4]):
            ctk.CTkButton(controls, text=mood.capitalize(), command=lambda m=mood: self._on_play_mood(m), fg_color="#555555").grid(row=1, column=idx, sticky="ew", padx=4, pady=4)
        for idx, mood in enumerate(KNOWN_MOODS[4:]):
            ctk.CTkButton(controls, text=mood.capitalize(), command=lambda m=mood: self._on_play_mood(m), fg_color="#555555").grid(row=2, column=idx, sticky="ew", padx=4, pady=4)
        ctk.CTkButton(controls, text="Fade out", command=self._on_stop, fg_color="#7d5a2a").grid(row=3, column=0, columnspan=2, sticky="ew", padx=(14, 4), pady=(8, 12))
        ctk.CTkButton(controls, text="Limpiar faltantes", command=self._on_cleanup_missing, fg_color="#8f2f2f").grid(row=3, column=2, columnspan=2, sticky="ew", padx=(4, 14), pady=(8, 12))

        stats = ctk.CTkFrame(root, fg_color="#151d26", corner_radius=16)
        stats.grid(row=3, column=0, sticky="ew", padx=12, pady=8)
        stats.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(stats, text="Biblioteca", font=ctk.CTkFont(size=16, weight="bold"), anchor="w").grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 4))
        lbl_counts = ctk.CTkLabel(stats, text="Sin música cargada.", text_color="#a9bdd3", anchor="w", justify="left", wraplength=760)
        lbl_counts.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 4))
        self._widgets["lbl_counts"] = lbl_counts
        text_tracks = ctk.CTkTextbox(stats, height=180, fg_color="#0f151c", text_color="#d8e2ef", wrap="word")
        text_tracks.grid(row=2, column=0, sticky="ew", padx=14, pady=(4, 14))
        text_tracks.configure(state="disabled")
        self._widgets["text_tracks"] = text_tracks

        delete_row = ctk.CTkFrame(stats, fg_color="transparent")
        delete_row.grid(row=3, column=0, sticky="ew", padx=14, pady=(0, 14))
        delete_row.grid_columnconfigure(0, weight=1)
        combo_delete = ctk.CTkOptionMenu(delete_row, values=["Sin tracks"], state="disabled")
        combo_delete.grid(row=0, column=0, sticky="ew", padx=(0, 8), pady=0)
        self._widgets["combo_delete_track"] = combo_delete
        ctk.CTkButton(delete_row, text="Eliminar track", command=self._dispatch_delete_track, fg_color="#8f2f2f").grid(row=0, column=1, sticky="ew", pady=0)
        return root

    def update_library(self, counts: dict[str, int], tracks: list[MusicTrack]) -> None:
        self._listed_tracks = tracks
        counts_text = " · ".join(f"{mood.capitalize()} ({counts.get(mood, 0)})" for mood in KNOWN_MOODS)
        self._widgets["lbl_counts"].configure(text=counts_text)
        lines = []
        for track in tracks:
            status = "faltante" if track.missing else "inválido" if track.invalid else "OK"
            lines.append(f"[{status}] {track.label} — {track.original_name} ({track.mood})")
        text = self._widgets["text_tracks"]
        text.configure(state="normal")
        text.delete("1.0", "end")
        text.insert("end", "\n".join(lines) if lines else "No hay tracks todavía.")
        text.configure(state="disabled")

        combo_delete = self._widgets["combo_delete_track"]
        labels = [self._delete_label(track) for track in tracks]
        if labels:
            combo_delete.configure(values=labels, state="normal")
            combo_delete.set(labels[0])
        else:
            combo_delete.configure(values=["Sin tracks"], state="disabled")
            combo_delete.set("Sin tracks")

    def _dispatch_import(self) -> None:
        mood = self._widgets["combo_mood"].get().lower()
        self._on_import(mood)

    def _dispatch_delete_track(self) -> None:
        selected = self._widgets["combo_delete_track"].get()
        for track in self._listed_tracks:
            if selected == self._delete_label(track):
                self._on_delete_track(track.id)
                return

    @staticmethod
    def _delete_label(track: MusicTrack) -> str:
        status = "faltante" if track.missing else "inválido" if track.invalid else "OK"
        return f"{track.label} — {track.original_name} [{status}]"
