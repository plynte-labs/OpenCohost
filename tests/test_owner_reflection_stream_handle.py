"""Runner-release spike (owner-reflection-memory-20261015, ADR-065 section 5).

ADR-065 cancels a run by closing the HTTP response from the arriving thread. These tests
prove the mechanism against a REAL local HTTP server (stdlib), standing in for Ollama:

* closing the handle from another thread aborts a read blocked mid-stream, and the server
  sees the socket close;
* the same holds while the request is still blocked before any response header, which is
  what Ollama's prefill looks like to the client.

They prove the client side only. Whether a real Ollama then frees its single runner on the
socket close cannot be shown here; it is the live T5 check (streaming gate).
"""
from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import opencohost.core.llm_engine as llm_engine
from opencohost.config import settings
from tests.owner_reflection_support import feed, make_motor, run_job, spy_capture


class _Server:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.request_seen = threading.Event()
        self.client_closed = threading.Event()
        self.payloads: list = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # silence
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                outer.payloads.append(json.loads(self.rfile.read(length) or b"{}"))
                outer.request_seen.set()
                if outer.mode == "reply":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    reply = json.dumps({"facts": [{"q": "What does the owner grow?",
                                                   "a": "The owner grows tomatoes.", "turns": [1]}]})
                    for text, done in ((reply[:20], False), (reply[20:], False), ("", True)):
                        line = json.dumps({"message": {"content": text}, "done": done}).encode() + b"\n"
                        self.wfile.write(f"{len(line):x}\r\n".encode() + line + b"\r\n")
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                    return
                if outer.mode == "stream":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    line = json.dumps({"message": {"content": "x"}, "done": False}).encode() + b"\n"
                    self.wfile.write(f"{len(line):x}\r\n".encode() + line + b"\r\n")
                    self.wfile.flush()
                # mode == "prefill": no header at all, the request just blocks.
                self.connection.settimeout(0.05)
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    try:
                        if self.connection.recv(1, socket.MSG_PEEK) == b"":
                            outer.client_closed.set()
                            return
                    except socket.timeout:
                        continue
                    except OSError:
                        outer.client_closed.set()
                        return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server_factory(monkeypatch):
    servers = []

    def make(mode):
        srv = _Server(mode)
        servers.append(srv)
        monkeypatch.setenv("OLLAMA_HOST", f"127.0.0.1:{srv.port}")
        return srv

    yield make
    for srv in servers:
        srv.stop()


def _open(monkeypatch, tmp_path):
    motor = make_motor(monkeypatch, tmp_path)
    handle = motor._owner_reflection_open_stream(
        model="m", messages=[{"role": "user", "content": "hi"}], options={}, timeout_s=20.0,
    )
    return handle


def _consume(handle, sink):
    try:
        for line in handle.iter_lines():
            sink.append(line)
    except Exception as exc:  # the close we cause surfaces as a read error
        sink.append(exc)


def test_close_from_another_thread_aborts_a_read_blocked_mid_stream(monkeypatch, tmp_path, server_factory):
    srv = server_factory("stream")
    handle = _open(monkeypatch, tmp_path)
    sink: list = []
    reader = threading.Thread(target=_consume, args=(handle, sink), daemon=True)
    reader.start()
    deadline = time.monotonic() + 3
    while not sink and time.monotonic() < deadline:
        time.sleep(0.01)
    assert sink, "the first line must arrive before the close"
    t0 = time.perf_counter()
    handle.close()  # from THIS thread, not the reader's
    assert (time.perf_counter() - t0) < 0.5
    reader.join(2)
    assert not reader.is_alive(), "the blocked read must be released by the close"
    assert srv.client_closed.wait(2), "the server must observe the socket close"


def test_close_from_another_thread_aborts_a_request_blocked_before_headers(monkeypatch, tmp_path, server_factory):
    srv = server_factory("prefill")
    handle = _open(monkeypatch, tmp_path)
    sink: list = []
    reader = threading.Thread(target=_consume, args=(handle, sink), daemon=True)
    reader.start()
    assert srv.request_seen.wait(3), "the request must reach the server"
    t0 = time.perf_counter()
    handle.close()
    assert (time.perf_counter() - t0) < 0.5
    reader.join(2)
    assert not reader.is_alive(), "a send blocked on prefill must be released by the close"
    assert srv.client_closed.wait(2), "the server must observe the socket close"


def test_handle_uses_the_job_budget_as_the_http_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "OWNER_REFLECTION_JOB_TIMEOUT_S", 20.0)
    motor = make_motor(monkeypatch, tmp_path)
    handle = motor._owner_reflection_open_stream(model="m", messages=[], options={}, timeout_s=7.5)
    try:
        assert handle._client.timeout.read == 7.5 and handle._client.timeout.connect == 7.5
    finally:
        handle.close()
    assert llm_engine is not None


def test_real_handle_end_to_end_against_a_local_server(monkeypatch, tmp_path, server_factory):
    """Wire format: the job posts a streaming JSON-mode chat and parses the NDJSON reply."""
    srv = server_factory("reply")
    motor = make_motor(monkeypatch, tmp_path)
    feed(motor, 8)
    calls = spy_capture(monkeypatch, motor)
    run_job(motor, timeout=10.0)
    assert len(calls) == 1 and calls[0][0][2] == "What does the owner grow?"
    (payload,) = srv.payloads
    assert payload["model"] == "m" and payload["stream"] is True and payload["format"] == "json"
    assert payload["options"]["num_predict"] == 256 and payload["options"]["temperature"] == 0
    (message,) = payload["messages"]
    assert message["role"] == "user" and "owner statement number 1" in message["content"]
