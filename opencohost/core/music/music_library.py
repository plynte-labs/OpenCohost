"""Mood-tagged local music library for Kira production beds."""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

from opencohost.config.settings import MUSIC_DIR, MUSIC_CONFIG_FILE
from opencohost.config.storage import atomic_write_text
from opencohost.config.logger import get_logger

logger = get_logger()


MUSIC_DIR = Path(MUSIC_DIR)
MUSIC_CONFIG_FILE = Path(MUSIC_CONFIG_FILE)
ALLOWED_AUDIO_EXTENSIONS = {".mp3", ".wav"}
KNOWN_MOODS = ("normal", "nostalgia", "hype", "tension", "sad", "calm", "comedy", "ending")


@dataclass
class MusicTrack:
    id: str
    original_name: str
    path: str
    mood: str
    label: str
    variant_index: int = 0
    enabled: bool = True
    missing: bool = False
    invalid: bool = False
    # Import idempotency key: "<resolved source>|<st_size>|<st_mtime_ns>".
    # Old json rows lack it (dataclass default fills ""); NEW json read by
    # pre-fix code drops these tracks via the load-time TypeError guard —
    # one-way format, no rollback contract (design D5 / WU4).
    source_sig: str = ""


def normalize_mood(mood: str) -> str:
    normalized = re.sub(r"[^a-z0-9_]+", "_", (mood or "normal").strip().lower()).strip("_")
    return normalized if normalized in KNOWN_MOODS else "normal"


def is_supported_audio_path(path: Path) -> bool:
    """Validate final extension and basic file signature; names alone are not trusted."""
    suffix = path.suffix.lower()
    if suffix not in ALLOWED_AUDIO_EXTENSIONS or not path.is_file():
        return False
    try:
        with path.open("rb") as fh:
            header = fh.read(12)
    except OSError:
        return False
    if suffix == ".wav":
        return header[:4] == b"RIFF" and header[8:12] == b"WAVE"
    if suffix == ".mp3":
        return header.startswith(b"ID3") or (len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xE0) == 0xE0)
    return False


class MusicLibrary:
    """Persistent mapping from user-selected files to mood buckets."""

    def __init__(self, *, library_dir: Path = MUSIC_DIR, config_file: Path = MUSIC_CONFIG_FILE) -> None:
        self.library_dir = Path(library_dir)
        self.config_file = Path(config_file)
        self.tracks: dict[str, MusicTrack] = {}

    def load(self) -> None:
        self.tracks = {}
        if not self.config_file.exists():
            return
        try:
            data = json.loads(self.config_file.read_text(encoding="utf-8"))
        except OSError:
            # Transient read failure (locked/permission) — do not quarantine a
            # potentially healthy file; leave it in place and load nothing.
            return
        except json.JSONDecodeError:
            self._quarantine_corrupt()
            return
        if not isinstance(data, dict):
            self._quarantine_corrupt()
            return
        for raw in data.get("tracks", []):
            try:
                track = MusicTrack(**raw)
            except TypeError:
                continue
            self.tracks[track.id] = self._refresh_track_state(track)

    def _quarantine_corrupt(self) -> None:
        """Rename a corrupt config to ``<file>.corrupt`` instead of silently
        loading empty — a crash-truncated file is preserved for inspection and
        the next save starts clean rather than fail-open-to-nothing."""
        corrupt = self.config_file.with_name(self.config_file.name + ".corrupt")
        try:
            os.replace(self.config_file, corrupt)
        except OSError:
            pass
        logger.warning("music library config corrupt — quarantined to %s", corrupt)

    def save(self) -> None:
        payload = {"tracks": [asdict(track) for track in self.tracks.values()]}
        atomic_write_text(self.config_file, json.dumps(payload, ensure_ascii=False, indent=2))

    def find_existing(self, source: str | Path, mood: str) -> MusicTrack | None:
        """Dedup pre-check (read-only) so a caller can short-circuit BEFORE the
        expensive stage_copy. Idempotency key = (source_sig, mood); the same
        file to a DIFFERENT mood is intentionally a new variant (WU4/D5).
        Caller holds the lock — this only reads self.tracks."""
        source_sig = self._source_sig(Path(source))
        if not source_sig:
            return None
        mood_key = normalize_mood(mood)
        for existing in self.tracks.values():
            if existing.source_sig == source_sig and existing.mood == mood_key:
                return existing
        return None

    def stage_copy(self, source: str | Path) -> Path:
        """Copy the source into library_dir under a fresh uuid name and return
        the staged path. Validates the audio signature first. Touches NO
        self.tracks state and computes no variant/label, so it is safe to call
        OUTSIDE the music lock — the up-to-200MB copy must not block concurrent
        control-plane reads (WU5). The label is assigned later in
        register_staged; the on-disk name stays mood-free."""
        source_path = Path(source)
        if not is_supported_audio_path(source_path):
            raise ValueError("Solo se aceptan archivos .mp3 o .wav válidos.")
        self.library_dir.mkdir(parents=True, exist_ok=True)
        staged = self.library_dir / f"{uuid.uuid4().hex}{source_path.suffix.lower()}"
        shutil.copy2(source_path, staged)
        return staged

    def register_staged(self, staged: Path, source: str | Path, mood: str) -> MusicTrack:
        """Register an already-staged copy as a track. Caller holds the lock.
        Rechecks dedup (a concurrent import of the same source may have won the
        race while we copied) and on that hit discards our staged copy and
        returns the winner. On save failure ROLLS BACK: pop the track and
        unlink the staged file, then re-raise — a 503 must never leave a live
        track or an orphan copy behind (WU5/D7)."""
        source_path = Path(source)
        mood_key = normalize_mood(mood)
        source_sig = self._source_sig(source_path)
        if source_sig:
            for existing in self.tracks.values():
                if existing.source_sig == source_sig and existing.mood == mood_key:
                    self._discard_staged(staged)
                    return existing
        variant_index = self._next_variant_index(mood_key)
        label = self._label_for(mood_key, variant_index)
        track = MusicTrack(
            id=uuid.uuid4().hex,
            original_name=source_path.name,
            path=str(staged),
            mood=mood_key,
            label=label,
            variant_index=variant_index,
            source_sig=source_sig,
        )
        self.tracks[track.id] = track
        try:
            self.save()
        except BaseException:
            self.tracks.pop(track.id, None)
            self._discard_staged(staged)
            raise
        return track

    def add_file(self, source: str | Path, mood: str) -> MusicTrack:
        """Thin wrapper: dedup pre-check -> stage the copy -> register it. Kept
        for direct callers/tests; the API handler splits these three so the
        copy runs OUTSIDE the music lock (WU5)."""
        existing = self.find_existing(source, mood)
        if existing is not None:
            return existing
        staged = self.stage_copy(source)
        return self.register_staged(staged, source, mood)

    @staticmethod
    def _discard_staged(staged: Path) -> None:
        try:
            staged.unlink(missing_ok=True)
        except OSError:
            pass

    def remove(self, track_id: str, *, delete_file: bool = False, raising: bool = False) -> bool:
        track = self.tracks.get(track_id)
        if not track:
            return False
        # Delete the managed file BEFORE deregistering: if an in-flight audio
        # stream holds the handle open (Windows blocks unlink), _delete_managed_file
        # raises OSError. With raising=True (API) we keep the track registered and
        # re-raise so the caller can surface a retryable 503 instead of dropping a
        # track mid-stream (WU5/D8). With raising=False (CTK desktop) we fail open:
        # deregister anyway so a locked file can't crash the Tk callback (the file
        # is left orphaned, matching the pre-WU5 behavior).
        if delete_file:
            try:
                self._delete_managed_file(track)
            except OSError:
                if raising:
                    raise
        self.tracks.pop(track_id, None)
        self.save()
        return True

    def cleanup_missing(self) -> int:
        missing_ids = [track_id for track_id, track in self.tracks.items() if self._refresh_track_state(track).missing]
        for track_id in missing_ids:
            self.tracks.pop(track_id, None)
        if missing_ids:
            self.save()
        return len(missing_ids)

    def counts_by_mood(self) -> dict[str, int]:
        counts = {mood: 0 for mood in KNOWN_MOODS}
        for track in self.valid_tracks():
            counts[track.mood] = counts.get(track.mood, 0) + 1
        return counts

    def valid_tracks(self) -> list[MusicTrack]:
        return [self._refresh_track_state(track) for track in self.tracks.values() if track.enabled and not self._refresh_track_state(track).missing and not self._refresh_track_state(track).invalid]

    def select_for_mood(self, mood: str, *, avoid_track_id: str | None = None, prefer_after_index: int = -1) -> MusicTrack | None:
        mood_key = normalize_mood(mood)
        valid = self.valid_tracks()
        for bucket in (mood_key, "normal", "__any__"):
            candidates = valid if bucket == "__any__" else [track for track in valid if track.mood == bucket]
            if avoid_track_id and len(candidates) > 1:
                candidates = [track for track in candidates if track.id != avoid_track_id]
            if candidates:
                if prefer_after_index >= 0 and len(candidates) > 1:
                    candidates.sort(key=lambda track: (track.variant_index, track.label))
                    after = [t for t in candidates if t.variant_index > prefer_after_index]
                    if after:
                        return after[0]
                return sorted(candidates, key=lambda track: (track.variant_index, track.label))[0]
        return None

    def all_tracks(self) -> Iterable[MusicTrack]:
        for track in self.tracks.values():
            yield self._refresh_track_state(track)

    def _delete_managed_file(self, track: MusicTrack) -> None:
        """Delete only files stored inside the app-managed music directory."""
        try:
            library_root = self.library_dir.resolve()
            track_path = Path(track.path).resolve()
        except OSError:
            return
        try:
            is_managed = track_path.is_relative_to(library_root)
        except AttributeError:
            is_managed = library_root == track_path or library_root in track_path.parents
        if not is_managed:
            return
        # OSError propagates: on Windows an in-flight audio stream's open handle
        # blocks unlink -> the caller keeps the track and returns a retryable
        # 503 rather than dropping a track whose bytes are still being served
        # (WU5/D8). missing_ok=True already tolerates an already-gone file.
        track_path.unlink(missing_ok=True)

    @staticmethod
    def _source_sig(source_path: Path) -> str:
        """Stable import signature: resolved path + size + mtime_ns.
        Returns "" if the file can't be stat'd (dedup then skipped, not matched).
        # ponytail: path/size/mtime, not a content hash — covers the reported
        # retry/double-click of the SAME file; a moved copy still dupes (upgrade
        # path = sha256 sig). Documented ceiling per D5.
        """
        try:
            st = source_path.stat()
            return f"{source_path.resolve()}|{st.st_size}|{st.st_mtime_ns}"
        except OSError:
            return ""

    def _next_variant_index(self, mood: str) -> int:
        existing = [track.variant_index for track in self.tracks.values() if track.mood == mood]
        return max(existing, default=-1) + 1

    @staticmethod
    def _label_for(mood: str, variant_index: int) -> str:
        if variant_index <= 0:
            return mood
        if variant_index == 1:
            return f"{mood}_alt"
        return f"{mood}_alt{variant_index}"

    @staticmethod
    def _refresh_track_state(track: MusicTrack) -> MusicTrack:
        path = Path(track.path)
        track.missing = not path.exists()
        track.invalid = False if track.missing else not is_supported_audio_path(path)
        return track
