"""Dedicated worker process entrypoint for search API fallback (Phase 3, A5/A6).

Runs as an isolated child process via:
    python -m opencohost.core.web.search_api_worker

Communicates with SearchApiRuntime over stdin/stdout via bounded newline-delimited JSON frames:
    - Request frames <= 8 KiB
    - Response frames <= 64 KiB

Executes the two-phase handshake:
    1. PREPARE: validates provider, resolves DNS via default_resolver, checks public IP via _public.
       NO HTTP request is made during PREPARE.
    2. SEND: calls tavily_search or brave_search with pinned host/IP, returns SearchResults.
    3. QUIT / EOF: exits cleanly.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any, BinaryIO, Callable, Iterable, Optional

import httpx

from opencohost.core.web.fetch import _public, default_resolver
from opencohost.core.web.search import SearchResult
from opencohost.core.web.search_api import (
    BRAVE_HOST,
    REASON_AUTH,
    REASON_BLOCKED,
    REASON_DNS,
    REASON_OTHER,
    REASON_PARSE,
    TAVILY_HOST,
    brave_search,
    tavily_search,
)

MAX_REQUEST_FRAME_BYTES = 8192      # 8 KiB
MAX_RESPONSE_FRAME_BYTES = 65536    # 64 KiB

_SILENCED_LOGGERS = ("httpx", "httpcore")


def _silence_transport_loggers() -> None:
    for name in _SILENCED_LOGGERS:
        log = logging.getLogger(name)
        log.setLevel(logging.WARNING)


_silence_transport_loggers()


class FrameError(Exception):
    """Raised on invalid frame formatting or EOF."""


class FrameOverflowError(FrameError):
    """Raised when a frame exceeds maximum allowed byte limit."""


def read_frame(stream: BinaryIO, max_bytes: int = MAX_REQUEST_FRAME_BYTES) -> dict:
    """Read a single newline-delimited JSON frame with strict byte bounding."""
    line = stream.readline(max_bytes + 2)
    if not line:
        raise EOFError("Stream closed")
    if len(line) > max_bytes or not line.endswith(b"\n"):
        raise FrameOverflowError(f"Frame exceeds maximum byte limit of {max_bytes}")
    stripped = line.rstrip(b"\r\n")
    if len(stripped) > max_bytes:
        raise FrameOverflowError(f"Frame exceeds maximum byte limit of {max_bytes}")
    try:
        data = json.loads(stripped.decode("utf-8"))
    except Exception as exc:
        raise FrameError(f"Malformed JSON frame: {exc}") from exc
    if not isinstance(data, dict):
        raise FrameError("Frame payload must be a JSON object")
    return data


def write_frame(stream: BinaryIO, frame: dict, max_bytes: int = MAX_RESPONSE_FRAME_BYTES) -> None:
    """Write a single newline-delimited JSON frame with strict byte bounding."""
    data = json.dumps(frame, ensure_ascii=False).encode("utf-8")
    if len(data) > max_bytes:
        raise FrameOverflowError(f"Response frame size {len(data)} exceeds maximum {max_bytes}")
    stream.write(data + b"\n")
    stream.flush()


# Worker-level prepared state for the single active slot
_PREPARED_STATE: Optional[dict[str, Any]] = None


def handle_prepare(
    frame: dict,
    resolver: Optional[Callable[[str], Iterable[str]]] = None,
) -> dict:
    """Execute PREPARE phase: validate provider, DNS resolution, and SSRF public IP check.

    NO HTTP request is made here.
    """
    global _PREPARED_STATE
    req_id = frame.get("request_id", "")
    gen = frame.get("generation", 1)
    provider = frame.get("provider", "")
    query = frame.get("query", "")
    key = frame.get("key", "")

    # Test hooks
    if os.environ.get("OPENCOHOST_WORKER_CRASH_PREPARE") == "1":
        sys.exit(42)

    if os.environ.get("OPENCOHOST_WORKER_HANG_PREPARE"):
        time.sleep(float(os.environ["OPENCOHOST_WORKER_HANG_PREPARE"]))

    if provider not in ("tavily", "brave"):
        return {
            "cmd": "READY",
            "request_id": req_id,
            "generation": gen,
            "ok": False,
            "reason": REASON_PARSE,
        }

    if not key or not str(key).strip():
        return {
            "cmd": "READY",
            "request_id": req_id,
            "generation": gen,
            "ok": False,
            "reason": REASON_AUTH,
        }

    host = TAVILY_HOST if provider == "tavily" else BRAVE_HOST

    # DNS resolution
    mock_dns = os.environ.get("OPENCOHOST_WORKER_MOCK_DNS")
    if mock_dns == "gaierror":
        return {
            "cmd": "READY",
            "request_id": req_id,
            "generation": gen,
            "ok": False,
            "reason": REASON_DNS,
        }
    elif mock_dns:
        ips = [mock_dns]
    else:
        res = resolver or default_resolver
        try:
            ips = [str(ip) for ip in res(host)]
        except Exception:
            return {
                "cmd": "READY",
                "request_id": req_id,
                "generation": gen,
                "ok": False,
                "reason": REASON_DNS,
            }

    if not ips:
        return {
            "cmd": "READY",
            "request_id": req_id,
            "generation": gen,
            "ok": False,
            "reason": REASON_DNS,
        }

    # Public IP SSRF guard
    if not all(_public(ip) for ip in ips):
        return {
            "cmd": "READY",
            "request_id": req_id,
            "generation": gen,
            "ok": False,
            "reason": REASON_BLOCKED,
        }

    pinned_ip = ips[0]
    _PREPARED_STATE = {
        "request_id": req_id,
        "provider": provider,
        "query": query,
        "key": key,
        "pinned_ip": pinned_ip,
    }

    resp_gen = gen
    if os.environ.get("OPENCOHOST_WORKER_SEND_OLD_GENERATION") == "1":
        resp_gen = gen - 1

    return {
        "cmd": "READY",
        "request_id": req_id,
        "generation": resp_gen,
        "ok": True,
        "reason": "",
    }


def handle_send(
    frame: dict,
    transport: Optional[httpx.BaseTransport] = None,
) -> dict:
    """Execute SEND phase: call provider search client with pinned IP."""
    global _PREPARED_STATE
    req_id = frame.get("request_id", "")
    gen = frame.get("generation", 1)
    provider = frame.get("provider", "")
    query = frame.get("query", "")
    key = frame.get("key", "")
    deadline = frame.get("deadline")

    # Test hooks
    if os.environ.get("OPENCOHOST_WORKER_FAIL_ON_HTTP") == "1":
        sys.exit(99)

    if os.environ.get("OPENCOHOST_WORKER_CRASH_SEND") == "1":
        sys.exit(42)

    if os.environ.get("OPENCOHOST_WORKER_HANG_SEND"):
        time.sleep(float(os.environ["OPENCOHOST_WORKER_HANG_SEND"]))

    pinned_ip = None
    if _PREPARED_STATE and _PREPARED_STATE.get("request_id") == req_id:
        pinned_ip = _PREPARED_STATE.get("pinned_ip")

    pinned_resolver = (lambda h: [pinned_ip]) if pinned_ip else default_resolver

    mock_http = os.environ.get("OPENCOHOST_WORKER_MOCK_HTTP")
    if transport is None and mock_http:
        mock_data = json.loads(mock_http)
        transport = httpx.MockTransport(lambda req: httpx.Response(200, json=mock_data))

    try:
        if provider == "tavily":
            results, reason = tavily_search(
                query,
                key,
                deadline=deadline,
                resolver=pinned_resolver,
                transport=transport,
            )
        elif provider == "brave":
            results, reason = brave_search(
                query,
                key,
                deadline=deadline,
                resolver=pinned_resolver,
                transport=transport,
            )
        else:
            results, reason = [], REASON_PARSE
    except Exception:
        results, reason = [], REASON_OTHER

    raw_results = [
        {"title": r.title, "url": r.url, "snippet": r.snippet}
        for r in results
    ]

    resp_gen = gen
    if os.environ.get("OPENCOHOST_WORKER_SEND_OLD_GENERATION") == "1":
        resp_gen = gen - 1

    return {
        "cmd": "RESULT",
        "request_id": req_id,
        "generation": resp_gen,
        "ok": not reason,
        "results": raw_results,
        "reason": reason,
    }


def run_worker(stdin: BinaryIO, stdout: BinaryIO) -> None:
    """Main worker event loop reading and dispatching IPC frames."""
    _silence_transport_loggers()
    while True:
        try:
            frame = read_frame(stdin, MAX_REQUEST_FRAME_BYTES)
        except EOFError:
            break
        except FrameOverflowError:
            break
        except Exception:
            break

        cmd = frame.get("cmd")
        if cmd == "QUIT":
            break
        elif cmd == "PREPARE":
            if os.environ.get("OPENCOHOST_WORKER_OVERFLOW_RESPONSE") == "1":
                stdout.write(b"x" * (MAX_RESPONSE_FRAME_BYTES + 100) + b"\n")
                stdout.flush()
                continue
            resp = handle_prepare(frame)
            write_frame(stdout, resp, MAX_RESPONSE_FRAME_BYTES)
        elif cmd == "SEND":
            if os.environ.get("OPENCOHOST_WORKER_OVERFLOW_RESPONSE") == "1":
                stdout.write(b"x" * (MAX_RESPONSE_FRAME_BYTES + 100) + b"\n")
                stdout.flush()
                continue
            resp = handle_send(frame)
            write_frame(stdout, resp, MAX_RESPONSE_FRAME_BYTES)
        else:
            resp = {
                "cmd": "ERROR",
                "request_id": frame.get("request_id", ""),
                "generation": frame.get("generation", 1),
                "ok": False,
                "reason": REASON_PARSE,
            }
            write_frame(stdout, resp, MAX_RESPONSE_FRAME_BYTES)


def main() -> None:
    """Dedicated entrypoint for search API worker process."""
    run_worker(sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":
    main()
