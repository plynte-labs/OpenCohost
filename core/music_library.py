"""Mood-tagged local music library for Kira production beds."""

from __future__ import annotations

import json
import re
import shutil
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

from config.settings import MUSIC_DIR, MUSIC_CONFIG_FILE


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
        except (OSError, json.JSONDecodeError):
            return
        for raw in data.get("tracks", []):
            try:
                track = MusicTrack(**raw)
            except TypeError:
                continue
            self.tracks[track.id] = self._refresh_track_state(track)

    def save(self) -> None:
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"tracks": [asdict(track) for track in self.tracks.values()]}
        self.config_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def add_file(self, source: str | Path, mood: str) -> MusicTrack:
        source_path = Path(source)
        if not is_supported_audio_path(source_path):
            raise ValueError("Solo se aceptan archivos .mp3 o .wav válidos.")
        mood_key = normalize_mood(mood)
        variant_index = self._next_variant_index(mood_key)
        label = self._label_for(mood_key, variant_index)
        self.library_dir.mkdir(parents=True, exist_ok=True)
        safe_suffix = source_path.suffix.lower()
        target = self.library_dir / f"{label}_{uuid.uuid4().hex[:8]}{safe_suffix}"
        shutil.copy2(source_path, target)
        track = MusicTrack(
            id=uuid.uuid4().hex,
            original_name=source_path.name,
            path=str(target),
            mood=mood_key,
            label=label,
            variant_index=variant_index,
        )
        self.tracks[track.id] = track
        self.save()
        return track

    def remove(self, track_id: str, *, delete_file: bool = False) -> bool:
        track = self.tracks.pop(track_id, None)
        if not track:
            return False
        if delete_file:
            self._delete_managed_file(track)
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
        try:
            track_path.unlink(missing_ok=True)
        except OSError:
            pass

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
