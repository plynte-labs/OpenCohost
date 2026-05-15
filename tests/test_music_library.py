from __future__ import annotations

from pathlib import Path

import pytest

from core.music_library import MusicLibrary, MusicTrack, is_supported_audio_path


def write_wav(path: Path) -> None:
    path.write_bytes(b"RIFF" + b"\x24\x00\x00\x00" + b"WAVEfmt " + b"\x00" * 32)


def write_mp3(path: Path) -> None:
    path.write_bytes(b"ID3" + b"\x00" * 32)


def test_audio_validation_uses_extension_and_header(tmp_path: Path):
    wav = tmp_path / "tema.wav"
    write_wav(wav)
    mp3 = tmp_path / "tema.txt.mp3"
    write_mp3(mp3)
    fake = tmp_path / "tema.mp3"
    fake.write_text("not audio", encoding="utf-8")
    disguised = tmp_path / "tema.mp3.exe"
    write_mp3(disguised)

    assert is_supported_audio_path(wav) is True
    assert is_supported_audio_path(mp3) is True
    assert is_supported_audio_path(fake) is False
    assert is_supported_audio_path(disguised) is False


def test_import_assigns_mood_variants_without_renaming_source(tmp_path: Path):
    source_one = tmp_path / "mi cancion rara.wav"
    source_two = tmp_path / "otra cosa.wav"
    write_wav(source_one)
    write_wav(source_two)
    library = MusicLibrary(library_dir=tmp_path / "library", config_file=tmp_path / "music_library.json")

    first = library.add_file(source_one, "nostalgia")
    second = library.add_file(source_two, "nostalgia")

    assert first.original_name == "mi cancion rara.wav"
    assert first.label == "nostalgia"
    assert second.label == "nostalgia_alt"
    assert library.counts_by_mood()["nostalgia"] == 2


def test_mood_selection_falls_back_to_available_music(tmp_path: Path):
    source = tmp_path / "solo nostalgia.wav"
    write_wav(source)
    library = MusicLibrary(library_dir=tmp_path / "library", config_file=tmp_path / "music_library.json")
    track = library.add_file(source, "nostalgia")

    selected = library.select_for_mood("hype")

    assert selected is not None
    assert selected.id == track.id


def test_missing_files_are_marked_and_cleanup_removes_metadata(tmp_path: Path):
    source = tmp_path / "normal.wav"
    write_wav(source)
    library = MusicLibrary(library_dir=tmp_path / "library", config_file=tmp_path / "music_library.json")
    track = library.add_file(source, "normal")
    Path(track.path).unlink()

    tracks = list(library.all_tracks())
    assert tracks[0].missing is True
    assert library.cleanup_missing() == 1
    assert list(library.all_tracks()) == []


def test_invalid_audio_is_rejected_on_import(tmp_path: Path):
    source = tmp_path / "fake.wav"
    source.write_bytes(b"nope")
    library = MusicLibrary(library_dir=tmp_path / "library", config_file=tmp_path / "music_library.json")

    with pytest.raises(ValueError):
        library.add_file(source, "normal")


def test_remove_deletes_only_app_managed_imported_file(tmp_path: Path):
    source = tmp_path / "source.wav"
    external = tmp_path / "external.wav"
    write_wav(source)
    write_wav(external)
    library = MusicLibrary(library_dir=tmp_path / "library", config_file=tmp_path / "music_library.json")
    imported = library.add_file(source, "normal")
    external_track = MusicTrack(
        id="external",
        original_name="external.wav",
        path=str(external),
        mood="normal",
        label="normal_alt",
        variant_index=1,
    )
    library.tracks[external_track.id] = external_track

    assert library.remove(imported.id, delete_file=True) is True
    assert Path(imported.path).exists() is False
    assert library.remove(external_track.id, delete_file=True) is True
    assert external.exists() is True


def test_remove_missing_managed_file_removes_metadata_without_error(tmp_path: Path):
    source = tmp_path / "missing.wav"
    write_wav(source)
    library = MusicLibrary(library_dir=tmp_path / "library", config_file=tmp_path / "music_library.json")
    track = library.add_file(source, "normal")
    Path(track.path).unlink()

    assert library.remove(track.id, delete_file=True) is True
    assert list(library.all_tracks()) == []
