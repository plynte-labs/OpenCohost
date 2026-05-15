"""Background music bed engine with mood fallback, fade and ducking."""

from __future__ import annotations

import time
import threading
from dataclasses import dataclass
from typing import Callable, Optional

from core.music_library import MusicLibrary, MusicTrack, normalize_mood


@dataclass
class AudioBedPolicy:
    min_play_seconds: float = 90.0
    max_play_seconds: float = 300.0
    fade_ms: int = 1800
    base_volume: float = 0.28
    ducked_volume: float = 0.08


class AudioBedEngine:
    """Small runtime controller for long-lived production music beds.

    Uses a dedicated pygame mixer Channel instead of ``mixer.music`` so Kira's
    TTS pipeline can keep using the music channel for speech chunks.
    """

    def __init__(self, library: MusicLibrary, *, policy: AudioBedPolicy | None = None, on_log: Optional[Callable[[str], None]] = None) -> None:
        self.library = library
        self.policy = policy or AudioBedPolicy()
        self.on_log = on_log or (lambda msg: None)
        self.current_track: MusicTrack | None = None
        self.current_mood: str = "normal"
        self.desired_mood: str = "normal"
        self.started_at: float = 0.0
        self.transition_pending: bool = False
        self.enabled: bool = True
        self._pygame = None
        self._channel = None
        self._sound = None
        self._lock = threading.RLock()

    def request_mood(self, mood: str, *, force: bool = False, boundary: bool = False) -> bool:
        """Request a mood; current songs keep inertia unless forced or safe."""
        with self._lock:
            self.desired_mood = normalize_mood(mood)
            if not self.enabled:
                return False
            if self.current_track and not force and not boundary and not self._can_transition_now():
                self.transition_pending = True
                return False
            return self._play_selected(self.desired_mood)

    def on_boundary(self) -> bool:
        with self._lock:
            if self.transition_pending or self._can_transition_now():
                self.transition_pending = False
                return self._play_selected(self.desired_mood)
            return False

    def duck(self) -> None:
        with self._lock:
            channel = self._channel
            if channel:
                channel.set_volume(self.policy.ducked_volume)

    def unduck(self) -> None:
        with self._lock:
            channel = self._channel
            if channel:
                channel.set_volume(self.policy.base_volume)

    def stop(self, *, emergency: bool = False) -> None:
        with self._lock:
            channel = self._channel
            if channel:
                if emergency:
                    channel.stop()
                else:
                    channel.fadeout(self.policy.fade_ms)
            self.current_track = None
            self.transition_pending = False

    def _can_transition_now(self) -> bool:
        if not self.current_track:
            return True
        elapsed = time.time() - self.started_at
        return elapsed >= self.policy.min_play_seconds or elapsed >= self.policy.max_play_seconds

    def _play_selected(self, mood: str) -> bool:
        with self._lock:
            track = self.library.select_for_mood(mood, avoid_track_id=self.current_track.id if self.current_track else None)
            if not track:
                self.on_log("[Música] No hay tracks válidos; se mantiene silencio.")
                return False
            if self.current_track and track.id == self.current_track.id:
                return True
            try:
                self._ensure_pygame()
                sound = self._pygame.mixer.Sound(track.path)
                if self._channel:
                    self._channel.fadeout(self.policy.fade_ms)
                self._sound = sound
                self._channel = self._pygame.mixer.Channel(7)
                self._channel.set_volume(self.policy.base_volume)
                self._channel.play(sound, loops=-1, fade_ms=self.policy.fade_ms)
            except Exception as exc:  # pragma: no cover - depends on local audio backend
                self.on_log(f"[Música] No se pudo reproducir {track.original_name}: {exc}")
                return False
            self.current_track = track
            self.current_mood = track.mood
            self.started_at = time.time()
            if track.mood != mood:
                self.on_log(f"[Música] No hay tracks para {mood}; usando {track.label} como fallback.")
            else:
                self.on_log(f"[Música] Reproduciendo {track.label}: {track.original_name}")
            return True

    def _ensure_pygame(self) -> None:
        if self._pygame is not None:
            return
        import pygame

        if not pygame.mixer.get_init():
            pygame.mixer.init()
        self._pygame = pygame
