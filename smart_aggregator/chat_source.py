import threading
import time
from typing import Optional, Dict, Any

try:
    import pytchat
    _PYTCHAT_AVAILABLE = True
except ImportError:
    _PYTCHAT_AVAILABLE = False

class YouTubeChatSource:
    def __init__(self, config: dict, callbacks: dict):
        self.config = config
        self.callbacks = callbacks
        self._video_id: Optional[str] = None
        self._chat = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._reconnect_delay = config.get("reconnect_delay_seconds", 5)
        self._max_retries = config.get("max_retries", 3)
        self._interruptable = config.get("interruptable", False)
        self._disconnect_notified = False
    
    def connect(self, video_id: str):
        if not video_id:
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
            self._video_id = video_id
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
                    video_id=self._video_id,
                    interruptable=self._interruptable
                )
                if self.callbacks.get("on_connect"):
                    try:
                        self.callbacks["on_connect"]({"video_id": self._video_id})
                    except Exception:
                        pass
                
                while self._running and self._chat.is_alive():
                    for c in self._chat.get().sync_items():
                        if not self._running:
                            break
                        msg = {
                            "user": c.author.name,
                            "text": c.message,
                            "timestamp": time.time()
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
