"""Runs the gateway app on a uvicorn server in a daemon thread, loopback only."""

import logging
import threading
import time

import uvicorn

logger = logging.getLogger("OpenCohost.gateway")

_BIND_HOST = "127.0.0.1"  # never configurable: a tunnel connects locally
_START_TIMEOUT_S = 10.0
_STOP_TIMEOUT_S = 10.0


class GatewayServer:
    def __init__(self, app, port: int):
        self._port = port
        self._server = uvicorn.Server(
            uvicorn.Config(app, host=_BIND_HOST, port=port, log_config=None, access_log=False)
        )
        self._thread = None
        self._lock = threading.Lock()

    def start(self) -> bool:
        """Start once; True when serving. A bounded wait gives up and logs."""
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._server.run, name="remote-gateway", daemon=True
                )
                self._thread.start()
            thread = self._thread
        deadline = time.monotonic() + _START_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._server.started:
                return True
            if not thread.is_alive():
                break
            time.sleep(0.05)
        logger.warning("Remote gateway failed to start on port %s", self._port)
        return False

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is None:
            return
        self._server.should_exit = True
        thread.join(timeout=_STOP_TIMEOUT_S)
        if thread.is_alive():
            logger.warning("Remote gateway did not stop within %ss", _STOP_TIMEOUT_S)
