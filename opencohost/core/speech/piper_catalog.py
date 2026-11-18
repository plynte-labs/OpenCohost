"""Piper voice catalog: the bundled voices plus the ``.onnx`` files in one owner-chosen folder.

A voice is a model file ``X`` (name ends with ``.onnx``, so ``foo.onnx.onnx`` is valid) paired
with ``X + ".json"`` (Piper's own rule). Only the json is read, and only up to a size cap; the
model bytes are never opened. The scan is NON-recursive and runs on every call, so a folder
change or a freshly copied voice shows up with no restart.

The folder choice is persisted in ``piper_voice_dir.json`` (``settings.PIPER_VOICE_DIR_FILE``).
Resolution: saved folder -> env ``OPENCOHOST_PIPER_VOICE_DIR`` -> the Piper cache folder the
bundled voices already use, so nothing changes for existing users until they pick a folder.
Folder paths are never logged (they can contain the Windows user name).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from opencohost.avatar.vrm_folder import VrmFolderError as PiperFolderError
from opencohost.avatar.vrm_folder import validate_folder
from opencohost.config import settings
from opencohost.config.storage import atomic_write_text

SAVED = "saved"
ENV = "env"
DEFAULT = "default"
ENV_VAR = "OPENCOHOST_PIPER_VOICE_DIR"

STATUS_OK = "ok"
STATUS_INCOMPLETE = "incomplete"
REASON_MISSING_CONFIG = "missing_config"
REASON_INVALID_CONFIG = "invalid_config"

_MODEL_SUFFIX = ".onnx"
_CONFIG_MAX_BYTES = 1024 * 1024
# Hard cap on directory entries examined per scan (a folder full of unrelated files must not stall
# the API). Entries are sorted by name BEFORE the cap, so the kept subset is deterministic.
_MAX_SCAN_ENTRIES = 2000
_MAX_GENERATED_SPEAKERS = 256


class PiperVoiceError(ValueError):
    """A voice id was refused; ``code`` is a stable enum for the front end."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PiperVoice:
    id: str
    label: str
    lang: str | None
    num_speakers: int
    speakers: tuple  # ((name, speaker_id), ...) sorted by speaker id
    sample_rate: int | None
    bundled: bool
    status: str
    reason: str | None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "lang": self.lang,
            "num_speakers": self.num_speakers,
            "speakers": [{"name": n, "id": i} for n, i in self.speakers],
            "sample_rate": self.sample_rate,
            "bundled": self.bundled,
            "status": self.status,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Folder resolution and persistence
# ---------------------------------------------------------------------------


def _default_dir() -> Path:
    return Path(str(settings.STORAGE_PATHS.cache_root)) / "piper"


def _read_saved() -> str:
    try:
        with open(settings.PIPER_VOICE_DIR_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return str(data.get("voice_dir", "") or "").strip() if isinstance(data, dict) else ""
    except (OSError, ValueError):
        return ""


def resolve_voice_dir() -> tuple[Path, str]:
    """The single place that decides the folder, as ``(path, source)``."""
    saved = _read_saved()
    if saved:
        candidate = Path(saved)
        try:
            if candidate.is_absolute() and candidate.is_dir():
                return candidate, SAVED
        except OSError:
            pass
    env = os.environ.get(ENV_VAR, "").strip()
    if env:
        return Path(env), ENV
    return _default_dir(), DEFAULT


def save_voice_dir(raw: str | Path) -> Path:
    """Validate (same rules as the VRM folder) and persist the folder. Raises PiperFolderError/OSError."""
    chosen = validate_folder(str(raw))
    payload = json.dumps({"voice_dir": str(chosen), "saved_at": datetime.now().isoformat()})
    atomic_write_text(Path(settings.PIPER_VOICE_DIR_FILE), payload)
    return chosen


def clear_voice_dir() -> None:
    """Forget the saved folder (idempotent). Raises OSError when the file cannot be removed."""
    try:
        os.remove(settings.PIPER_VOICE_DIR_FILE)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Voice ids and scanning
# ---------------------------------------------------------------------------


def _valid_id(name: str) -> bool:
    return bool(name) and not any(c in name for c in "/\\:\x00") and name.lower().endswith(_MODEL_SUFFIX)


def normalize_lang(code) -> str | None:
    """``"es_ES"`` / ``"en-us"`` -> ``"es"`` / ``"en"``; anything unusable -> None."""
    if not isinstance(code, str):
        return None
    primary = code.replace("_", "-").split("-", 1)[0].strip().lower()
    return primary or None


def _label_for(voice_id: str) -> str:
    stem = voice_id
    while stem.lower().endswith(_MODEL_SUFFIX):
        stem = stem[: -len(_MODEL_SUFFIX)]
    return stem or voice_id


def _int_or(value, default):
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _parse_config(config_path: Path):
    """Return ``(lang, num_speakers, speakers, sample_rate)`` or None when unreadable."""
    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            raw = fh.read(_CONFIG_MAX_BYTES + 1)  # hard cap: never read more than the limit + 1
        if len(raw) > _CONFIG_MAX_BYTES:
            return None
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    language = data.get("language")
    lang = None
    if isinstance(language, dict):
        lang = normalize_lang(language.get("code")) or normalize_lang(language.get("family"))
    num_speakers = max(1, _int_or(data.get("num_speakers"), 1))
    raw_map = data.get("speaker_id_map")
    pairs = []
    if isinstance(raw_map, dict):
        pairs = [(str(k), v) for k, v in raw_map.items() if isinstance(v, int) and not isinstance(v, bool)]
    if not pairs and num_speakers > 1:
        pairs = [(str(i), i) for i in range(min(num_speakers, _MAX_GENERATED_SPEAKERS))]
    speakers = tuple(sorted(pairs, key=lambda p: p[1]))
    audio = data.get("audio")
    sample_rate = _int_or(audio.get("sample_rate"), None) if isinstance(audio, dict) else None
    return lang, num_speakers, speakers, sample_rate


def _build(
    voice_id: str,
    model_path: Path,
    *,
    label: str | None = None,
    lang_hint: str | None = None,
    bundled=False,
    config_path: Path | None = None,
):
    """``config_path`` is the already-confined sidecar; ``None`` means it is absent or escaped."""
    label = label or _label_for(voice_id)
    if config_path is None:
        return PiperVoice(voice_id, label, lang_hint, 1, (), None, bundled, STATUS_INCOMPLETE, REASON_MISSING_CONFIG)
    parsed = _parse_config(config_path)
    if parsed is None:
        return PiperVoice(voice_id, label, lang_hint, 1, (), None, bundled, STATUS_INCOMPLETE, REASON_INVALID_CONFIG)
    lang, num_speakers, speakers, sample_rate = parsed
    return PiperVoice(voice_id, label, lang_hint or lang, num_speakers, speakers, sample_rate, bundled, STATUS_OK, None)


def _confined(root: Path, name: str) -> Path | None:
    """The resolved file ``root/name`` when it is a regular file directly inside ``root``."""
    try:
        path = (root / name).resolve(strict=True)
        return path if path.parent == root and path.is_file() else None
    except (OSError, RuntimeError, ValueError):
        return None


def scan_folder(folder: Path) -> list[PiperVoice]:
    """Voices directly inside ``folder`` (non-recursive), sorted by id. Empty when unreadable.

    Only the first ``_MAX_SCAN_ENTRIES`` directory entries (sorted by name) are examined.
    """
    root, names = _model_names(folder)
    voices = []
    for name in names:
        path = _confined(root, name)
        if path is not None:
            voices.append(_build(name, path, config_path=_confined(root, name + ".json")))
    return voices


def _model_names(folder: Path) -> tuple[Path | None, list[str]]:
    """``(resolved root, sorted model names)`` for the first ``_MAX_SCAN_ENTRIES`` entries by name."""
    try:
        root = Path(folder).resolve(strict=True)
        with os.scandir(root) as it:
            entries = sorted(e.name for e in it)[:_MAX_SCAN_ENTRIES]
    except (OSError, RuntimeError, ValueError):
        return None, []
    return root, [n for n in entries if _valid_id(n)]


def count_voices(folder: Path) -> int:
    """Count valid ``X.onnx`` + ``X.onnx.json`` pairs without opening or parsing any file."""
    root, names = _model_names(folder)
    return sum(1 for n in names if _confined(root, n) is not None and _confined(root, n + ".json") is not None)


def _bundled_entries() -> list[tuple[dict, Path]]:
    """Registry entries (``settings.PIPER_VOICES``) whose file resolves on disk."""
    found = []
    for entry in settings.PIPER_VOICES.values():
        try:
            path = Path(settings._resolve_piper_voice_file(entry["file"]))
            if path.is_file():
                found.append((entry, path))
        except (OSError, KeyError, ValueError):
            continue
    return found


def bundled_voices() -> list[PiperVoice]:
    voices = []
    for entry, path in _bundled_entries():
        sidecar = path.with_name(path.name + ".json")
        voices.append(
            _build(
                path.name,
                path,
                label=entry.get("label"),
                lang_hint=entry.get("lang"),
                bundled=True,
                config_path=sidecar if sidecar.is_file() else None,
            )
        )
    return voices


def list_voices(lang: str | None = None) -> list[PiperVoice]:
    """Bundled voices first, then the folder's; a folder voice with a bundled id loses.

    ``lang`` keeps voices of that language family plus voices whose language is unknown
    (an incomplete voice cannot be proven to be in another language).
    """
    voices = bundled_voices()
    seen = {v.id for v in voices}
    for voice in scan_folder(resolve_voice_dir()[0]):
        if voice.id not in seen:
            voices.append(voice)
    wanted = normalize_lang(lang) if lang else None
    if wanted:
        voices = [v for v in voices if v.lang is None or v.lang == wanted]
    return voices


def voice_path(voice_id: str) -> Path:
    """Confined path of a catalog voice. Raises :class:`PiperVoiceError` (``invalid_voice_id`` / ``voice_not_found``)."""
    if not _valid_id(voice_id):
        raise PiperVoiceError("invalid_voice_id")
    for _entry, path in _bundled_entries():
        if path.name == voice_id:
            return path
    try:
        root = resolve_voice_dir()[0].resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise PiperVoiceError("voice_not_found") from None
    path = _confined(root, voice_id)
    if path is None:
        raise PiperVoiceError("voice_not_found")
    return path


def resolve_voice(voice: str) -> tuple[str, str | None]:
    """``(model path, language family or None)`` for a registry key OR a catalog voice id.

    A registry key (``"neutral"``/``"english"``, legacy) resolves through ``settings``; anything else
    goes through the confined catalog lookup. Raises :class:`PiperVoiceError` for an id that is
    unsafe or absent. Never raises for a registry key (the file may be missing; loading reports it).
    """
    entry = settings.PIPER_VOICES.get(voice) if isinstance(voice, str) else None
    if entry is not None:
        return settings.piper_voice_path(voice), normalize_lang(entry.get("lang"))
    path = voice_path(voice)
    sidecar = _confined(path.parent, path.name + ".json")
    parsed = _parse_config(sidecar) if sidecar is not None else None
    return str(path), (parsed[0] if parsed else None)
