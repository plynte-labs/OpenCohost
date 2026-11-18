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
    _PIPER_IMPORT_ERROR = None
except (ImportError, OSError) as exc:
    _piper_voice = None  # type: ignore[assignment]
    _PIPER_AVAILABLE = False
    _PIPER_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
    logger.warning("Piper import unavailable: %s", _PIPER_IMPORT_ERROR)

try:
    # Available since piper-tts 1.2; older versions only support the
    # two-argument synthesize_wav call (model-default speaking rate).
    from piper.config import SynthesisConfig as _SynthesisConfig
    _PIPER_SYNTHESIS_CONFIG_IMPORT_ERROR = None
except (ImportError, OSError) as exc:
    _SynthesisConfig = None  # type: ignore[assignment]
    _PIPER_SYNTHESIS_CONFIG_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
    logger.warning("Piper SynthesisConfig unavailable: %s", _PIPER_SYNTHESIS_CONFIG_IMPORT_ERROR)


class PiperEngine:
    """
    Wrapper around a Piper ONNX voice model for offline TTS synthesis.

    Thread-safe: synthesize() acquires an internal lock so multiple producer
    threads cannot race on the same voice object.
    """

    def __init__(self, model_path: str, length_scale: float = 1.0, speaker_id: int | None = None) -> None:
        self._model_path = model_path
        # 1.0 = model default; >1.0 slows speech down proportionally.
        # Requires SynthesisConfig (piper-tts >= 1.2); silently ignored otherwise.
        self._length_scale = float(length_scale or 1.0)
        # Multi-speaker models: which speaker to use (None = the model default).
        self._speaker_id = speaker_id
        self._syn_config = None
        self._rebuild_syn_config()
        self._voice = None
        self._lock = threading.Lock()
        # Stable code for the last failed reload/speaker change (None after a success).
        self.last_error: str | None = None

    def _rebuild_syn_config(self) -> None:
        """Build the SynthesisConfig only when something differs from the model defaults."""
        self._syn_config = None
        if _SynthesisConfig is None:
            return
        kwargs = {}
        if self._length_scale != 1.0:
            kwargs["length_scale"] = self._length_scale
        if self._speaker_id is not None:
            kwargs["speaker_id"] = self._speaker_id
        if kwargs:
            self._syn_config = _SynthesisConfig(**kwargs)

    @staticmethod
    def _num_speakers(voice) -> int | None:
        """``num_speakers`` of a loaded voice, or None when it cannot be determined."""
        value = getattr(getattr(voice, "config", None), "num_speakers", None)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @classmethod
    def _speaker_ok(cls, voice, speaker_id) -> bool:
        if speaker_id is None:
            return True
        if isinstance(speaker_id, bool) or not isinstance(speaker_id, int) or speaker_id < 0:
            return False
        count = cls._num_speakers(voice)
        return count is None or speaker_id < count

    @property
    def speaker_id(self) -> int | None:
        return self._speaker_id

    @property
    def num_speakers(self) -> int | None:
        return self._num_speakers(self._voice)

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
            if not self._speaker_ok(self._voice, self._speaker_id):
                logger.warning("Piper: speaker_id fuera de rango para el modelo; se usa el predeterminado")
                self._speaker_id = None
                self._rebuild_syn_config()
            logger.info("Piper TTS cargado: %s", self._model_path)
            return True
        except FileNotFoundError:
            logger.warning(
                "Piper: modelo no encontrado en '%s'. Configura TTS_LOCAL_MODEL_PATH con una ruta valida.",
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
            self._rebuild_syn_config()

    def set_speaker_id(self, speaker_id: int | None) -> bool:
        """Select a speaker of the loaded voice (None = model default).

        Rejects ids outside ``[0, num_speakers)`` when the count is known; the
        previous speaker is kept and ``last_error`` carries the code.
        """
        with self._lock:
            if not self._speaker_ok(self._voice, speaker_id):
                self.last_error = "speaker_out_of_range"
                return False
            self._speaker_id = speaker_id
            self._rebuild_syn_config()
            self.last_error = None
            return True

    def reload(self, model_path: str, speaker_id: int | None = None) -> bool:
        """Swap to a different Piper voice model (and speaker) at runtime.

        Powers the in-app voice switch. Takes the synthesis lock so an in-flight
        synthesize() completes on the old voice before the swap. On any failure
        (load error, speaker outside the new model's range) the previous voice
        AND speaker stay in place, ``last_error`` carries a stable code, and False
        is returned; never raises.
        """
        with self._lock:
            if not _PIPER_AVAILABLE or not model_path:
                self.last_error = "piper_unavailable"
                return False
            try:
                candidate = _piper_voice.PiperVoice.load(model_path)
            except Exception as exc:
                logger.warning(
                    "Piper: error al recargar el modelo '%s': %s", model_path, exc
                )
                self.last_error = "load_failed"
                return False  # the previous voice was never replaced
            if not self._speaker_ok(candidate, speaker_id):
                logger.warning("Piper: speaker_id fuera de rango para '%s'", model_path)
                self.last_error = "speaker_out_of_range"
                return False
            self._voice = candidate
            self._model_path = model_path
            self._speaker_id = speaker_id
            self._rebuild_syn_config()
            self.last_error = None
            logger.info("Piper TTS recargado: %s", model_path)
            return True

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
