"""OpenAI-compatible cloud LLM client (multi_provider_llm_20260723 Phase 2).

Pure, engine-agnostic module -- zero imports from ``opencohost.core.llm_engine``,
importable standalone. Provides the option mapping used by both chat call
sites (design 'Option Mapping') and a thin non-streaming HTTP client whose
response shape mirrors the Ollama response the engine already parses
(``message.content`` / ``message.thinking`` / ``usage``), so wiring this in at
``llm_engine.py`` (Phase 3) needs no new parsing branch.

Security: the api_key is only ever placed in the Authorization request
header. It is never interpolated into an exception message, a repr, or any
other string this module constructs. No exception raised by this module
carries the response/request object -- or the key -- anywhere in its object
graph (``raise_for_status()``'s ``HTTPError`` is caught and re-raised as a
response/request-free ``CloudLLMResponseError`` via ``raise ... from None``).
"""

import requests

CHAT_COMPLETIONS_PATH = "/chat/completions"

# Options that pass through unchanged to an OpenAI-compatible chat body.
_PASSTHROUGH_OPTION_KEYS = ("temperature", "top_p", "presence_penalty", "frequency_penalty")


class CloudLLMResponseError(requests.exceptions.RequestException):
    """Malformed or unexpected 2xx response body from the cloud provider --
    or, since F2 (runtime_findings_batch_20260731 unit 1.1), any non-2xx HTTP
    status the provider returned.

    Subclasses ``requests.exceptions.RequestException`` (rather than a bare
    ``Exception``) so the engine's existing transport-error classifier
    (``LlmEngine._is_ollama_transport_error``, which already checks
    ``isinstance(exc, requests.exceptions.RequestException)``) catches it
    with zero changes required on the engine side.

    Carries a bounded, classification-only surface -- never the ``requests``
    response/request object itself, which is how the Authorization bearer key
    would leak into the exception's object graph:

    - ``status_code``: the HTTP status, or ``None`` for a malformed-2xx body.
    - ``headers``: a bounded, lower-cased subset (``retry-after`` and any
      ``x-ratelimit-*``) -- never the full header dict.
    - ``body_excerpt``: the first ~500 chars of the response body. Stored for
      future use; ``classify_cloud_error`` below never reads it -- parsing
      body content would mean guessing a provider-specific error shape,
      which is exactly what this taxonomy refuses to do.
    """

    def __init__(self, message, *, status_code=None, headers=None, body_excerpt=None):
        super().__init__(message)
        self.status_code = status_code
        self.headers = headers or {}
        self.body_excerpt = body_excerpt


def map_options_to_openai(model: str, messages: list, options: dict) -> dict:
    """Pure mapping: Ollama-shaped (model, messages, options) -> OpenAI-compatible request body.

    - ``num_predict`` -> ``max_tokens`` (renamed; omitted entirely when absent
      from ``options`` -- e.g. once the engine's reasoning-model retry pops
      it, this mapping must not reintroduce a cap).
    - ``num_ctx`` / ``repeat_penalty`` / ``keep_alive`` are Ollama-only knobs
      with no OpenAI-compatible equivalent -- dropped, never sent.
    - ``temperature`` / ``top_p`` / ``presence_penalty`` / ``frequency_penalty``
      pass through unchanged when present.
    """
    options = options or {}
    body = {"model": model, "messages": messages}
    for key in _PASSTHROUGH_OPTION_KEYS:
        if key in options:
            body[key] = options[key]
    if "num_predict" in options:
        body["max_tokens"] = options["num_predict"]
    return body


def _extract_relevant_headers(headers) -> dict:
    """Bounded, case-insensitive subset of response headers worth keeping on
    the exception for classification (F2) -- never the full header dict,
    which could carry tracing/request-id values with no reason to persist."""
    out = {}
    for key, value in (headers or {}).items():
        lower = str(key).lower()
        if lower == "retry-after" or lower.startswith("x-ratelimit-"):
            out[lower] = value
    return out


def _adapt_response(data: dict) -> dict:
    """Map an OpenAI-compatible chat-completion body to the Ollama response shape."""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise CloudLLMResponseError("cloud LLM response missing 'choices[0]'")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise CloudLLMResponseError("cloud LLM response missing 'choices[0].message'")
    return {
        "message": {
            "content": message.get("content") or "",
            "thinking": message.get("reasoning_content") or "",
        },
        "usage": data.get("usage") or {},
    }


def send_chat_completion(
    base_url: str,
    api_key: str,
    model: str,
    messages: list,
    options: dict,
    timeout: float,
) -> dict:
    """Non-streaming POST to ``{base_url}/chat/completions``. Returns an Ollama-shaped dict.

    Raises ``requests.exceptions.RequestException`` for network failures,
    timeouts, HTTP error statuses (via ``raise_for_status``), and malformed
    response bodies (``CloudLLMResponseError``, a ``RequestException``
    subclass) -- all are instances the engine's existing transport-error
    contract already classifies without any change. The api_key never
    appears in any raised message.
    """
    body = map_options_to_openai(model, messages, options)
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    url = f"{base_url.rstrip('/')}{CHAT_COMPLETIONS_PATH}"
    resp = requests.post(url, json=body, headers=headers, timeout=timeout)
    http_error_detail = None
    http_error_status_code = None
    http_error_headers = {}
    http_error_body_excerpt = None
    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        # Extract only plain values here (status code, a bounded header
        # subset, a bounded body excerpt); exiting this except block (rather
        # than raising inside it) is what keeps the original HTTPError --
        # whose .response.request.headers carries the Authorization bearer
        # key -- out of __context__, not just __cause__.
        http_error_detail = f"cloud chat HTTP {exc.response.status_code}: {exc.response.reason}"
        http_error_status_code = exc.response.status_code
        http_error_headers = _extract_relevant_headers(exc.response.headers)
        http_error_body_excerpt = (exc.response.text or "")[:500]
    if http_error_detail is not None:
        raise CloudLLMResponseError(
            http_error_detail,
            status_code=http_error_status_code,
            headers=http_error_headers,
            body_excerpt=http_error_body_excerpt,
        )
    try:
        data = resp.json()
    except ValueError:
        raise CloudLLMResponseError(
            "cloud LLM response was not valid JSON",
            status_code=resp.status_code,
            headers=_extract_relevant_headers(resp.headers),
            body_excerpt=(resp.text or "")[:500],
        ) from None
    if not isinstance(data, dict):
        raise CloudLLMResponseError(
            "cloud LLM response body was not a JSON object",
            status_code=resp.status_code,
            headers=_extract_relevant_headers(resp.headers),
        )
    return _adapt_response(data)


# ── F2 taxonomy (runtime_findings_batch_20260731 Batch 1 unit 1.1) ──────────
#
# Provider-agnostic on purpose: only NVIDIA NIM has ever been exercised, so
# this reads status_code + a bounded header subset, never a per-provider
# error-code or body table. `AMBIGUOUS_429` is the honest completion of that
# rule, not a gap: when neither headers nor body reliably tell a temporary
# rate limit apart from an exhausted quota, the classifier says so instead of
# inventing certainty. `quota_exhausted` is deliberately NOT implemented here
# (owner ruling, future proposal) -- an exhausted quota lands in
# `AMBIGUOUS_429` and gets conservative treatment, which is correct.

CLOUD_ERROR_BAD_KEY = "bad_key"
CLOUD_ERROR_RATE_LIMITED = "rate_limited"
CLOUD_ERROR_AMBIGUOUS_429 = "ambiguous_429"
CLOUD_ERROR_TRANSIENT = "transient"


def _has_retry_timing_info(headers: dict) -> bool:
    """True when *headers* say WHEN to retry, not just THAT a limit exists.

    ``x-ratelimit-remaining``/``-limit`` say how many calls are left, not
    when the window resets, so only ``retry-after`` or an
    ``x-ratelimit-*reset*`` key counts as usable retry-timing info.
    """
    headers = headers or {}
    if "retry-after" in headers:
        return True
    return any(key.startswith("x-ratelimit-") and "reset" in key for key in headers)


def classify_cloud_error(exc: Exception) -> str:
    """Classify a cloud transport failure for retry/fallback policy (F2).

    Reads only ``status_code``/``headers`` already carried on a
    ``CloudLLMResponseError`` (see its docstring) -- never response body
    content, which would mean guessing a provider-specific shape. Anything
    without a status_code (``ConnectionError``, ``Timeout``, ``socket.timeout``,
    a malformed-2xx-body ``CloudLLMResponseError``) is ``transient``, along
    with every 5xx.
    """
    status_code = getattr(exc, "status_code", None)
    if status_code in (401, 403):
        return CLOUD_ERROR_BAD_KEY
    if status_code == 429:
        if _has_retry_timing_info(getattr(exc, "headers", None) or {}):
            return CLOUD_ERROR_RATE_LIMITED
        return CLOUD_ERROR_AMBIGUOUS_429
    return CLOUD_ERROR_TRANSIENT


def parse_retry_after_seconds(headers: dict) -> int | None:
    """Delta-seconds ``Retry-After`` only.

    ponytail: RFC 7231 also allows an HTTP-date ``Retry-After``; every
    provider exercised so far (NVIDIA NIM) sends delta-seconds, so date
    parsing is skipped -- add ``email.utils.parsedate_to_datetime`` if a
    provider that sends a date ever shows up. ``x-ratelimit-*reset*`` values
    are provider-specific (unix epoch vs. seconds-remaining) and are
    deliberately not parsed here -- that is exactly the guessed-shape table
    this taxonomy refuses to build.
    """
    value = (headers or {}).get("retry-after")
    if value is None:
        return None
    try:
        parsed = int(str(value).strip())
    except ValueError:
        return None
    # RFC 7231 delta-seconds is 1*DIGIT -- a negative value is a malformed
    # header, and consumers feed this straight into time.sleep(), so treat
    # it like the unparseable cases above.
    return parsed if parsed >= 0 else None
