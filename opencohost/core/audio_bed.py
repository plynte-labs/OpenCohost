"""Background music bed engine with mood fallback, fade and ducking."""

from __future__ import annotations

import time
import random
import threading
from dataclasses import dataclass
from typing import Callable, Optional

from opencohost.core.music_library import MusicLibrary, MusicTrack, normalize_mood


@dataclass
class AudioBedPolicy:
    min_play_seconds: float = 90.0
    max_play_seconds: float = 300.0
    fade_ms: int = 6000
    base_volume: float = 0.28
    ducked_volume: float = 0.08
    idle_loop_limit: int = 2        # max loops with no Kira interaction before stopping
    idle_check_interval: float = 30.0  # seconds between idle checks


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
        self._mood_last_index: dict[str, int] = {}
        self._pygame = None
        self._channel = None
        self._sound = None
        self._lock = threading.RLock()
        # Idle loop tracking
        self._last_interaction: float = time.time()
        self._idle_loop_count: int = 0
        self._last_looping_track_id: str | None = None
        self._idle_check_timer: threading.Timer | None = None
        self._idle_stopped: bool = False

    def request_mood(self, mood: str, *, force: bool = False, boundary: bool = False) -> bool:
        """Request a mood; current songs keep inertia unless forced or safe."""
        with self._lock:
            self._mark_interaction()
            self._idle_stopped = False  # user action clears idle-stop block
            self.desired_mood = normalize_mood(mood)
            if not self.enabled:
                return False
            if self.current_track and not force and not boundary and not self._can_transition_now():
                self.transition_pending = True
                return False
            return self._play_selected(self.desired_mood)

    def on_boundary(self) -> bool:
        with self._lock:
            self._mark_interaction()
            if self.transition_pending or self._can_transition_now():
                self.transition_pending = False
                return self._play_selected(self.desired_mood)
            return False

    def duck(self) -> None:
        with self._lock:
            self._mark_interaction()
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
            self._cancel_idle_check()

    def shutdown(self) -> None:
        """Cancel timers and stop. Call before destroying the engine."""
        self.stop(emergency=True)
        self._cancel_idle_check()

    # ── idle / anti-infinite-loop ────────────────────────────────────────

    def _mark_interaction(self) -> None:
        """Reset the idle loop counter — Kira or the streamer did something."""
        self._last_interaction = time.time()
        self._idle_loop_count = 0
        self._start_idle_check()

    def _start_idle_check(self) -> None:
        """Schedule next idle loop check in a background timer."""
        self._cancel_idle_check()
        self._idle_check_timer = threading.Timer(
            self.policy.idle_check_interval, self._check_idle,
        )
        self._idle_check_timer.daemon = True
        self._idle_check_timer.start()

    def _cancel_idle_check(self) -> None:
        if self._idle_check_timer is not None:
            self._idle_check_timer.cancel()
            self._idle_check_timer = None

    def _check_idle(self) -> None:
        """Background timer: if music has looped too long without interaction, stop."""
        with self._lock:
            self._idle_check_timer = None
            if not self.current_track or not self._channel:
                return
            if not self._channel.get_busy():
                return  # nothing playing

            elapsed = time.time() - self._last_interaction
            max_allowed = self.policy.max_play_seconds * self.policy.idle_loop_limit

            if elapsed < max_allowed:
                # Still within limit — reschedule
                self._start_idle_check()
                return

            # Exceeded idle loop limit — fade out, remember track, block auto-restart
            self._last_looping_track_id = self.current_track.id
            self._idle_stopped = True
            self.on_log(
                f"[Música] Límite de loop inactivo alcanzado ({self.policy.idle_loop_limit}x). "
                f"Deteniendo {self.current_track.label}."
            )
            self.stop(emergency=False)

    def _can_transition_now(self) -> bool:
        if not self.current_track:
            return True
        elapsed = time.time() - self.started_at
        return elapsed >= self.policy.min_play_seconds or elapsed >= self.policy.max_play_seconds

    def _play_selected(self, mood: str) -> bool:
        with self._lock:
            mood_key = normalize_mood(mood)
            valid = self.library.valid_tracks()
            candidates: list[MusicTrack] = []
            for bucket in (mood_key, "normal"):
                bucket_tracks = [t for t in valid if t.mood == bucket]
                if bucket_tracks:
                    bucket_tracks.sort(key=lambda t: (t.variant_index, t.label))
                    candidates = bucket_tracks
                    break
            if not candidates:
                candidates = sorted(valid, key=lambda t: (t.variant_index, t.label))

            avoid_id = self.current_track.id if self.current_track else None
            if avoid_id and len(candidates) > 1:
                filtered = [t for t in candidates if t.id != avoid_id]
                if filtered:
                    candidates = filtered
            # Skip the track that was looping before silence (if any)
            if self._last_looping_track_id and len(candidates) > 1:
                filtered = [t for t in candidates if t.id != self._last_looping_track_id]
                if filtered:
                    candidates = filtered
                self._last_looping_track_id = None  # consumed

            if not candidates:
                self.on_log("[Música] No hay tracks válidos; se mantiene silencio.")
                return False

            last_pos = self._mood_last_index.get(mood_key)
            if last_pos is not None and last_pos >= len(candidates):
                last_pos = None
            if last_pos is None:
                pos = random.randint(0, len(candidates) - 1)
            else:
                jump = random.randint(1, min(3, len(candidates) - 1)) if len(candidates) > 1 else 0
                pos = (last_pos + jump) % len(candidates)
                if len(candidates) > 1 and pos == last_pos:
                    pos = (pos + 1) % len(candidates)

            track = candidates[pos]
            self._mood_last_index[mood_key] = pos
            if self.current_track and track.id == self.current_track.id:
                return True
            try:
                self._ensure_pygame()
                sound = self._pygame.mixer.Sound(track.path)
                old_channel = self._channel
                self._channel = self._pygame.mixer.find_channel(force=True)
                if self._channel is None:
                    self._channel = self._pygame.mixer.Channel(7)
                self._channel.set_volume(self.policy.base_volume)
                self._channel.play(sound, loops=-1, fade_ms=self.policy.fade_ms)
                if old_channel and old_channel is not self._channel:
                    old_channel.fadeout(self.policy.fade_ms)
                self._sound = sound
            except Exception as exc:  # pragma: no cover - depends on local audio backend
                self.on_log(f"[Música] No se pudo reproducir {track.original_name}: {exc}")
                return False
            self.current_track = track
            self.current_mood = track.mood
            self.started_at = time.time()
            self._start_idle_check()  # schedule idle loop detection for new track
            if track.mood != mood_key:
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
