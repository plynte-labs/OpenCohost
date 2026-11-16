"""A fake Ollama HTTP server for fault injection.

Speaks the subset of Ollama's HTTP API the engine uses on the turn path:

* ``POST /api/chat``      NDJSON streaming over chunked transfer encoding, or
                          one JSON object when the request says ``stream: false``
* ``POST /api/generate``  non-streaming (the rollback's warm and unload calls)
* ``POST /api/show``      minimal model metadata (context length, capabilities)
* ``GET  /api/tags``      the configured model list
* ``GET  /api/ps``        no resident models
* ``GET  /api/version``

Each ``/api/chat`` request consumes the next queued ``ChatScript`` (FIFO; a
plain successful reply when the queue is empty). A script can misbehave on
demand:

``normal``      every chunk, then a ``done`` chunk
``slow``        every chunk, ``delay_s`` apart
``stall``       ``after`` chunks, then silence until the client closes the socket
``cut``         ``after`` chunks, then the server closes the socket mid-body
``http_500``    status 500 with an Ollama-shaped ``{"error": ...}`` body
``hang``        no bytes at all (not even headers) until the client closes
``error_line``  ``after`` chunks, then an NDJSON ``{"error": ...}`` line

Every chat request is recorded as a ``ChatCall`` with monotonic timestamps,
including whether and when the CLIENT closed the socket while the server was
holding it open (``stall`` and ``hang``). That disconnect is the observable
client half of "the runner is freed": real Ollama cancels a generation when
its client goes away, which this fake cannot prove.

Binds 127.0.0.1 on an ephemeral port, serves on daemon threads, and never
blocks longer than ``hold_s`` on any request, so a test bug cannot hang the
suite. Waits use events and ``select`` instead of sleeps where possible.
Request bodies are NOT retained beyond the model name and option keys.
"""

from __future__ import annotations

import json
import select
import socket
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

MODES = ("normal", "slow", "stall", "cut", "http_500", "hang", "error_line")
_CREATED_AT = "2026-11-12T00:00:00.000000Z"


@dataclass
class ChatScript:
    chunks: list = field(default_factory=lambda: ["Respuesta normal."])
    mode: str = "normal"
    after: int = 0
    delay_s: float = 0.0
    done_reason: str = "stop"

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError(f"unknown mode {self.mode!r}; expected one of {MODES}")


@dataclass
class ChatCall:
    script: ChatScript
    model: str
    stream: bool
    option_keys: tuple
    received_at: float
    status: Optional[int] = None
    first_byte_at: Optional[float] = None
    stalled_at: Optional[float] = None
    cut_at: Optional[float] = None
    disconnected_at: Optional[float] = None
    client_closed: bool = False
    chunks_sent: int = 0
    completed: bool = False
    finished: threading.Event = field(default_factory=threading.Event, repr=False)
    closed: threading.Event = field(default_factory=threading.Event, repr=False)

    def mark_closed(self) -> None:
        if self.disconnected_at is None:
            self.disconnected_at = time.monotonic()
        self.client_closed = True
        self.closed.set()


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, fake: "FakeOllamaServer"):
        self.fake = fake
        super().__init__(("127.0.0.1", 0), _Handler)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _Server

    def log_message(self, format, *args):  # quiet: never print to the test log
        pass

    # ------------------------------------------------------------ plumbing

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            return {}

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _write_chunk(self, payload: dict) -> None:
        data = (json.dumps(payload) + "\n").encode("utf-8")
        self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
        self.wfile.flush()

    def _end_chunks(self) -> None:
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _hold(self, call: ChatCall, seconds: float) -> bool:
        """Keep the socket open for *seconds*, watching for the client to close it.

        Returns True as soon as the client closes (EOF or reset), recording when.
        Bounded by the fake's ``hold_s`` and by ``stop()``."""
        fake = self.server.fake
        sock = self.connection
        deadline = time.monotonic() + min(float(seconds), fake.hold_s)
        while not fake._stopping.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            readable, _, _ = select.select([sock], [], [], min(remaining, 0.01))
            if not readable:
                continue
            try:
                data = sock.recv(1, socket.MSG_PEEK)
            except OSError:  # reset counts as closed
                data = b""
            if not data:
                call.mark_closed()
                return True
            sock.recv(4096)  # unexpected client bytes: discard, keep holding
        return False

    # -------------------------------------------------------------- routes

    def do_GET(self):
        fake = self.server.fake
        fake._record("GET", self.path)
        if self.path == "/api/tags":
            self._send_json(200, {"models": [_model_entry(m) for m in fake.models]})
        elif self.path == "/api/ps":
            self._send_json(200, {"models": []})
        elif self.path == "/api/version":
            self._send_json(200, {"version": "0.0.0-fake"})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        fake = self.server.fake
        fake._record("POST", self.path)
        body = self._read_json()
        if self.path == "/api/chat":
            self._chat(body)
        elif self.path == "/api/generate":
            fake._note_generate(body)
            model = body.get("model", "")
            self._send_json(
                200,
                {
                    "model": model,
                    "created_at": _CREATED_AT,
                    "response": "ok",
                    "done": True,
                    "done_reason": "stop" if body.get("prompt") else "unload",
                },
            )
        elif self.path == "/api/show":
            self._send_json(200, _show_payload())
        else:
            self._send_json(404, {"error": "not found"})

    # ---------------------------------------------------------------- chat

    def _chat(self, body: dict) -> None:
        fake = self.server.fake
        script = fake._next_script()
        call = ChatCall(
            script=script,
            model=str(body.get("model", "")),
            stream=bool(body.get("stream", True)),
            option_keys=tuple(sorted((body.get("options") or {}).keys())),
            received_at=time.monotonic(),
        )
        fake._note_chat(call)
        try:
            self._serve_chat(call, script)
        except OSError:
            # The client went away while we were writing: that is a close too.
            call.mark_closed()
            self.close_connection = True
        finally:
            call.finished.set()

    def _serve_chat(self, call: ChatCall, script: ChatScript) -> None:
        fake = self.server.fake
        if script.mode == "hang":
            self._hold(call, fake.hold_s)
            self.close_connection = True
            return
        if script.mode == "http_500":
            call.status = 500
            call.first_byte_at = time.monotonic()
            self._send_json(500, {"error": "fake internal server error"})
            return
        if not call.stream:
            call.status = 200
            call.first_byte_at = time.monotonic()
            self._send_json(200, _chunk(call.model, "".join(script.chunks), True, script.done_reason))
            call.chunks_sent = len(script.chunks)
            call.completed = True
            return

        call.status = 200
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.flush()
        call.first_byte_at = time.monotonic()
        self.close_connection = True

        limit = script.after if script.mode in ("stall", "cut", "error_line") else len(script.chunks)
        for index, text in enumerate(script.chunks[:limit]):
            if index and script.mode == "slow" and (
                self._hold(call, script.delay_s) or fake._stopping.is_set()
            ):
                return
            self._write_chunk(_chunk(call.model, text, False))
            call.chunks_sent += 1

        if script.mode == "stall":
            call.stalled_at = time.monotonic()
            self._hold(call, fake.hold_s)
            return
        if script.mode == "cut":
            call.cut_at = time.monotonic()
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return
        if script.mode == "error_line":
            self._write_chunk({"error": "fake mid-stream error"})
            self._end_chunks()
            return
        self._write_chunk(_chunk(call.model, "", True, script.done_reason))
        self._end_chunks()
        call.completed = True


def _chunk(model: str, content: str, done: bool, done_reason: str = "stop") -> dict:
    payload = {
        "model": model,
        "created_at": _CREATED_AT,
        "message": {"role": "assistant", "content": content},
        "done": done,
    }
    if done:
        payload.update(
            done_reason=done_reason,
            total_duration=1_000_000,
            load_duration=100_000,
            prompt_eval_count=10,
            prompt_eval_duration=100_000,
            eval_count=max(1, len(content.split())),
            eval_duration=500_000,
        )
    return payload


def _model_entry(name: str) -> dict:
    return {
        "name": name,
        "model": name,
        "modified_at": _CREATED_AT,
        "size": 1,
        "digest": "0" * 64,
        "details": {"format": "gguf", "family": "llama", "parameter_size": "8B", "quantization_level": "Q4_0"},
    }


def _show_payload() -> dict:
    return {
        "modelfile": "",
        "parameters": "",
        "template": "",
        "details": {"format": "gguf", "family": "llama", "parameter_size": "8B", "quantization_level": "Q4_0"},
        "model_info": {"general.architecture": "llama", "llama.context_length": 8192},
        "capabilities": ["completion"],
    }


class FakeOllamaServer:
    """Loopback fake Ollama; see the module docstring for the protocol."""

    def __init__(self, *, models=("llama3",), hold_s: float = 10.0):
        self.models = tuple(models)
        self.hold_s = float(hold_s)
        self.chat_calls: list = []
        self.generate_calls: list = []
        self.requests: list = []
        self._scripts: list = []
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._server: Optional[_Server] = None
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------ lifecycle

    def start(self) -> "FakeOllamaServer":
        self._server = _Server(self)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True,
            name="fake-ollama",
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stopping.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    # ------------------------------------------------------------ scripting

    def script_chat(self, script: ChatScript) -> None:
        with self._lock:
            self._scripts.append(script)

    def _next_script(self) -> ChatScript:
        with self._lock:
            return self._scripts.pop(0) if self._scripts else ChatScript()

    # ------------------------------------------------------------ recording

    def _record(self, method: str, path: str) -> None:
        with self._lock:
            self.requests.append((method, path))

    def _note_chat(self, call: ChatCall) -> None:
        with self._lock:
            self.chat_calls.append(call)

    def _note_generate(self, body: dict) -> None:
        with self._lock:
            self.generate_calls.append(
                {
                    "model": body.get("model"),
                    "keep_alive": body.get("keep_alive"),
                    "warm": bool(body.get("prompt")),
                    "at": time.monotonic(),
                }
            )

    def wait_finished(self, index: int, timeout: float = 5.0) -> ChatCall:
        """Block until chat call *index* exists and its handler returned."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                call = self.chat_calls[index] if index < len(self.chat_calls) else None
            if call is not None:
                if call.finished.wait(max(0.0, deadline - time.monotonic())):
                    return call
                break
            time.sleep(0.01)
        raise AssertionError(f"chat call {index} did not finish within {timeout}s")

    @property
    def paths(self) -> list:
        with self._lock:
            return [path for _method, path in self.requests]
