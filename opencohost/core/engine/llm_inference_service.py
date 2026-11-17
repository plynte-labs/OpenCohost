"""Pure LLM inference service extracted from ``MotorVocalIA``.

This service encapsulates model dispatching to local Ollama runners and cloud
providers, client lifecycle timeouts, and streaming token generation without
coupling to audio queues or UI loops.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import inspect
import logging
import re
import threading
import time
from typing import Any, Callable, Iterator, Optional
import uuid

import httpx

try:
    import ollama
except ImportError:
    ollama = None

from opencohost.config.settings import (
    LLM_PROVIDER_PRESETS,
    CLOUD_CHAT_TIMEOUT,
    CLOUD_DISABLE_REASONING,
    CLOUD_STREAM_FIRST_CHUNK_TIMEOUT_SECONDS,
    CLOUD_STREAM_IDLE_TIMEOUT_SECONDS,
    LLM_KEYS_FILE,
    OLLAMA_CHAT_TIMEOUT,
    OLLAMA_REQUEST_TIMEOUT,
    STREAM_IDLE_PROBE_SECONDS,
    STREAM_IDLE_TIMEOUT_SECONDS,
)
from opencohost.core.providers.cloud import cloud_llm_client
from opencohost.core.speech.sentence_splitter import SentenceSplitter
from opencohost.stream_admin.oauth_store import OAuthStore

logger = logging.getLogger("OpenCohost")


# ── Immutable cloud sampling params (evidence 2026-09-09, NVIDIA NIM) ──────
#
# Some OpenAI-compatible providers pin sampling knobs per model and answer a
# fast HTTP 400 naming the required value instead of coercing, e.g. NIM:
#   "Validation: `top_p` is immutable for this model and must be 0.95, got 0.9"
# Our engine always sends its own `LLM_TOP_P` (0.9), so EVERY turn to such a
# model burns instantly with `cloud_llm_error` — no timeout, no retry, just a
# dead provider that looks configured. `readiness` cannot catch it: the
# `GET /models` probe never validates sampling params.
#
# `cloud_chat` below therefore retries a 400 that names an immutable param,
# bounded (1 original + up to 3 single-param corrections) and cached per
# (base_url, model) so later turns skip the 400 round-trip. Anything without a
# parseable `must be <number>` requirement re-raises untouched.

_IMMUTABLE_PARAM_RE = re.compile(r"`([A-Za-z_]\w*)`[^`]{0,200}?must be (\d+(?:\.\d+)?)")

# OpenAI body name -> Ollama-shaped options key used by our callers.
_IMMUTABLE_PARAM_MAP = {
    "temperature": "temperature",
    "top_p": "top_p",
    "presence_penalty": "presence_penalty",
    "frequency_penalty": "frequency_penalty",
    "max_tokens": "num_predict",
}

_MAX_IMMUTABLE_FIXES = 3

# ── Reasoning-kwargs 400 fallback (nim-cloud-reasoning-off, T2) ────────────
#
# T1 defaults every cloud request to disable_reasoning=True, which adds a
# `chat_template_kwargs` body key. Not every OpenAI-compatible model accepts
# that key -- a 400 naming it must not kill the turn, and must not repeat
# the same failed request forever. `cloud_chat` retries once without the
# key on a 400 whose body_excerpt mentions it, then caches that (base_url,
# model) as "no kwargs" so later turns skip the 400 round-trip.
_REASONING_KWARGS_MARKER = "chat_template_kwargs"

# ── Local ``think`` transport (warmth-guardrails-runtime-20261006 K1) ──────
#
# Callers carry the reasoning switch as ``options["think"]`` because that is
# where the cloud client reads it. Ollama does not: ``think`` is a top-level
# ``Client.chat(think=...)`` argument (ollama-python >= 0.5), and an options
# key is dropped with ``invalid option provided option=think``, so gemma4
# reasoned on every turn. The local transport lifts it out of ``options``.
_THINK_UNSUPPORTED_WARNED = False


def _accepts_think(chat_fn: Any) -> bool:
    """True when *chat_fn* takes a ``think`` argument (or ``**kwargs``)."""
    try:
        params = inspect.signature(chat_fn).parameters.values()
    except (TypeError, ValueError):
        return True
    return any(p.name == "think" or p.kind is inspect.Parameter.VAR_KEYWORD for p in params)


def _lift_think(client: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Move ``options["think"]`` to a top-level ``think`` kwarg for a local
    Ollama call. The caller's options dict is never mutated. A client whose
    ``chat`` has no ``think`` parameter gets neither (warned once)."""
    global _THINK_UNSUPPORTED_WARNED
    options = kwargs.get("options")
    if isinstance(options, dict) and "think" in options:
        options = dict(options)
        think = options.pop("think")
        kwargs = {**kwargs, "options": options}
        kwargs.setdefault("think", think)
    if "think" in kwargs and not _accepts_think(getattr(client, "chat", None)):
        kwargs = {k: v for k, v in kwargs.items() if k != "think"}
        if not _THINK_UNSUPPORTED_WARNED:
            _THINK_UNSUPPORTED_WARNED = True
            logger.warning(
                "Ollama client chat() has no think parameter; reasoning control "
                "is not sent (upgrade the ollama package)"
            )
    return kwargs


# A model without the thinking capability rejects a top-level ``think`` with
# HTTP 400 '"<model>" does not support thinking' (live log 2026-10-06; before
# K1 the options key was silently dropped). The local transport retries such a
# request once without ``think`` and remembers the model for the process, so
# later calls omit it up front instead of paying the 400 round-trip each turn.
_THINK_UNSUPPORTED_MODELS: set[str] = set()
_THINK_UNSUPPORTED_LOCK = threading.Lock()
_THINK_UNSUPPORTED_MARKER = "does not support thinking"


def _is_think_unsupported_error(exc: BaseException) -> bool:
    if getattr(exc, "status_code", None) != 400:
        return False
    text = f"{getattr(exc, 'error', '') or ''} {exc}"
    return _THINK_UNSUPPORTED_MARKER in text.lower()


def _without_think(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in kwargs.items() if k != "think"}


def _remember_think_unsupported(kwargs: dict[str, Any]) -> None:
    model = str(kwargs.get("model") or "")
    with _THINK_UNSUPPORTED_LOCK:
        if model in _THINK_UNSUPPORTED_MODELS:
            return
        _THINK_UNSUPPORTED_MODELS.add(model)
    logger.warning("[THINK_UNSUPPORTED] model=%s", model)


def _prepare_local_kwargs(client: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    """``_lift_think`` plus dropping ``think`` for a model known to reject it."""
    kwargs = _lift_think(client, kwargs)
    if "think" in kwargs and str(kwargs.get("model") or "") in _THINK_UNSUPPORTED_MODELS:
        kwargs = _without_think(kwargs)
    return kwargs


def local_chat(client: Any, kwargs: dict[str, Any]) -> Any:
    """Buffered local Ollama ``chat`` with the think-unsupported fallback."""
    call_kwargs = _prepare_local_kwargs(client, kwargs)
    try:
        return client.chat(**call_kwargs)
    except Exception as exc:
        if "think" not in call_kwargs or not _is_think_unsupported_error(exc):
            raise
        _remember_think_unsupported(call_kwargs)
    return client.chat(**_without_think(call_kwargs))


def _close_stream(stream: Any) -> None:
    if stream is not None and hasattr(stream, "close"):
        try:
            stream.close()
        except Exception:
            logger.debug("Failed closing response stream", exc_info=True)


def _log_stream_watchdog(kind: str, budget: float, *, model: Any, elapsed: float) -> None:
    """One line where a local streamed call is cut by the watchdog. ``idle`` is
    the per-read budget (no bytes within it); ``total`` is the wall-clock cap.
    Logs the model name and timing only, never chat text."""
    logger.warning(
        "[STREAM_WATCHDOG] kind=%s watchdog_timeout:%.2fs model=%s elapsed_s=%.2f",
        kind, budget, model, elapsed,
    )


def _iter_local_stream(client: Any, kwargs: dict[str, Any]) -> Iterator[Any]:
    """Streaming local Ollama ``chat`` with the think-unsupported fallback.

    ollama-python raises the 400 lazily, on the first iteration. Retry only
    while nothing has been yielded; after a chunk the error re-raises."""
    call_kwargs = _prepare_local_kwargs(client, kwargs)
    stream = None
    yielded = False
    try:
        try:
            stream = client.chat(stream=True, **call_kwargs)
            for chunk in stream:
                yielded = True
                yield chunk
            return
        except Exception as exc:
            if yielded or "think" not in call_kwargs or not _is_think_unsupported_error(exc):
                raise
            _remember_think_unsupported(call_kwargs)
        _close_stream(stream)
        stream = client.chat(stream=True, **_without_think(call_kwargs))
        yield from stream
    finally:
        _close_stream(stream)


@dataclass(frozen=True)
class InferenceRequest:
    """Normalized inference request across local and cloud providers."""

    messages: list[dict[str, Any]]
    model: str
    options: dict[str, Any] = field(default_factory=dict)
    is_local: bool = True
    provider_config: dict[str, Any] = field(default_factory=dict)
    watchdog_timeout: Optional[float] = None
    stream: bool = False


@dataclass
class InferenceResponse:
    """Normalized inference result."""

    text: str
    gen_ms: int = -1
    provider: str = "local"
    is_local: bool = True
    raw: Any = None
    error: Optional[str] = None


def configured_cloud_model(provider_cfg: Optional[dict[str, Any]]) -> str:
    """Return only the selected profile's model, never its credentials."""
    cfg = provider_cfg or {}
    active_id = cfg.get("active_provider")
    profiles = cfg.get("profiles") or {}
    profile = profiles.get(active_id) if active_id else None
    return str(profile.get("model") or "") if profile is not None else ""


def get_inference_service(engine: Any) -> LLMInferenceService:
    """Retrieve or dynamically bind an LLMInferenceService for an engine instance."""
    service = getattr(engine, "_inference_service", None)
    if service is not None and isinstance(service, LLMInferenceService):
        return service
    service = LLMInferenceService(
        ollama_resolver=lambda: getattr(
            engine, "ollama", getattr(engine, "_ollama_chat_client", None)
        ),
        api_key_resolver=lambda pid: (
            getattr(engine, "_cloud_api_key")(pid)
            if hasattr(engine, "_cloud_api_key")
            else ""
        ),
        clients_resolver=lambda: getattr(engine, "_ollama_chat_clients", None),
    )
    if hasattr(engine, "_ollama_chat_clients") and isinstance(
        engine._ollama_chat_clients, dict
    ):
        service._ollama_chat_clients = engine._ollama_chat_clients
    if hasattr(engine, "_ollama_chat_client"):
        service.set_default_ollama_client(engine._ollama_chat_client)
    try:
        object.__setattr__(engine, "_inference_service", service)
    except Exception:
        try:
            engine._inference_service = service
        except Exception:
            pass
    return service


class LLMInferenceService:
    """Pure LLM inference runner supporting local Ollama and Cloud providers."""

    def __init__(
        self,
        *,
        ollama_client: Any = None,
        cloud_client_module: Any = None,
        api_key_resolver: Optional[Callable[[str], str]] = None,
        ollama_resolver: Optional[Callable[[], Any]] = None,
        clients_resolver: Optional[Callable[[], Optional[dict[float, Any]]]] = None,
    ) -> None:
        self._ollama = ollama if ollama_client is None else ollama_client
        self._ollama_resolver = ollama_resolver
        self._clients_resolver = clients_resolver
        self._cloud_client = (
            cloud_llm_client if cloud_client_module is None else cloud_client_module
        )
        self._api_key_resolver = api_key_resolver or self._default_cloud_api_key
        self._ollama_chat_clients: dict[float, Any] = {}
        self._default_ollama_client: Any = None
        # (base_url, model) -> {ollama-option: required value} learned from
        # 400 immutable-param rejections (see module note above).
        self._immutable_param_cache: dict[tuple[str, str], dict[str, Any]] = {}
        # (base_url, model) set learned from a chat_template_kwargs 400 (T2
        # module note above): reasoning kwargs are not sent again for it.
        self._no_reasoning_kwargs_cache: set[tuple[str, str]] = set()

    def _get_ollama(self) -> Any:
        if self._ollama_resolver is not None:
            try:
                resolved = self._ollama_resolver()
                if resolved is not None:
                    return resolved
            except Exception:
                pass
        return self._ollama

    def _default_cloud_api_key(self, profile_id: str) -> str:
        token = OAuthStore(LLM_KEYS_FILE).load(profile_id)
        if isinstance(token, dict):
            return str(token.get("api_key") or "")
        return ""

    def create_chat_client(self, client_factory: Any, *, timeout: float) -> Optional[Any]:
        """Pre-warm a memoized client scoped to a specific timeout budget."""
        if not hasattr(client_factory, "Client"):
            return None
        try:
            client = client_factory.Client(timeout=timeout)
            self._ollama_chat_clients[timeout] = client
            return client
        except TypeError as exc:
            logger.warning("Ollama Client does not support timeout: %s", exc)
            return None

    def select_chat_client(self, chat_timeout: Optional[float]) -> Any:
        """Select the memoized client for chat_timeout, or fallback to default."""
        if self._clients_resolver is not None:
            try:
                cache = self._clients_resolver()
                if cache and chat_timeout is not None and chat_timeout in cache:
                    return cache[chat_timeout]
            except Exception:
                pass
        if chat_timeout is not None and chat_timeout in self._ollama_chat_clients:
            return self._ollama_chat_clients[chat_timeout]
        return self._default_ollama_client or self._get_ollama()

    def set_default_ollama_client(self, client: Any) -> None:
        self._default_ollama_client = client

    def call_with_watchdog(
        self,
        call: Callable[..., Any],
        *,
        timeout: float,
        label: str = "OllamaChatWatchdog",
        **kwargs: Any,
    ) -> Any:
        """Run call on a daemon thread and enforce timeout budget."""
        if kwargs.get("stream"):
            raise ValueError(
                "_call_with_watchdog cannot supervise stream=True: a generator-returning "
                "call returns before its body runs, so the watchdog would always succeed. "
                "Use the dedicated streaming seam, which iterates on the calling thread."
            )

        result: dict[str, Any] = {}
        done = threading.Event()

        def worker() -> None:
            try:
                result["response"] = call(**kwargs)
            except httpx.TimeoutException:
                result["error"] = TimeoutError(f"watchdog_timeout:{timeout:.2f}s")
            except Exception as exc:
                result["error"] = exc
            finally:
                done.set()

        thread = threading.Thread(
            target=worker,
            name=f"{label}-{uuid.uuid4().hex[:8]}",
            daemon=True,
        )
        thread.start()
        if not done.wait(timeout=max(0.1, float(timeout))):
            raise TimeoutError(f"watchdog_timeout:{timeout:.2f}s")
        if "error" in result:
            raise result["error"]
        return result.get("response")

    def chat_with_watchdog(
        self,
        *,
        timeout: float,
        chat_callable: Optional[Callable[..., Any]] = None,
        **kwargs: Any,
    ) -> Any:
        """Execute chat under the watchdog budget."""
        call = chat_callable or (lambda **kw: self.chat(chat_timeout=timeout, **kw))
        return self.call_with_watchdog(call, timeout=timeout, **kwargs)

    def chat(
        self,
        *,
        provider_cfg: Optional[dict[str, Any]] = None,
        is_local: bool = True,
        chat_timeout: Optional[float] = None,
        **kwargs: Any,
    ) -> Any:
        """Execute chat dispatching to Cloud or Local Ollama."""
        if not is_local:
            return self.cloud_chat(
                provider_cfg=provider_cfg,
                timeout=chat_timeout or OLLAMA_REQUEST_TIMEOUT,
                **kwargs,
            )
        client = self.select_chat_client(chat_timeout)
        if client is None:
            raise RuntimeError("No Ollama client available for local chat")
        return local_chat(client, kwargs)

    def _immutable_fix(
        self,
        cache_key: tuple[str, str],
        options: dict[str, Any],
        exc: Exception,
    ) -> Optional[dict[str, Any]]:
        """Corrected Ollama-shaped options for a 400 immutable-param rejection.

        Returns None when the error is not a parseable `must be <number>`
        requirement (caller re-raises the original), or when the requirement
        already holds (nothing to correct — retrying would loop).
        """
        if getattr(exc, "status_code", None) != 400:
            return None
        excerpt = str(getattr(exc, "body_excerpt", "") or "")
        match = _IMMUTABLE_PARAM_RE.search(excerpt)
        if match is None:
            return None
        openai_name, raw_value = match.group(1), match.group(2)
        ollama_name = _IMMUTABLE_PARAM_MAP.get(openai_name)
        if ollama_name is None:
            return None
        value: Any = float(raw_value) if "." in raw_value else int(raw_value)
        if options.get(ollama_name) == value:
            return None
        fixed = dict(options)
        fixed[ollama_name] = value
        learned = dict(self._immutable_param_cache.get(cache_key, {}))
        learned[ollama_name] = value
        self._immutable_param_cache[cache_key] = learned
        logger.warning(
            "cloud immutable param: %s must be %r for model %s; retrying with corrected options",
            ollama_name,
            value,
            cache_key[1],
        )
        return fixed

    def _is_reasoning_kwargs_400(self, exc: Exception) -> bool:
        """True for a 400 whose body names ``chat_template_kwargs`` (T2)."""
        if getattr(exc, "status_code", None) != 400:
            return False
        excerpt = str(getattr(exc, "body_excerpt", "") or "")
        return _REASONING_KWARGS_MARKER in excerpt

    def _resolve_cloud_dispatch(
        self, provider_cfg: Optional[dict[str, Any]], options: Optional[dict[str, Any]]
    ) -> tuple[str, str, str, tuple[str, str], dict[str, Any], bool]:
        """Resolve (base_url, api_key, model, cache_key, options, disable_reasoning)
        shared by ``cloud_chat`` and ``_cloud_chat_streaming`` -- same profile
        lookup, same learned immutable-param/no-reasoning-kwargs caches (T2/T8),
        so a lesson learned on one transport is not re-paid on the other."""
        cfg = provider_cfg or {}
        active_id = cfg.get("active_provider")
        profiles = cfg.get("profiles") or {}
        profile = profiles.get(active_id) if active_id else None
        if profile is None:
            raise self._cloud_client.CloudLLMResponseError(
                "cloud provider active but no profile configured"
            )
        api_key = self._api_key_resolver(active_id) if active_id else ""
        base_url = str(profile.get("base_url") or "")
        model = configured_cloud_model(provider_cfg)
        cache_key = (base_url, model)
        opts = dict(options or {})
        for cached_name, cached_value in self._immutable_param_cache.get(cache_key, {}).items():
            opts[cached_name] = cached_value
        disable_reasoning = CLOUD_DISABLE_REASONING and cache_key not in self._no_reasoning_kwargs_cache
        return base_url, api_key, model, cache_key, opts, disable_reasoning

    @staticmethod
    def _cloud_preset_kwargs(provider_cfg: Optional[dict[str, Any]]) -> dict[str, Any]:
        """Per-preset request shaping (reasoning style, extra headers, dropped
        params) for the active profile. Empty for a custom (preset-less)
        profile, which keeps the pre-template call shape and behavior."""
        cfg = provider_cfg or {}
        profile = (cfg.get("profiles") or {}).get(cfg.get("active_provider")) or {}
        preset = LLM_PROVIDER_PRESETS.get(str(profile.get("preset") or ""))
        return cloud_llm_client.preset_request_kwargs(preset)

    def cloud_chat(
        self,
        *,
        provider_cfg: Optional[dict[str, Any]] = None,
        messages: list[dict[str, Any]],
        options: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
        **_ignored: Any,
    ) -> Any:
        """Dispatch chat to cloud provider."""
        base_url, api_key, model, cache_key, opts, disable_reasoning = self._resolve_cloud_dispatch(
            provider_cfg, options
        )
        preset_kw = self._cloud_preset_kwargs(provider_cfg)
        err_cls = getattr(self._cloud_client, "CloudLLMResponseError", None)
        if not (isinstance(err_cls, type) and issubclass(err_cls, Exception)):
            # Test doubles without a real exception type: single direct call,
            # byte-identical to the pre-fix behavior.
            return self._cloud_client.send_chat_completion(
                base_url=base_url,
                api_key=api_key,
                model=model,
                messages=messages,
                options=opts,
                timeout=timeout or OLLAMA_REQUEST_TIMEOUT,
                disable_reasoning=disable_reasoning,
                **preset_kw,
            )
        attempts = 1 + _MAX_IMMUTABLE_FIXES
        for _ in range(attempts):
            try:
                return self._cloud_client.send_chat_completion(
                    base_url=base_url,
                    api_key=api_key,
                    model=model,
                    messages=messages,
                    options=opts,
                    timeout=timeout or OLLAMA_REQUEST_TIMEOUT,
                    disable_reasoning=disable_reasoning,
                    **preset_kw,
                )
            except err_cls as exc:
                if disable_reasoning and self._is_reasoning_kwargs_400(exc):
                    # Retry ONCE without the kwargs (T2) -- do not consume an
                    # immutable-fix attempt, and cache so later turns skip
                    # straight to no-kwargs without paying the 400 again.
                    disable_reasoning = False
                    self._no_reasoning_kwargs_cache.add(cache_key)
                    logger.warning(
                        "cloud chat_template_kwargs rejected (400) for model %s; "
                        "retrying without reasoning kwargs",
                        cache_key[1],
                    )
                    continue
                fixed = self._immutable_fix(cache_key, opts, exc)
                if fixed is None:
                    raise
                opts = fixed
        # Unreachable: the loop above either returns or raises (a no-op fix
        # also raises). Kept as a defensive tail instead of trusting that.
        raise self._cloud_client.CloudLLMResponseError(
            "cloud immutable-param retry budget exhausted"
        )

    def chat_streaming(
        self,
        *,
        timeout: float,
        is_local: bool = True,
        provider_cfg: Optional[dict[str, Any]] = None,
        chat_client: Any = None,
        time_fn: Optional[Callable[[], float]] = None,
        **kwargs: Any,
    ) -> Iterator[Any]:
        """Yield raw chunks bounded by watchdog, local Ollama or cloud SSE
        (live_session_fixes_20260925 T8: ``is_local=False`` dispatches to
        ``_cloud_chat_streaming`` -- same seam, same caller contract, so
        ``GenerationOrchestrator.run_streaming_attempt`` needs no branch of
        its own beyond deciding which transport to ask for)."""
        if not is_local:
            yield from self._cloud_chat_streaming(
                provider_cfg=provider_cfg, timeout=timeout, time_fn=time_fn, **kwargs
            )
            return
        get_time = time_fn or time.monotonic
        idle_budget = float(STREAM_IDLE_TIMEOUT_SECONDS)
        client = chat_client or self.select_chat_client(idle_budget)
        if client is None:
            raise RuntimeError("No Ollama client available for streaming")

        started = get_time()
        first_chunk_pending = True
        stream = None
        try:
            stream = _iter_local_stream(client, kwargs)
            for chunk in stream:
                if first_chunk_pending:
                    first_chunk_pending = False
                    waited = get_time() - started
                    if waited > STREAM_IDLE_PROBE_SECONDS:
                        logger.warning(
                            "[STREAM_IDLE_PROBE] first_chunk_wait_s=%.2f threshold_s=%.2f "
                            "idle_budget_s=%.2f total_budget_s=%.2f",
                            waited,
                            float(STREAM_IDLE_PROBE_SECONDS),
                            idle_budget,
                            float(timeout),
                        )
                if (get_time() - started) > timeout:
                    _log_stream_watchdog(
                        "total", timeout, model=kwargs.get("model"),
                        elapsed=get_time() - started,
                    )
                    raise TimeoutError(f"watchdog_timeout:{timeout:.2f}s")
                yield chunk
        except httpx.TimeoutException:
            _log_stream_watchdog(
                "idle", idle_budget, model=kwargs.get("model"),
                elapsed=get_time() - started,
            )
            raise TimeoutError(f"watchdog_timeout:{idle_budget:.2f}s")
        finally:
            if stream is not None and hasattr(stream, "close"):
                try:
                    stream.close()
                except Exception:
                    logger.debug("Failed closing response stream", exc_info=True)

    def _budget_cloud_stream(
        self,
        stream: Iterator[Any],
        *,
        get_time: Callable[[], float],
        total_budget: float,
    ) -> Iterator[Any]:
        """Enforce the two cloud streaming regimes (T8 design note) around a
        raw ``cloud_llm_client.stream_chat_completion`` generator: a tighter
        wait budget for the FIRST chunk (model load/prefill/queueing) than for
        every chunk after it (a stall once tokens are flowing), plus the same
        overall wall-clock cap the buffered cloud path already uses
        (``timeout``, resolved to ``CLOUD_CHAT_TIMEOUT`` upstream). Exceeding
        either raises the same ``TimeoutError("watchdog_timeout:...")`` shape
        the local streaming seam and ``_call_with_watchdog`` already raise, so
        it lands in the attempt loop's existing classification unchanged.

        The transport's own read timeout (set by the caller to the max of the
        two regimes) is a backstop for a stall this software check cannot see
        (e.g. the very first recv, before any chunk -- or ever -- arrives);
        this is the actual enforcement for the tighter of the two budgets."""
        started = get_time()
        last_chunk_at = started
        first_chunk_pending = True
        for chunk in stream:
            now = get_time()
            budget = (
                float(CLOUD_STREAM_FIRST_CHUNK_TIMEOUT_SECONDS)
                if first_chunk_pending
                else float(CLOUD_STREAM_IDLE_TIMEOUT_SECONDS)
            )
            if (now - last_chunk_at) > budget:
                raise TimeoutError(f"watchdog_timeout:{budget:.2f}s")
            if (now - started) > total_budget:
                raise TimeoutError(f"watchdog_timeout:{total_budget:.2f}s")
            first_chunk_pending = False
            last_chunk_at = now
            yield chunk

    def _cloud_chat_streaming(
        self,
        *,
        provider_cfg: Optional[dict[str, Any]] = None,
        messages: list[dict[str, Any]],
        options: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
        time_fn: Optional[Callable[[], float]] = None,
        **_ignored: Any,
    ) -> Iterator[Any]:
        """Cloud SSE streaming dispatch (live_session_fixes_20260925 T8).

        Mirrors ``cloud_chat``'s bounded immutable-param / reasoning-kwargs
        retry, including the SAME learned per-(base_url, model) caches -- but
        a retry is only attempted before the first chunk is yielded to the
        caller. Once a chunk has been handed out, the turn may already be
        speaking (the orchestrator's lazy job submit): a transport failure
        past that point takes the existing streaming partial-exit path
        upstream, never a silent regenerate-and-replace here.
        """
        get_time = time_fn or time.monotonic
        base_url, api_key, model, cache_key, opts, disable_reasoning = self._resolve_cloud_dispatch(
            provider_cfg, options
        )
        preset_kw = self._cloud_preset_kwargs(provider_cfg)
        total_budget = float(timeout or CLOUD_CHAT_TIMEOUT)
        read_timeout = max(
            float(CLOUD_STREAM_FIRST_CHUNK_TIMEOUT_SECONDS),
            float(CLOUD_STREAM_IDLE_TIMEOUT_SECONDS),
        )
        err_cls = getattr(self._cloud_client, "CloudLLMResponseError", None)
        retryable = isinstance(err_cls, type) and issubclass(err_cls, Exception)

        if not retryable:
            # Test doubles without a real exception type: single direct call,
            # byte-identical to cloud_chat's own fallback for the same case.
            raw_stream = self._cloud_client.stream_chat_completion(
                base_url=base_url,
                api_key=api_key,
                model=model,
                messages=messages,
                options=opts,
                timeout=(read_timeout, read_timeout),
                disable_reasoning=disable_reasoning,
                **preset_kw,
            )
            yield from self._budget_cloud_stream(
                raw_stream, get_time=get_time, total_budget=total_budget
            )
            return

        attempts = 1 + _MAX_IMMUTABLE_FIXES
        for _ in range(attempts):
            raw_stream = self._cloud_client.stream_chat_completion(
                base_url=base_url,
                api_key=api_key,
                model=model,
                messages=messages,
                options=opts,
                timeout=(read_timeout, read_timeout),
                disable_reasoning=disable_reasoning,
                **preset_kw,
            )
            budgeted = self._budget_cloud_stream(
                raw_stream, get_time=get_time, total_budget=total_budget
            )
            try:
                first_chunk = next(budgeted)
            except StopIteration:
                return
            except err_cls as exc:
                if disable_reasoning and self._is_reasoning_kwargs_400(exc):
                    # Retry ONCE without the kwargs (mirrors cloud_chat's T2
                    # retry) -- do not consume an immutable-fix attempt, and
                    # cache so later turns (streamed or buffered) skip
                    # straight to no-kwargs without paying the 400 again.
                    disable_reasoning = False
                    self._no_reasoning_kwargs_cache.add(cache_key)
                    logger.warning(
                        "cloud stream chat_template_kwargs rejected (400) for model %s; "
                        "retrying without reasoning kwargs",
                        cache_key[1],
                    )
                    continue
                fixed = self._immutable_fix(cache_key, opts, exc)
                if fixed is None:
                    raise
                opts = fixed
                continue
            yield first_chunk
            yield from budgeted
            return
        # Unreachable in practice (mirrors cloud_chat's own defensive tail):
        # the loop above either returns or raises before exhausting attempts.
        raise self._cloud_client.CloudLLMResponseError(
            "cloud immutable-param retry budget exhausted"
        )

    def execute_chat(
        self,
        request: InferenceRequest,
        *,
        on_raw_chunk: Optional[Callable[[Any], None]] = None,
    ) -> InferenceResponse:
        """Execute a synchronous chat completion."""
        start_time = time.monotonic()
        try:
            timeout = request.watchdog_timeout or (
                OLLAMA_CHAT_TIMEOUT if request.is_local else OLLAMA_REQUEST_TIMEOUT
            )
            raw = self.chat_with_watchdog(
                timeout=timeout,
                model=request.model,
                messages=request.messages,
                options=request.options,
                provider_cfg=request.provider_config,
                is_local=request.is_local,
            )
            gen_ms = max(0, int((time.monotonic() - start_time) * 1000))
            content = ""
            if isinstance(raw, dict):
                content = raw.get("message", {}).get("content", "") or raw.get("text", "")
            elif hasattr(raw, "message") and hasattr(raw.message, "content"):
                content = raw.message.content
            return InferenceResponse(
                text=content,
                gen_ms=gen_ms,
                provider=request.provider_config.get("active_provider")
                or ("local" if request.is_local else "cloud"),
                is_local=request.is_local,
                raw=raw,
            )
        except Exception as exc:
            gen_ms = max(0, int((time.monotonic() - start_time) * 1000))
            return InferenceResponse(
                text="",
                gen_ms=gen_ms,
                provider="local"
                if request.is_local
                else (request.provider_config.get("active_provider") or "cloud"),
                is_local=request.is_local,
                error=str(exc),
            )

    def stream_tokens(
        self,
        request: InferenceRequest,
    ) -> Iterator[str]:
        """Stream raw tokens from local or cloud providers."""
        if request.is_local:
            timeout = request.watchdog_timeout or OLLAMA_CHAT_TIMEOUT
            stream = self.chat_streaming(
                timeout=timeout,
                model=request.model,
                messages=request.messages,
                options=request.options,
            )
            for chunk in stream:
                token = ""
                if isinstance(chunk, dict):
                    token = chunk.get("message", {}).get("content", "")
                elif hasattr(chunk, "message") and hasattr(chunk.message, "content"):
                    token = chunk.message.content
                if token:
                    yield token
        else:
            timeout = request.watchdog_timeout or OLLAMA_REQUEST_TIMEOUT
            response = self.cloud_chat(
                provider_cfg=request.provider_config,
                messages=request.messages,
                options=request.options,
                timeout=timeout,
            )
            if isinstance(response, dict):
                yield response.get("text", "")

    def stream_sentences(
        self,
        request: InferenceRequest,
        splitter: Optional[SentenceSplitter] = None,
    ) -> Iterator[str]:
        """Stream complete sentence clauses using SentenceSplitter."""
        sent_splitter = splitter or SentenceSplitter()
        for token in self.stream_tokens(request):
            for sentence in sent_splitter.feed(token):
                yield sentence
        final_sentence = sent_splitter.flush()
        if final_sentence:
            yield final_sentence
