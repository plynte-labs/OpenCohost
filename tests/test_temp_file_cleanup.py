import logging

from opencohost.core.temp_file_cleanup import cleanup_opencohost_temp_artifacts, register_temp_file_cleanup


class FakeResponse:
    def __init__(self):
        self.callbacks = []

    def call_on_close(self, callback):
        self.callbacks.append(callback)


def test_temp_file_cleanup_removes_file_after_response_close(tmp_path):
    generated = tmp_path / "out_pesado_test.wav"
    generated.write_bytes(b"RIFF")
    response = FakeResponse()

    returned = register_temp_file_cleanup(response, str(generated), logging.getLogger("test"))

    assert returned is response
    assert generated.exists()
    assert len(response.callbacks) == 1

    response.callbacks[0]()

    assert not generated.exists()


def test_temp_file_cleanup_swallows_and_logs_remove_failure(tmp_path, caplog):
    generated = tmp_path / "out_pesado_locked.wav"
    generated.write_bytes(b"RIFF")
    response = FakeResponse()
    logger = logging.getLogger("test-temp-cleanup")

    def fail_remove(path):
        raise OSError("locked")

    register_temp_file_cleanup(response, str(generated), logger, remove=fail_remove)

    with caplog.at_level(logging.WARNING, logger="test-temp-cleanup"):
        response.callbacks[0]()

    assert generated.exists()
    assert any("Could not remove generated temp audio file" in record.message for record in caplog.records)


def test_janitor_removes_only_opencohost_temp_artifacts(tmp_path):
    owned = tmp_path / "tts_chunk_0_abcd.wav"
    server_owned = tmp_path / "out_pesado_abcd1234.wav"
    user_file = tmp_path / "user_reference.wav"
    asset_file = tmp_path / "avatar_idle.png"
    for path in (owned, server_owned, user_file, asset_file):
        path.write_bytes(b"data")

    stats = cleanup_opencohost_temp_artifacts(tmp_path, logging.getLogger("test"), min_age_seconds=0.0)

    assert stats["removed"] == 2
    assert not owned.exists()
    assert not server_owned.exists()
    assert user_file.exists()
    assert asset_file.exists()


def test_janitor_is_idempotent(tmp_path):
    owned = tmp_path / "tts_chunk_0_abcd.mp3"
    owned.write_bytes(b"data")

    first = cleanup_opencohost_temp_artifacts(tmp_path, logging.getLogger("test"), min_age_seconds=0.0)
    second = cleanup_opencohost_temp_artifacts(tmp_path, logging.getLogger("test"), min_age_seconds=0.0)

    assert first["removed"] == 1
    assert second["removed"] == 0
