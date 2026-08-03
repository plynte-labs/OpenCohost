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

try:
    # Available since piper-tts 1.2; older versions only support the
    # two-argument synthesize_wav call (model-default speaking rate).
    from piper.config import SynthesisConfig as _SynthesisConfig
except ImportError:
    _SynthesisConfig = None  # type: ignore[assignment]


class PiperEngine:
    """
    Wrapper around a Piper ONNX voice model for offline TTS synthesis.

    Thread-safe: synthesize() acquires an internal lock so multiple producer
    threads cannot race on the same voice object.
    """

    def __init__(self, model_path: str, length_scale: float = 1.0) -> None:
        self._model_path = model_path
        # 1.0 = model default; >1.0 slows speech down proportionally.
        # Requires SynthesisConfig (piper-tts >= 1.2); silently ignored otherwise.
        self._length_scale = float(length_scale or 1.0)
        self._syn_config = None
        if _SynthesisConfig is not None and self._length_scale != 1.0:
            self._syn_config = _SynthesisConfig(length_scale=self._length_scale)
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

    def set_length_scale(self, length_scale: float) -> None:
        """Change the speaking rate at runtime (1.0 = default; >1.0 = slower).

        Takes the synthesis lock so an in-flight synthesize() always uses a
        consistent config. No-op rate-wise on piper-tts without SynthesisConfig.
        """
        with self._lock:
            self._length_scale = float(length_scale or 1.0)
            self._syn_config = None
            if _SynthesisConfig is not None and self._length_scale != 1.0:
                self._syn_config = _SynthesisConfig(length_scale=self._length_scale)

    def reload(self, model_path: str) -> bool:
        """Swap to a different Piper voice model at runtime.

        Powers the in-app Argentina↔Neutral voice toggle. Takes the synthesis
        lock so an in-flight synthesize() completes on the old voice before the
        swap. On failure the previous voice stays loaded (the engine is never
        left voiceless) and False is returned; never raises.
        """
        with self._lock:
            if not _PIPER_AVAILABLE or not model_path:
                return False
            previous = self._voice
            try:
                self._voice = _piper_voice.PiperVoice.load(model_path)
                self._model_path = model_path
                logger.info("Piper TTS recargado: %s", model_path)
                return True
            except Exception as exc:
                logger.warning(
                    "Piper: error al recargar el modelo '%s': %s", model_path, exc
                )
                self._voice = previous  # keep the working voice
                return False

    def synthesize(self, text: str, output_path: str) -> bool:
        """
        Synthesize *text* and write the result as a WAV file to *output_path*.

        Returns True on success, False (never raises) on any synthesis error.
        Acquires the internal lock to serialize concurrent calls.
        """
        with self._lock:
            try:
                with wave.open(output_path, "wb") as wav_file:
                    if self._syn_config is not None:
                        self._voice.synthesize_wav(text, wav_file, syn_config=self._syn_config)
                    else:
                        self._voice.synthesize_wav(text, wav_file)
                return True
            except Exception as exc:
                logger.warning("Piper: error en sintesis: %s", exc)
                return False
