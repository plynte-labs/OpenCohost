import logging

from core.temp_file_cleanup import register_temp_file_cleanup


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
