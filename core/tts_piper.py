"""
Offline TTS fallback engine using Piper (ONNX Runtime, in-process).

This module is importable even when piper-tts is not installed.
The _PIPER_AVAILABLE flag gates all actual piper usage.
"""
import logging
import threading
import wave

logger = logging.getLogger(__name__)

try:
    import piper.voice as _piper_voice
    _PIPER_AVAILABLE = True
except ImportError:
    _piper_voice = None  # type: ignore[assignment]
    _PIPER_AVAILABLE = False


class PiperEngine:
    """
    Wrapper around a Piper ONNX voice model for offline TTS synthesis.

    Thread-safe: synthesize() acquires an internal lock so multiple producer
    threads cannot race on the same voice object.
    """

    def __init__(self, model_path: str) -> None:
        self._model_path = model_path
        self._voice = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(self) -> bool:
        """
        Load the Piper ONNX voice model from self._model_path.

        Returns True on success; False (never raises) on any failure:
        - piper-tts not installed
        - empty model path
        - model file not found on disk
        - any exception from PiperVoice.load()
        """
        if not _PIPER_AVAILABLE:
            logger.warning(
                "TTS local no disponible: instala piper-tts y configura "
                "TTS_LOCAL_MODEL_PATH"
            )
            return False

        if not self._model_path:
            logger.warning(
                "TTS local desactivado: TTS_LOCAL_MODEL_PATH esta vacio"
            )
            return False

        try:
            self._voice = _piper_voice.PiperVoice.load(self._model_path)
            logger.info("Piper TTS cargado: %s", self._model_path)
            return True
        except FileNotFoundError:
            logger.warning(
                "Piper: modelo no encontrado en '%s'. "
                "Configura TTS_LOCAL_MODEL_PATH con una ruta valida.",
                self._model_path,
            )
            return False
        except Exception as exc:
            logger.warning("Piper: error al cargar el modelo: %s", exc)
            return False

    def is_available(self) -> bool:
        """Return True only if a voice model was successfully loaded."""
        return self._voice is not None

    def synthesize(self, text: str, output_path: str) -> bool:
        """
        Synthesize *text* and write the result as a WAV file to *output_path*.

        Returns True on success, False (never raises) on any synthesis error.
        Acquires the internal lock to serialize concurrent calls.
        """
        with self._lock:
            try:
                with wave.open(output_path, "wb") as wav_file:
                    self._voice.synthesize_wav(text, wav_file)
                return True
            except Exception as exc:
                logger.warning("Piper: error en sintesis: %s", exc)
                return False
