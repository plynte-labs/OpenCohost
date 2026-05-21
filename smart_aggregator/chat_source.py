import random
import re
import socket
import threading
import time
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, TypedDict

try:
    import pytchat
    _PYTCHAT_AVAILABLE = True
except ImportError:
    _PYTCHAT_AVAILABLE = False


class NormalizedChatMessage(TypedDict, total=False):
    """Normalized chat message emitted by every ChatSource implementation.

    Required for on_message callbacks. Platform-agnostic shape.
    """

    platform: str
    source_id: str
    user: str
    text: str
    timestamp: float


class ChatSource(ABC):
    """Abstract base class for platform-specific chat sources.

    Subclasses MUST implement connect(), disconnect(), is_connected(),
    and platform property. Concrete __init__ handles shared state
    (lock, running flag, disconnect-notified flag, config, callbacks).
    """

    def __init__(self, config: dict, callbacks: dict):
        self.config = config
        self.callbacks = callbacks
        self._source_id: Optional[str] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._disconnect_notified = False

    @abstractmethod
    def connect(self, source_id: str):
        """Start the chat source connection for the given source_id."""
        ...

    @abstractmethod
    def disconnect(self):
        """Stop the chat source connection and clean up resources."""
        ...

    @abstractmethod
    def is_connected(self) -> bool:
        """Return True if the source is currently connected and receiving."""
        ...

    @property
    @abstractmethod
    def platform(self) -> str:
        """Return the platform identifier (e.g. 'youtube', 'twitch')."""
        ...


class YouTubeChatSource(ChatSource):
    def __init__(self, config: dict, callbacks: dict):
        super().__init__(config, callbacks)
        self._chat = None
        self._reconnect_delay = config.get("reconnect_delay_seconds", 5)
        self._max_retries = config.get("max_retries", 3)
        self._interruptable = config.get("interruptable", False)

    @property
    def platform(self) -> str:
        return "youtube"

    def connect(self, source_id: str):
        if not source_id:
            raise ValueError("video_id es requerido")
        
        if not _PYTCHAT_AVAILABLE:
            raise RuntimeError(
                "pytchat no esta instalado en flux_env. Consulta antes de instalar "
                "dependencias, segun docs/AGENT_RF3_INSTRUCTIONS.md."
            )

        should_disconnect = False
        with self._lock:
            if self._running:
                should_disconnect = True

        if should_disconnect:
            self.disconnect()

        with self._lock:
            self._source_id = source_id
            self._running = True
            self._disconnect_notified = False
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
    
    def disconnect(self):
        with self._lock:
            self._running = False
            self._chat = None

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
        self._notify_disconnect()
    
    def is_connected(self) -> bool:
        with self._lock:
            return self._running and self._chat is not None
    
    def _run(self):
        retries = 0
        while self._running and retries < self._max_retries:
            try:
                self._chat = pytchat.create(
                    video_id=self._source_id,
                    interruptable=self._interruptable
                )
                if self.callbacks.get("on_connect"):
                    try:
                        self.callbacks["on_connect"]({
                            "platform": "youtube",
                            "source_id": self._source_id,
                            "video_id": self._source_id,  # backward compat
                        })
                    except Exception:
                        pass
                
                while self._running and self._chat.is_alive():
                    for c in self._chat.get().sync_items():
                        if not self._running:
                            break
                        msg = {
                            "platform": "youtube",
                            "source_id": self._source_id,
                            "user": c.author.name,
                            "text": c.message,
                            "timestamp": time.time(),
                        }
                        if self.callbacks.get("on_message"):
                            try:
                                self.callbacks["on_message"](msg)
                            except Exception:
                                pass
                    time.sleep(0.5)
                
                if self._running:
                    retries += 1
                    time.sleep(self._reconnect_delay)
            except Exception as e:
                retries += 1
                if self.callbacks.get("on_error"):
                    try:
                        self.callbacks["on_error"](str(e))
                    except Exception:
                        pass
                if self._running and retries < self._max_retries:
                    time.sleep(self._reconnect_delay)
        
        with self._lock:
            self._running = False
            self._chat = None

        self._notify_disconnect()

    def _notify_disconnect(self):
        with self._lock:
            if self._disconnect_notified:
                return
            self._disconnect_notified = True

        if self.callbacks.get("on_disconnect"):
            try:
                self.callbacks["on_disconnect"]()
            except Exception:
                pass


class TwitchChatSource(ChatSource):
    """Anonymous IRC-based Twitch chat source (REQ-4..7).

    Connects to irc.chat.twitch.tv:6667 without authentication,
    joins a channel, parses PRIVMSG lines, and responds to PING/PONG.
    """

    def __init__(self, config: dict, callbacks: dict):
        super().__init__(config, callbacks)
        self._socket: Optional[socket.socket] = None
        self._reconnect_delay = config.get("reconnect_delay_seconds", 10)
        self._max_retries = config.get("max_retries", 3)

    @property
    def platform(self) -> str:
        return "twitch"

    def connect(self, source_id: str):
        if not source_id:
            raise ValueError("channel name es requerido")

        should_disconnect = False
        with self._lock:
            if self._running:
                should_disconnect = True

        if should_disconnect:
            self.disconnect()

        with self._lock:
            self._source_id = source_id
            self._running = True
            self._disconnect_notified = False
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def disconnect(self):
        with self._lock:
            self._running = False

        sock = None
        with self._lock:
            sock = self._socket
            self._socket = None

        if sock:
            try:
                sock.close()
            except Exception:
                pass

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
        self._notify_disconnect()

    def is_connected(self) -> bool:
        with self._lock:
            return self._running and self._socket is not None

    def _run(self):
        retries = 0
        while self._running and retries < self._max_retries:
            try:
                nick = f"justinfan{random.randint(10000, 99999)}"
                sock = socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=30)
                sock.sendall(f"NICK {nick}\r\n".encode())
                sock.sendall(f"USER {nick} 8 * :{nick}\r\n".encode())
                sock.sendall(f"JOIN #{self._source_id}\r\n".encode())

                with self._lock:
                    self._socket = sock

                if self.callbacks.get("on_connect"):
                    try:
                        self.callbacks["on_connect"]({
                            "platform": "twitch",
                            "source_id": self._source_id,
                        })
                    except Exception:
                        pass

                buffer = ""
                while self._running:
                    try:
                        data = sock.recv(4096)
                    except (ConnectionError, OSError, socket.timeout):
                        break

                    if not data:
                        break

                    try:
                        decoded = data.decode("utf-8", errors="replace")
                    except Exception:
                        continue

                    buffer += decoded
                    while "\r\n" in buffer:
                        line, buffer = buffer.split("\r\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        if line.startswith("PING"):
                            try:
                                sock.sendall(b"PONG :tmi.twitch.tv\r\n")
                            except Exception:
                                break
                        elif "PRIVMSG" in line:
                            match = re.match(r":(\w+)!.*PRIVMSG #\w+ :(.*)", line)
                            if match:
                                msg = {
                                    "platform": "twitch",
                                    "source_id": self._source_id,
                                    "user": match.group(1),
                                    "text": match.group(2),
                                    "timestamp": time.time(),
                                }
                                if self.callbacks.get("on_message"):
                                    try:
                                        self.callbacks["on_message"](msg)
                                    except Exception:
                                        pass

                if self._running:
                    retries += 1
                    time.sleep(self._reconnect_delay)
            except Exception as e:
                retries += 1
                if self.callbacks.get("on_error"):
                    try:
                        self.callbacks["on_error"](str(e))
                    except Exception:
                        pass
                if self._running and retries < self._max_retries:
                    time.sleep(self._reconnect_delay)

        with self._lock:
            self._running = False
            if self._socket:
                try:
                    self._socket.close()
                except Exception:
                    pass
                self._socket = None

        self._notify_disconnect()

    def _notify_disconnect(self):
        with self._lock:
            if self._disconnect_notified:
                return
            self._disconnect_notified = True

        if self.callbacks.get("on_disconnect"):
            try:
                self.callbacks["on_disconnect"]()
            except Exception:
                pass
