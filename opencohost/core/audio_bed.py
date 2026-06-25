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

    def __post_init__(self) -> None:
        # Clamp so a bad config can never produce an invalid mix:
        # 0.0 <= ducked_volume <= base_volume <= 1.0
        self.base_volume = max(0.0, min(float(self.base_volume), 1.0))
        self.ducked_volume = max(0.0, min(float(self.ducked_volume), self.base_volume))


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
        self._is_ducked: bool = False
        self._lock = threading.RLock()
        # Generation counter: bumped by stop() and each new _play_selected call so
        # an in-flight off-lock decode can detect it has been superseded.
        self._play_seq: int = 0
        # Idle loop tracking
        self._last_interaction: float = time.time()
        self._idle_loop_count: int = 0
        self._last_looping_track_id: str | None = None
        self._idle_check_timer: threading.Timer | None = None
        self._idle_stopped: bool = False

    def request_mood(self, mood: str, *, force: bool = False, boundary: bool = False, only_if_idle: bool = False) -> bool:
        """Request a mood; current songs keep inertia unless forced or safe.

        The state decision (should we play at all?) is made under the lock.
        The actual _play_selected call runs OUTSIDE the lock so the
        synchronous disk decode in Phase 2 never stalls other lock holders.

        only_if_idle: when True, skip the request if current_track is already
        set (atomically inside the lock, eliminating the check-then-act race
        that two concurrent callers would otherwise encounter).
        """
        with self._lock:
            self._mark_interaction()
            self._idle_stopped = False  # user action clears idle-stop block
            self.desired_mood = normalize_mood(mood)
            if not self.enabled:
                return False
            if only_if_idle and self.current_track is not None:
                return False
            if self.current_track and not force and not boundary and not self._can_transition_now():
                self.transition_pending = True
                return False
            target = self.desired_mood
        # Lock released here — _play_selected acquires/releases its own lock
        # internally for each of its three phases.
        return self._play_selected(target)

    def on_boundary(self) -> bool:
        # Bug 2 fix: do NOT call _mark_interaction() here.
        # on_boundary fires on every Kira speaking-end; calling _mark_interaction
        # perpetually resets _last_interaction, making the idle-drain threshold
        # (max_play_seconds * idle_loop_limit) unreachable under sustained Kira
        # activity.  Reserve _mark_interaction for genuine human-initiated events
        # (PTT, chat send, direct mood request via request_mood).
        with self._lock:
            if not self.enabled:
                return False
            if self.transition_pending or self._can_transition_now():
                self.transition_pending = False
                target = self.desired_mood
            else:
                return False
        # Lock released — _play_selected runs its three phases with its own lock
        # acquisitions so the off-lock decode does not stall other callers.
        return self._play_selected(target)

    def duck(self) -> None:
        with self._lock:
            self._mark_interaction()
            self._is_ducked = True
            channel = self._channel
            if channel:
                channel.set_volume(self.policy.ducked_volume)

    def unduck(self) -> None:
        with self._lock:
            self._is_ducked = False
            channel = self._channel
            if channel:
                channel.set_volume(self.policy.base_volume)

    def stop(self, *, emergency: bool = False) -> None:
        with self._lock:
            # Bump generation so any in-flight off-lock decode is superseded
            # and cannot publish music after this stop completes.
            self._play_seq += 1
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
        # Bug 3 fix: when current_track is None (i.e. music was explicitly stopped)
        # and there is no pending transition, return False so on_boundary() never
        # auto-restarts music without an explicit request_mood() call.
        # Also honour _idle_stopped: the idle-drain mechanism set it precisely to
        # prevent auto-restart after the loop limit was reached (#751 regression).
        if self._idle_stopped:
            return False
        if not self.current_track and not self.transition_pending:
            return False
        if not self.current_track:
            # transition_pending is True but there is nothing playing yet — allow
            return True
        elapsed = time.time() - self.started_at
        return elapsed >= self.policy.min_play_seconds or elapsed >= self.policy.max_play_seconds

    def _play_selected(self, mood: str) -> bool:
        """Select and play a track for the given mood.

        This method is always called WITHOUT the lock held (request_mood and
        on_boundary both release the lock before calling here).  It runs three
        distinct phases to keep the synchronous disk decode (Phase 2) off the lock:

        Phase 1 — under self._lock: all candidate selection logic, same-track
                   no-op check, pygame init, and generation-counter claim.
        Phase 2 — NO lock: mixer.Sound(track.path) — the only blocking I/O.
        Phase 3 — under self._lock: supersession guard, then channel publish.
        """
        # ── Phase 1: candidate selection (under lock) ───────────────────────
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
            # Skip the track that was looping before silence (if any).
            # READ the id here but do NOT null it yet — only the WINNING generation
            # (the one that passes the Phase-3 guard) may consume it.  Nulling here
            # lets a concurrent Phase-1 see a already-cleared id and fail to filter,
            # defeating the anti-loop guard (#751 regression fix).
            consumed_looping_id: str | None = None
            if self._last_looping_track_id and len(candidates) > 1:
                filtered = [t for t in candidates if t.id != self._last_looping_track_id]
                if filtered:
                    candidates = filtered
                    consumed_looping_id = self._last_looping_track_id
                # Do NOT set self._last_looping_track_id = None here.

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
            # Do NOT write _mood_last_index here — defer to Phase 3 so only the
            # WINNING generation commits the index update.
            if self.current_track and track.id == self.current_track.id:
                return True

            self._ensure_pygame()
            # Claim a generation slot and capture pygame ref before releasing
            self._play_seq += 1
            seq = self._play_seq
            pygame = self._pygame
        # Lock released — Phase 2 runs without holding it.

        # ── Phase 2: disk decode (NO lock) ──────────────────────────────────
        try:
            sound = pygame.mixer.Sound(track.path)
        except Exception as exc:
            self.on_log(f"[Música] No se pudo reproducir {track.original_name}: {exc}")
            return False

        # ── Phase 3: publish (under lock) ───────────────────────────────────
        with self._lock:
            # Supersession guard: if stop() or a newer request was made while we
            # were decoding, our generation ticket is stale — drop this decode.
            if seq != self._play_seq:
                return False
            # Commit deferred selection state — only the winning generation reaches
            # here, so these writes are safe from the concurrent Phase-1 race.
            self._mood_last_index[mood_key] = pos
            if consumed_looping_id is not None:
                self._last_looping_track_id = None
            old_channel = self._channel
            self._channel = pygame.mixer.find_channel(force=True)
            if self._channel is None:
                self._channel = pygame.mixer.Channel(7)
            initial_vol = (
                self.policy.ducked_volume if self._is_ducked
                else self.policy.base_volume
            )
            self._channel.set_volume(initial_vol)
            self._channel.play(sound, loops=-1, fade_ms=self.policy.fade_ms)
            if old_channel and old_channel is not self._channel:
                old_channel.fadeout(self.policy.fade_ms)
            self._sound = sound
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
