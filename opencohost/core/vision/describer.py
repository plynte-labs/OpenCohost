"""VisionDescriber port and its Ollama adapter (vision-on-demand V1.3).

The adapter talks to a *dedicated* Ollama daemon (default port 11435) that
holds the vision model, so it never touches the main model manager or the
voice model's daemon on 11434.

Abort design: the request body is read through ``httpx`` response streaming
(Ollama itself is called with ``stream: false``, so the body is one JSON
document that only arrives when generation ends). A small watcher thread
waits on the caller's ``cancel`` event and on a wall-clock deadline; when
either fires it closes the in-flight response (and the client, when this
adapter owns it), which closes the socket and unblocks the reading thread.
Closing the socket is what lets Ollama free its runner -- the same behavior
the streaming track relies on and has not yet validated on the adverse path.

Privacy: the image, the question and the description are never logged.
Logs carry the ``[VISION]`` tag, a reason code and latency only.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
from typing import Literal, Protocol, Sequence, Union, runtime_checkable

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:11435"
DEFAULT_MODEL = "qwen3-vl:4b-instruct"
DEFAULT_NUM_CTX = 4096
# V1.21: 120 tokens cut on-screen text off mid-sentence (owner run 2026-10-02).
DEFAULT_NUM_PREDICT = 350
# V1.8: the controller pre-warms the model with this residency and unloads it
# (keep_alive 0) on disable/shutdown/model change. -1 = "until unloaded": the
# daemon is dedicated to this one model, so an idle timeout would only make the
# next look pay a ~3-7 s cold load inside the describe budget (V1.7 root cause).
# Every describe sends the same value so a call never shortens the residency.
VISION_KEEP_ALIVE: int = -1
DEFAULT_KEEP_ALIVE = VISION_KEEP_ALIVE

_WATCH_POLL_S = 0.02

VisionDescribeReason = Literal["timeout", "http", "empty", "cancelled", "unavailable"]

# V0.5 live run: the describer drifted into answering/recommending and twice
# replied in English to Spanish questions. The prompt pins a purely
# descriptive role. V1.33: the prompt is written natively in the app language
# (a 2B VL model follows the prompt's language, not an instruction about it)
# and ALWAYS translates: everything, including on-screen text, comes back in
# the app language so read-aloud never speaks a foreign language.
DESCRIBER_SYSTEM_PROMPT = (
    "You describe a screenshot for another assistant. "
    "You never answer the question, recommend, advise, roleplay or address the player; "
    "another assistant will do that using your description. "
    "State only what is visible on the screen that is relevant to the question. "
    "Transcribe the relevant visible text faithfully and completely (titles, paragraphs, "
    "labels), translating it into English, and quote exact on-screen numbers. "
    "If the relevant thing is not visible, say plainly that it is not visible. "
    "Keep it to one compact paragraph. "
    "Always write everything in English, even if the question or the screen text is in "
    "another language."
)

DESCRIBER_SYSTEM_PROMPT_ES = (
    "Describes una captura de pantalla para otro asistente. "
    "Nunca respondes la pregunta, ni recomiendas, aconsejas, interpretas un personaje "
    "ni te diriges al jugador; otro asistente lo hará usando tu descripción. "
    "Indica solo lo que se ve en la pantalla y es relevante para la pregunta. "
    "Transcribe el texto visible relevante de forma fiel y completa (títulos, párrafos, "
    "etiquetas), traduciéndolo al español, y cita exactamente los números que se vean. "
    "Si lo relevante no se ve, di claramente que no se ve. "
    "Máximo un párrafo compacto. "
    "Escribe siempre todo en español, aunque la pregunta o el texto de la pantalla "
    "estén en otro idioma."
)

_USER_PROMPT_TEMPLATE = (
    "Player question (for relevance only): {question}\n"
    "Describe what is visible in the screenshot that is relevant to it."
)

_USER_PROMPT_TEMPLATE_ES = (
    "Pregunta del jugador (solo para saber qué es relevante): {question}\n"
    "Describe lo que se ve en la captura y es relevante para ella."
)

# V1.11: a look sends a compacted burst of up to 3 frames in one request.
MULTI_FRAME_HINT = (
    "These are frames in time order; describe what is visible now and what changed."
)

MULTI_FRAME_HINT_ES = (
    "Estos son fotogramas en orden temporal; describe lo que se ve ahora y lo que cambió."
)

# V1.26: a clip look sends ONE image that is a 3x2 grid of frames (mosaic.py).
MOSAIC_HINT = (
    "This single image is a grid of frames in time order, left to right, top to bottom; "
    "each cell has its index. Describe what happens over time and quote visible text."
)

MOSAIC_HINT_ES = (
    "Esta imagen única es una cuadrícula de fotogramas en orden temporal, de izquierda a "
    "derecha y de arriba abajo; cada celda lleva su índice. Describe lo que ocurre con el "
    "tiempo y cita el texto visible."
)

DESCRIBER_LANGS = ("es", "en")


def resolve_describer_lang() -> str:
    """App language for the describer prompts: ``es`` or ``en`` (default ``en``).

    Reads the active locale bundle's primary subtag; any other locale or any
    failure falls back to English. Never raises."""
    try:
        from opencohost.i18n import active as i18n_active
        from opencohost.i18n.coherence import primary_subtag

        lang = primary_subtag(i18n_active.get_active_bundle().code)
    except Exception:
        return "en"
    return lang if lang in DESCRIBER_LANGS else "en"


def _prompts(lang: str) -> tuple[str, str, str, str]:
    """(system, user template, multi-frame hint, mosaic hint) for ``lang``."""
    if lang == "es":
        return (
            DESCRIBER_SYSTEM_PROMPT_ES,
            _USER_PROMPT_TEMPLATE_ES,
            MULTI_FRAME_HINT_ES,
            MOSAIC_HINT_ES,
        )
    return DESCRIBER_SYSTEM_PROMPT, _USER_PROMPT_TEMPLATE, MULTI_FRAME_HINT, MOSAIC_HINT


# One JPEG, or several in time order (oldest first).
Frames = Union[bytes, Sequence[bytes]]


def _as_frames(jpeg: Frames) -> list[bytes]:
    if isinstance(jpeg, (bytes, bytearray, memoryview)):
        return [bytes(jpeg)]
    return [bytes(frame) for frame in jpeg]


class VisionDescribeError(Exception):
    """A describe call failed; ``reason`` is a stable code safe to log.

    The message never carries the question, the description or the body.
    """

    def __init__(self, reason: VisionDescribeReason, detail: str = "") -> None:
        self.reason: VisionDescribeReason = reason
        self.detail = detail  # stable code from the caller, e.g. "model_warming"
        super().__init__(f"vision_describe_{reason}" + (f": {detail}" if detail else ""))


@runtime_checkable
class VisionDescriber(Protocol):
    """Image + question -> short description text."""

    def describe(
        self,
        jpeg: Frames,
        question: str,
        *,
        timeout_s: float,
        cancel: threading.Event | None = None,
        num_ctx: int | None = None,
        hint: str | None = None,
        num_predict: int | None = None,
    ) -> str: ...


class _AbortWatcher:
    """Closes an in-flight request on cancel or deadline, from its own thread."""

    def __init__(
        self,
        *,
        cancel: threading.Event | None,
        deadline: float,
        owned_client: httpx.Client | None,
    ) -> None:
        self._cancel = cancel
        self._deadline = deadline
        self._owned_client = owned_client
        self._response: httpx.Response | None = None
        self._lock = threading.Lock()
        self._done = threading.Event()
        self.cancelled = False
        self.timed_out = False
        self._thread = threading.Thread(target=self._run, name="vision-describe-abort", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def attach(self, response: httpx.Response) -> None:
        with self._lock:
            self._response = response
            aborted = self.cancelled or self.timed_out
        if aborted:
            self._abort()

    def stop(self) -> None:
        self._done.set()
        self._thread.join(timeout=1.0)

    def _run(self) -> None:
        while True:
            remaining = self._deadline - time.monotonic()
            if self._done.wait(max(0.0, min(_WATCH_POLL_S, remaining))):
                return
            if self._cancel is not None and self._cancel.is_set():
                self.cancelled = True
                break
            if time.monotonic() >= self._deadline:
                self.timed_out = True
                break
        self._abort()

    def _abort(self) -> None:
        with self._lock:
            response = self._response
        for closer in (response, self._owned_client):
            if closer is None:
                continue
            try:
                closer.close()
            except Exception:  # best effort: the reader thread handles the fallout
                logger.debug("[VISION] abort close failed", exc_info=True)


class OllamaVisionDescriber:
    """``VisionDescriber`` backed by Ollama ``/api/chat`` on its own port."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        num_ctx: int = DEFAULT_NUM_CTX,
        num_predict: int = DEFAULT_NUM_PREDICT,
        client: httpx.Client | None = None,
        *,
        keep_alive: int | str = DEFAULT_KEEP_ALIVE,
        lang: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.num_ctx = num_ctx
        self.num_predict = num_predict
        self.keep_alive = keep_alive
        self._client = client
        # Fixed at construction: the app locale only changes with a restart.
        self.lang = lang if lang in DESCRIBER_LANGS else (
            resolve_describer_lang() if lang is None else "en"
        )

    def _payload(
        self,
        jpeg: Frames,
        question: str,
        *,
        num_ctx: int | None = None,
        hint: str | None = None,
        num_predict: int | None = None,
    ) -> dict:
        # ``think`` is deliberately omitted: the instruct build rejects it.
        frames = _as_frames(jpeg)
        system_prompt, template, multi_hint, mosaic_hint = _prompts(self.lang)
        content = template.format(question=question)
        if hint:
            # The clip mosaic hint is passed as the English constant; swap in
            # the localized wording so clip mode follows the app language too.
            content = f"{content}\n{mosaic_hint if hint == MOSAIC_HINT else hint}"
        elif len(frames) > 1:
            content = f"{content}\n{multi_hint}"
        return {
            "model": self.model,
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": 0,
                "num_ctx": num_ctx or self.num_ctx,
                "num_predict": num_predict or self.num_predict,
            },
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": content,
                    "images": [base64.b64encode(f).decode("ascii") for f in frames],
                },
            ],
        }

    def describe(
        self,
        jpeg: Frames,
        question: str,
        *,
        timeout_s: float,
        cancel: threading.Event | None = None,
        num_ctx: int | None = None,
        hint: str | None = None,
        num_predict: int | None = None,
    ) -> str:
        """Describe one JPEG, or several frames (time order) in one request.

        ``num_ctx`` overrides the context for this call and ``hint`` replaces
        the multi-frame wording (the clip mosaic). A different ``num_ctx`` than
        the warmed one makes Ollama reload the model, so callers keep it equal
        to the warm value unless the frames genuinely need more. ``num_predict``
        overrides the output cap for this call (V1.28 text mode)."""
        started = time.monotonic()
        try:
            text = self._describe(
                jpeg, question, timeout_s=timeout_s, cancel=cancel, started=started,
                num_ctx=num_ctx, hint=hint, num_predict=num_predict,
            )
        except VisionDescribeError as exc:
            logger.info(
                "[VISION] describe_failed reason=%s latency_ms=%d",
                exc.reason,
                int((time.monotonic() - started) * 1000),
            )
            raise
        logger.info("[VISION] describe_ok latency_ms=%d", int((time.monotonic() - started) * 1000))
        return text

    def _describe(
        self,
        jpeg: Frames,
        question: str,
        *,
        timeout_s: float,
        cancel: threading.Event | None,
        started: float,
        num_ctx: int | None = None,
        hint: str | None = None,
        num_predict: int | None = None,
    ) -> str:
        if cancel is not None and cancel.is_set():
            raise VisionDescribeError("cancelled")
        if not _as_frames(jpeg):
            raise VisionDescribeError("empty", "no_frames")

        payload = self._payload(
            jpeg, question, num_ctx=num_ctx, hint=hint, num_predict=num_predict
        )
        owned = self._client is None
        client = httpx.Client() if owned else self._client
        watcher = _AbortWatcher(
            cancel=cancel,
            deadline=started + timeout_s,
            owned_client=client if owned else None,
        )
        watcher.start()
        try:
            with client.stream(
                "POST",
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=httpx.Timeout(timeout_s),
            ) as response:
                watcher.attach(response)
                if response.status_code >= 400:
                    raise VisionDescribeError("http", f"status={response.status_code}")
                body = response.read()
        except VisionDescribeError:
            raise
        except httpx.TimeoutException as exc:
            raise self._abort_reason(watcher) or VisionDescribeError("timeout") from exc
        except httpx.ConnectError as exc:
            raise self._abort_reason(watcher) or VisionDescribeError("unavailable") from exc
        except (httpx.HTTPError, httpx.StreamError, RuntimeError) as exc:
            raise self._abort_reason(watcher) or VisionDescribeError("http", type(exc).__name__) from exc
        finally:
            watcher.stop()
            if owned:
                client.close()

        aborted = self._abort_reason(watcher)
        if aborted is not None:
            raise aborted
        text = self._parse(body)
        self._record_usage(body, started)
        return text

    def _record_usage(self, body: bytes, started: float) -> None:
        """Count this vision call in the usage ledger (numbers only; never raises)."""
        try:
            from opencohost.core.usage import ledger

            data = json.loads(body)
            ledger.record_response(
                data if isinstance(data, dict) else None, model=self.model, is_local=True,
                kind="vision", wall_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception:
            logger.debug("usage ledger vision hook failed", exc_info=True)

    @staticmethod
    def _abort_reason(watcher: _AbortWatcher) -> VisionDescribeError | None:
        if watcher.cancelled:
            return VisionDescribeError("cancelled")
        if watcher.timed_out:
            return VisionDescribeError("timeout")
        return None

    @staticmethod
    def _parse(body: bytes) -> str:
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise VisionDescribeError("http", "invalid_json") from exc
        if not isinstance(data, dict):
            raise VisionDescribeError("http", "invalid_json")
        message = data.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise VisionDescribeError("empty")
        return content.strip()
