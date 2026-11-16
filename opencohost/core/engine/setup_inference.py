"""Setup-assistant inference speed test (setup-assistant T2).

One cold run (its ``load_ms`` is reported separately) plus ``runs`` warm runs
against the ACTIVE engine: the local Ollama model or the active cloud profile.
The prompt is fixed (no user content), thinking is off and the output is capped.
Metrics are the median of the warm runs; anything the backend did not report is
``None``, never an invented 0.

The HTTP layer is injectable (``local_stream`` / ``cloud_stream`` / ``clock`` /
``api_key_loader``) so tests never touch the network. Nothing here mutates engine
config, conversation history or memory, and neither the prompt nor an API key is
logged.
"""
from __future__ import annotations

import logging
import statistics
import threading
import time
from typing import Any, Callable, Iterator, Optional

from opencohost.config.settings import (
    CLOUD_CHAT_TIMEOUT,
    CLOUD_STREAM_FIRST_CHUNK_TIMEOUT_SECONDS,
    LLM_KEEP_ALIVE,
    OLLAMA_CHAT_TIMEOUT,
)

logger = logging.getLogger("OpenCohost")

TEST_PROMPT = "Say hello in one short sentence."
MAX_OUTPUT_TOKENS = 48
MIN_RUNS, MAX_RUNS, DEFAULT_RUNS = 1, 5, 3
SLOW_FIRST_TOKEN_MS = 2500
SLOW_TOKENS_PER_SECOND = 8

# Whole-request budget, reusing the engine's own chat timeouts.
LOCAL_BUDGET_S = float(OLLAMA_CHAT_TIMEOUT)
CLOUD_BUDGET_S = float(CLOUD_CHAT_TIMEOUT)

_METRIC_KEYS = ("load_ms", "first_token_ms", "tokens_per_second")
_default_clock = time.perf_counter
_test_lock = threading.Lock()


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _local_stream(model: str, messages: list, options: dict, timeout: float) -> Iterator[Any]:
    """Real transport: streaming ``ollama`` chat with the engine's think fallback."""
    import ollama

    from opencohost.core.engine.llm_inference_service import _iter_local_stream

    client = ollama.Client(timeout=timeout)
    yield from _iter_local_stream(
        client,
        {"model": model, "messages": messages, "options": options, "keep_alive": LLM_KEEP_ALIVE},
    )


def _cloud_stream(
    base_url: str,
    api_key: str,
    model: str,
    messages: list,
    options: dict,
    timeout: Any,
    **preset_kw: Any,
) -> Iterator[Any]:
    """``preset_kw`` (reasoning_style / drop_params / extra_headers) is the active
    preset's request shaping, exactly as the chat path forwards it."""
    from opencohost.core.providers.cloud.cloud_llm_client import stream_chat_completion

    return stream_chat_completion(base_url, api_key, model, messages, options, timeout, **preset_kw)


def _preset_kwargs(profile: dict) -> dict:
    from opencohost.config.settings import LLM_PROVIDER_PRESETS
    from opencohost.core.providers.cloud.cloud_llm_client import preset_request_kwargs

    # A custom (preset-less) profile keeps the plain call shape ({}).
    return preset_request_kwargs(LLM_PROVIDER_PRESETS.get(str(profile.get("preset") or "")))


def _load_api_key(profile_id: str) -> str:
    try:
        from opencohost.api import deps
        from opencohost.stream_admin.oauth_store import OAuthStore

        token = OAuthStore(deps.llm_keys_file()).load(profile_id)
        return str(token.get("api_key") or "").strip() if isinstance(token, dict) else ""
    except Exception:
        return ""


def _classify_local(exc: BaseException) -> str:
    if getattr(exc, "status_code", None) == 404:
        return "model_missing"
    if isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower():
        return "timeout"
    # Connection refused / reset and anything else the transport raises: the
    # enum has no generic code, and a dead daemon is by far the common cause.
    return "ollama_unreachable"


def _classify_cloud(exc: BaseException) -> str:
    import requests

    if isinstance(exc, (requests.exceptions.Timeout, TimeoutError)):
        return "timeout"
    status = getattr(exc, "status_code", None)
    if status in (401, 403):
        return "cloud_auth"
    if status in (402, 429):
        # 402 = out of credits / billing: same user fix as a rate limit.
        return "cloud_rate_limit"
    if status in (400, 404, 422):
        return "cloud_bad_request"
    return "cloud_error"


def _cloud_failure_detail(exc: BaseException, api_key: str) -> tuple:
    """(http_status, sanitized provider message) -- both None when not an HTTP error."""
    from opencohost.core.providers.cloud.cloud_llm_client import extract_provider_message

    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        return None, None
    return status, extract_provider_message(exc, api_key)


def _median(values: list) -> Optional[float]:
    values = [v for v in values if v is not None]
    return float(statistics.median(values)) if values else None


def build_result(
    status: str,
    provider: str,
    model: Optional[str],
    runs: list,
    error_code: Optional[str],
    cold_load_ms: Optional[float],
    http_status: Optional[int] = None,
    provider_message: Optional[str] = None,
) -> dict:
    ok_runs = [r for r in runs if r["ok"]]
    return {
        "status": status,
        "provider": provider,
        "model": model,
        "metrics": {key: _median([r[key] for r in ok_runs]) for key in _METRIC_KEYS},
        "runs": runs,
        "error_code": error_code,
        "cold_load_ms": cold_load_ms,
        "http_status": http_status,
        "provider_message": provider_message,
    }


def _one_run(open_stream: Callable[[float], Iterator[Any]], timeout: float, clock, *, local: bool) -> dict:
    """Time one streamed generation. Raises whatever the transport raises."""
    started = clock()
    first = None
    last_chunk = None
    for chunk in open_stream(timeout):
        message = _get(chunk, "message") or {}
        if first is None and (_get(message, "content") or _get(message, "thinking")):
            first = clock()
        if _get(chunk, "done"):
            last_chunk = chunk
    finished = clock()

    load_ms = tps = None
    if last_chunk is not None and local:
        load_ns = _get(last_chunk, "load_duration")
        eval_count = _get(last_chunk, "eval_count")
        eval_ns = _get(last_chunk, "eval_duration")
        if load_ns is not None:
            load_ms = load_ns / 1e6
        if eval_count and eval_ns:
            tps = eval_count / (eval_ns / 1e9)
    elif last_chunk is not None and first is not None:
        usage = _get(last_chunk, "usage") or {}
        tokens = usage.get("completion_tokens") if isinstance(usage, dict) else None
        elapsed = finished - first
        if tokens and elapsed > 0:
            tps = tokens / elapsed
    return {
        "load_ms": load_ms,
        "first_token_ms": None if first is None else (first - started) * 1000.0,
        "tokens_per_second": tps,
        "ok": first is not None,
    }


def _verdict(metrics: dict) -> str:
    ftl, tps = metrics["first_token_ms"], metrics["tokens_per_second"]
    if (ftl is not None and ftl > SLOW_FIRST_TOKEN_MS) or (
        tps is not None and tps < SLOW_TOKENS_PER_SECOND
    ):
        return "slow"
    return "passed"


def _resolve_engine(host: Any, local_stream, cloud_stream, api_key_loader):
    """(provider, model, opener, budget_s, early_error_code, api_key) for the active engine."""
    motor = getattr(host, "motor", None)
    cfg = getattr(motor, "_provider_config", None)
    if not cfg:
        try:
            from opencohost.config.llm_provider import load_provider_config

            cfg = load_provider_config()
        except Exception:
            cfg = {}
    active = str((cfg or {}).get("active_provider") or "local")
    options = {"num_predict": MAX_OUTPUT_TOKENS, "temperature": 0, "think": False}
    messages = [{"role": "user", "content": TEST_PROMPT}]
    if active == "local":
        model = getattr(motor, "current_model", None)

        def opener(timeout):
            return local_stream(model, messages, dict(options), timeout)

        return "local", model, opener, LOCAL_BUDGET_S, None, ""

    profile = ((cfg or {}).get("profiles") or {}).get(active) or {}
    model = profile.get("model") or None
    base_url = str(profile.get("base_url") or "")
    api_key = api_key_loader(active)
    if not api_key:
        return "cloud", model, None, CLOUD_BUDGET_S, "cloud_auth", ""
    preset_kw = _preset_kwargs(profile)

    def cloud_opener(timeout):
        connect = min(timeout, float(CLOUD_STREAM_FIRST_CHUNK_TIMEOUT_SECONDS))
        return cloud_stream(
            base_url, api_key, model, messages, dict(options), (connect, timeout), **preset_kw
        )

    return "cloud", model, cloud_opener, CLOUD_BUDGET_S, None, api_key


def run_inference_test(
    host: Any,
    runs: int = DEFAULT_RUNS,
    *,
    local_stream: Optional[Callable] = None,
    cloud_stream: Optional[Callable] = None,
    api_key_loader: Optional[Callable[[str], str]] = None,
    clock: Optional[Callable[[], float]] = None,
) -> dict:
    if not MIN_RUNS <= int(runs) <= MAX_RUNS:
        raise ValueError(f"runs must be {MIN_RUNS}..{MAX_RUNS}")
    if not _test_lock.acquire(blocking=False):  # a second test already in flight
        return _finish(build_result("failed", "local", None, [], "busy", None))
    try:
        result = _run(
            host,
            int(runs),
            local_stream or _local_stream,
            cloud_stream or _cloud_stream,
            api_key_loader or _load_api_key,
            clock or _default_clock,
        )
    finally:
        _test_lock.release()
    return _finish(result)


def _run(host: Any, runs: int, local_stream, cloud_stream, api_key_loader, clock) -> dict:
    provider, model, opener, budget, early, api_key = _resolve_engine(
        host, local_stream, cloud_stream, api_key_loader
    )
    classify = _classify_local if provider == "local" else _classify_cloud
    if early:
        return build_result("failed", provider, model, [], early, None)
    if getattr(getattr(host, "motor", None), "llm_generating", False):
        return build_result("failed", provider, model, [], "busy", None)

    deadline = clock() + budget
    results: list = []
    cold_load_ms = None
    last_error = None
    http_status = provider_message = None
    for index in range(runs + 1):  # index 0 is the cold run
        remaining = deadline - clock()
        if remaining <= 0:
            last_error = last_error or "timeout"
            break
        try:
            result = _one_run(opener, remaining, clock, local=provider == "local")
        except Exception as exc:
            last_error = classify(exc)
            if provider == "cloud":
                http_status, provider_message = _cloud_failure_detail(exc, api_key)
            if index == 0:
                return build_result(
                    "failed", provider, model, [], last_error, None, http_status, provider_message
                )
            results.append({**dict.fromkeys(_METRIC_KEYS), "ok": False})
            continue
        if index == 0:
            cold_load_ms = result["load_ms"]
        else:
            results.append(result)

    if not any(r["ok"] for r in results):
        return build_result(
            "failed", provider, model, results, last_error or "timeout", cold_load_ms,
            http_status, provider_message,
        )
    out = build_result("passed", provider, model, results, None, cold_load_ms)
    out["status"] = _verdict(out["metrics"])
    return out


def _failure_suffix(result: dict) -> str:
    """Error code, HTTP status and the already-sanitized provider message (never a key)."""
    if not result.get("error_code"):
        return ""
    suffix = f" error_code={result['error_code']}"
    if result.get("http_status") is not None:
        suffix += f" http_status={result['http_status']}"
    if result.get("provider_message"):
        suffix += f" provider_message={result['provider_message']!r}"
    return suffix


def _finish(result: dict) -> dict:
    metrics = result["metrics"]
    logger.info(
        "[SETUP] inference_test status=%s provider=%s first_token_ms=%s tps=%s%s",
        result["status"],
        result["provider"],
        None if metrics["first_token_ms"] is None else round(metrics["first_token_ms"]),
        None if metrics["tokens_per_second"] is None else round(metrics["tokens_per_second"], 1),
        _failure_suffix(result),
    )
    return result
