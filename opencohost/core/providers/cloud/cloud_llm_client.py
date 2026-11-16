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

import json
import re

import requests

from opencohost.core.providers.think_tags import (
    THINK_BLOCK_RE,
    THINK_CLOSE_TAG,
    THINK_OPEN_TAG,
    ThinkTagStreamSplitter,
    strip_think_blocks,
)

CHAT_COMPLETIONS_PATH = "/chat/completions"

# Options that pass through unchanged to an OpenAI-compatible chat body.
_PASSTHROUGH_OPTION_KEYS = ("temperature", "top_p", "presence_penalty", "frequency_penalty")

# Inline reasoning stripping (nim-cloud-reasoning-off, T3): a NIM reasoning
# model that still reasons emits it as a <think>...</think> block inside
# `content` rather than (or in addition to) `reasoning_content`. The helpers
# live in a shared, equally pure module so the local Ollama path uses the
# same split (live-session-fixes-20261005 L1); the private names stay.
_THINK_BLOCK_RE = THINK_BLOCK_RE
_THINK_OPEN_TAG = THINK_OPEN_TAG
_THINK_CLOSE_TAG = THINK_CLOSE_TAG
_strip_think_blocks = strip_think_blocks
_ThinkTagStreamSplitter = ThinkTagStreamSplitter


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
    - ``body_excerpt``: the first ~500 chars of the response body.
      ``classify_cloud_error`` below never reads it -- letting retry POLICY
      hinge on body content would mean guessing a provider-specific error
      shape, which is exactly what this taxonomy refuses to do. Only
      ``extract_error_code`` reads it, and only for a log line: it takes
      ``error.code``/``error.type`` and never ``error.message``. The raw
      excerpt itself still never reaches a log.
    """

    def __init__(self, message, *, status_code=None, headers=None, body_excerpt=None):
        super().__init__(message)
        self.status_code = status_code
        self.headers = headers or {}
        self.body_excerpt = body_excerpt


def map_options_to_openai(
    model: str,
    messages: list,
    options: dict,
    *,
    disable_reasoning: bool = True,
    reasoning_style: str | None = None,
    drop_params=None,
) -> dict:
    """Pure mapping: Ollama-shaped (model, messages, options) -> OpenAI-compatible request body.

    - ``num_predict`` -> ``max_tokens`` (renamed; omitted entirely when absent
      from ``options`` -- e.g. once the engine's reasoning-model retry pops
      it, this mapping must not reintroduce a cap).
    - ``num_ctx`` / ``repeat_penalty`` / ``keep_alive`` are Ollama-only knobs
      with no OpenAI-compatible equivalent -- dropped, never sent.
    - ``temperature`` / ``top_p`` / ``presence_penalty`` / ``frequency_penalty``
      pass through unchanged when present.
    - ``reasoning_style`` (from the provider preset) picks the wire form of
      "reasoning off": ``nim_kwargs`` (also the default for ``None``, i.e. a
      custom profile) sends ``chat_template_kwargs``; ``deepseek_thinking``
      sends ``thinking: {"type": "disabled"}``; ``openrouter_exclude`` sends
      ``reasoning: {"exclude": true}``; ``none`` sends nothing.
      ``drop_params`` names body keys to remove (e.g. sampling params a
      provider rejects).
    - ``think`` (nim-cloud-reasoning-off, T1) is never sent as a top-level
      body key. ``options["think"] is True`` -> ``chat_template_kwargs``
      turns reasoning ON. Otherwise, when ``disable_reasoning`` is on (NIM
      evidence 2026-09-25, ``CLOUD_DISABLE_REASONING``), it turns reasoning
      OFF. ``disable_reasoning`` off and ``think`` not True -> no
      ``chat_template_kwargs`` key at all, byte-identical to before T1.
    """
    options = options or {}
    body = {"model": model, "messages": messages}
    for key in _PASSTHROUGH_OPTION_KEYS:
        if key in options:
            body[key] = options[key]
    if "num_predict" in options:
        body["max_tokens"] = options["num_predict"]
    style = reasoning_style or "nim_kwargs"
    think_on = options.get("think") is True
    if style == "nim_kwargs":
        if think_on:
            body["chat_template_kwargs"] = {"enable_thinking": True, "thinking": True}
        elif disable_reasoning:
            body["chat_template_kwargs"] = {"enable_thinking": False, "thinking": False}
    elif disable_reasoning and not think_on:
        if style == "deepseek_thinking":
            body["thinking"] = {"type": "disabled"}
        elif style == "openrouter_exclude":
            body["reasoning"] = {"exclude": True}
    for name in drop_params or ():
        body.pop(name, None)
    return body


def preset_request_kwargs(preset) -> dict:
    """The per-preset request shaping every cloud chat call site forwards
    (``reasoning_style`` / ``extra_headers`` / ``drop_params``). One builder so
    the chat path, the setup speed test and the provider key test cannot
    drift apart (gemini-chat-parity-20261112 G1). ``None`` / empty (a custom,
    preset-less profile) keeps the plain call shape: ``{}``."""
    if not preset:
        return {}
    return {
        "reasoning_style": preset["reasoning_style"],
        "extra_headers": dict(preset["extra_headers"]),
        "drop_params": list(preset["drop_params"]),
    }


def _request_headers(api_key: str, extra_headers) -> dict:
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    for name, value in (extra_headers or {}).items():
        if name.lower() != "authorization":
            headers[name] = value
    return headers


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
    content, thinking = _strip_think_blocks(
        message.get("content") or "", message.get("reasoning_content") or ""
    )
    return {
        "message": {"content": content, "thinking": thinking},
        "usage": data.get("usage") or {},
    }


def send_chat_completion(
    base_url: str,
    api_key: str,
    model: str,
    messages: list,
    options: dict,
    timeout: float,
    *,
    disable_reasoning: bool = True,
    reasoning_style: str | None = None,
    extra_headers=None,
    drop_params=None,
) -> dict:
    """Non-streaming POST to ``{base_url}/chat/completions``. Returns an Ollama-shaped dict.

    Raises ``requests.exceptions.RequestException`` for network failures,
    timeouts, HTTP error statuses (via ``raise_for_status``), and malformed
    response bodies (``CloudLLMResponseError``, a ``RequestException``
    subclass) -- all are instances the engine's existing transport-error
    contract already classifies without any change. The api_key never
    appears in any raised message.

    ``disable_reasoning`` (nim-cloud-reasoning-off, T1) forwards to
    ``map_options_to_openai`` -- see that function for the exact kwargs
    behavior.
    """
    body = map_options_to_openai(
        model,
        messages,
        options,
        disable_reasoning=disable_reasoning,
        reasoning_style=reasoning_style,
        drop_params=drop_params,
    )
    headers = _request_headers(api_key, extra_headers)
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


def stream_chat_completion(
    base_url: str,
    api_key: str,
    model: str,
    messages: list,
    options: dict,
    timeout,
    *,
    disable_reasoning: bool = True,
    reasoning_style: str | None = None,
    extra_headers=None,
    drop_params=None,
):
    """Streaming POST to ``{base_url}/chat/completions`` (live_session_fixes_20260925 T8).

    Generator yielding Ollama-shaped chunks: ``{"message": {"content": ...,
    "thinking": ...}, "done": False}`` for each SSE data event that carries a
    content/reasoning_content delta, then exactly one final ``{"done": True,
    "usage": ...}`` marker chunk (no ``message`` key -- callers reconstructing
    a full response, e.g. ``GenerationOrchestrator.rebuild_stream_response``,
    fill it in from the accumulated deltas). Same security posture as
    ``send_chat_completion``: the api_key never appears in any raised message,
    and a non-2xx status raises the SAME sanitized ``CloudLLMResponseError``
    -- BEFORE any chunk is yielded, since Python does not execute a generator
    function's body (including the HTTP call) until the caller starts
    iterating it.

    ``timeout`` is forwarded verbatim to ``requests.post(..., stream=True,
    timeout=timeout)`` -- typically a ``(connect, read)`` tuple so the caller
    can bound the first-chunk/idle read differently from the connect phase.
    The response is always closed (``finally: resp.close()``), including when
    the caller closes this generator early (an abort mid-stream) or an
    exception propagates -- freeing the connection the same way an aborted
    non-streaming call already does.
    """
    body = map_options_to_openai(
        model,
        messages,
        options,
        disable_reasoning=disable_reasoning,
        reasoning_style=reasoning_style,
        drop_params=drop_params,
    )
    body["stream"] = True
    body["stream_options"] = {"include_usage": True}
    # A preset may reject stream_options too; "stream" itself is never
    # droppable because this client parses the SSE body.
    for name in drop_params or ():
        if name != "stream":
            body.pop(name, None)
    headers = _request_headers(api_key, extra_headers)
    url = f"{base_url.rstrip('/')}{CHAT_COMPLETIONS_PATH}"
    resp = requests.post(url, json=body, headers=headers, timeout=timeout, stream=True)
    try:
        http_error_detail = None
        http_error_status_code = None
        http_error_headers = {}
        http_error_body_excerpt = None
        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            # Same rationale as send_chat_completion: extracting only plain
            # values here, then exiting this except block (rather than
            # raising inside it), is what keeps the original HTTPError --
            # whose .response.request.headers carries the Authorization
            # bearer key -- out of __context__, not just __cause__.
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

        splitter = _ThinkTagStreamSplitter()
        usage: dict = {}
        # SSE is UTF-8 by spec, but NIM sends text/event-stream without a
        # charset and requests then decodes as ISO-8859-1 (mojibake). Decode
        # each byte line ourselves; newline splits never cut a UTF-8 sequence.
        for raw_line in resp.iter_lines():
            line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
            if not line or line.startswith(":"):
                continue  # blank line (SSE frame separator) or a comment/keepalive
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except ValueError:
                continue
            if not isinstance(chunk, dict):
                continue
            if chunk.get("usage"):
                usage = chunk["usage"]
            choices = chunk.get("choices")
            if not choices or not isinstance(choices[0], dict):
                continue
            delta = choices[0].get("delta") or {}
            content_piece, thinking_piece = splitter.feed(delta.get("content") or "")
            reasoning_piece = delta.get("reasoning_content") or ""
            if reasoning_piece:
                thinking_piece = f"{thinking_piece}{reasoning_piece}" if thinking_piece else reasoning_piece
            if content_piece or thinking_piece:
                yield {
                    "message": {"content": content_piece, "thinking": thinking_piece},
                    "done": False,
                }
        tail_content, tail_thinking = splitter.flush()
        if tail_content or tail_thinking:
            yield {
                "message": {"content": tail_content, "thinking": tail_thinking},
                "done": False,
            }
        yield {"done": True, "usage": usage}
    finally:
        resp.close()


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


def extract_error_code(exc: Exception) -> str | None:
    """The provider's own machine-readable error code -- for LOGGING only.

    Runtime gap found 2026-08-14: a real OpenAI 429 logged as bare
    ``cloud chat HTTP 429: Too Many Requests``, which is the SAME line for
    "you are sending requests too fast" (transient, waiting fixes it) and
    "you have no credits" (permanent, waiting never fixes it). The provider
    already says which in ``error.code``; ``body_excerpt`` was carrying it
    and nothing ever read it.

    Reads ONLY ``error.code`` / ``error.type`` from the OpenAI-shaped error
    envelope this client already targets (see ``map_options_to_openai``) --
    never ``error.message``, which some providers populate with echoed
    request content that must not reach a log.

    ``classify_cloud_error`` above still refuses to read the body at all,
    and that stays true: retry POLICY must not hinge on a provider-specific
    shape. Naming a failure in a log line may.

    Returns ``None`` for no body, a non-JSON body (including one the 500-char
    excerpt truncated mid-object), or any other envelope shape.
    """
    excerpt = getattr(exc, "body_excerpt", None)
    if not excerpt:
        return None
    try:
        error = json.loads(excerpt).get("error")
    except (ValueError, AttributeError):
        return None
    if not isinstance(error, dict):
        return None
    code = error.get("code") or error.get("type")
    return str(code)[:64] if code else None


# --- sanitized provider error text (setup-wizard-simple S1) -----------------
# The user (setup speed test) and the log need the provider's REASON for a
# failure (e.g. a Gemini 400), but never a credential. One shared helper strips
# anything key-like, collapses whitespace and bounds the length.
PROVIDER_MESSAGE_MAX_CHARS = 200
_REDACTED = "[redacted]"
_SECRET_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[^\s\"',;]+"),
    # ``Authorization: Basic ...`` / ``"authorization": "..."``: the scheme word
    # is optional so a bare credential after the header is dropped too.
    re.compile(
        r"(?i)\b(authorization)[\"']?\s*[:=]\s*[\"']?(?:(?:basic|bearer|token)\s+)?[^\s\"',;]+"
    ),
    # ``key=...``, ``"api_key": "..."``, ``x-api-key: ...``, ``'apiKey' = '...'``.
    re.compile(
        r"(?i)\b((?:x-)?(?:api[_-]?)?key|token|access_token|auth|secret|password)"
        r"[\"']?\s*[:=]\s*[\"']?[^&\s\"',;]+"
    ),
    # Google / OpenAI-style keys, including the short fragments a length cap
    # can leave behind at the end of a truncated body.
    re.compile(r"\bAIza[0-9A-Za-z_\-]*"),
    re.compile(r"\bsk-[0-9A-Za-z_\-]*"),
    re.compile(r"\bAQ\.[0-9A-Za-z_.\-]{12,}"),
    # Any other long opaque run (hex/base64/base64url/JWT-like) is a token.
    re.compile(r"(?<![0-9A-Za-z_\-+/])[0-9A-Za-z_\-+/]{32,}={0,2}"),
)


def sanitize_provider_text(
    text: object, api_key: str | None = None, limit: int = PROVIDER_MESSAGE_MAX_CHARS
) -> str:
    """Make provider error text safe to log and show: no key, one line, bounded."""
    if text is None:
        return ""
    out = str(text)
    if api_key and len(api_key) >= 4:
        out = out.replace(api_key, _REDACTED)
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            out = pattern.sub(lambda m: f"{m.group(1)}={_REDACTED}", out)
        else:
            out = pattern.sub(_REDACTED, out)
    out = " ".join(out.split())
    return out[: max(0, int(limit))]


_MESSAGE_FIELD_RE = re.compile(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)')
_STATUS_FIELD_RE = re.compile(r'"status"\s*:\s*"([A-Za-z0-9_.\-]{1,64})"')
# Body fields that echo the REQUEST (chat text). A truncated body carrying any
# of them is never shown raw, and an inline ``input=...`` echo is dropped.
_ECHO_FIELD_RE = re.compile(r'"(?:input|messages|content|contents|detail|prompt)"\s*:')
_INPUT_ECHO_RE = re.compile(
    r"""(?i)["']?\b(input(?:_value)?|content|contents|messages|prompt)["']?\s*[:=]\s*"""
    r"""(?:'[^']*'?|"[^"]*"?|\[[^\]]*\]?|\{[^}]*\}?|\S+)"""
)
_OMITTED = "[omitted]"
_STATUS_LABEL_RE = re.compile(r"[A-Za-z0-9_.\-]{1,64}")


def _message_from_json(data: object) -> str | None:
    if isinstance(data, list):
        data = next((d for d in data if isinstance(d, dict)), None)
    if not isinstance(data, dict):
        return None
    error = data.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        label = error.get("status") or error.get("code") or error.get("type")
        if message and label and str(label) not in str(message):
            return f"{message} [{label}]"
        return str(message or label) if (message or label) else None
    if isinstance(error, str) and error:
        return error
    if isinstance(data.get("message"), str) and data["message"]:
        return data["message"]
    detail = data.get("detail")
    if isinstance(detail, str) and detail:
        return detail
    if isinstance(detail, list):
        # Validation-error lists (pydantic/FastAPI shape): only ``msg``, never
        # the ``input`` they echo back.
        first = next((d for d in detail if isinstance(d, dict)), None)
        if first and isinstance(first.get("msg"), str) and first["msg"]:
            return first["msg"]
    return None


def extract_error_status(exc: Exception) -> str | None:
    """The provider's machine status label (Gemini ``error.status``), or ``None``.

    Enum-like only (``[A-Za-z0-9_.-]{1,64}``), so it is safe to log beside the
    HTTP status: it never carries request text.
    """
    excerpt = getattr(exc, "body_excerpt", None)
    if not excerpt or not isinstance(excerpt, str):
        return None
    status = None
    try:
        data = json.loads(excerpt)
        if isinstance(data, list):
            data = next((d for d in data if isinstance(d, dict)), None)
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(error, dict) and isinstance(error.get("status"), str):
            status = error["status"]
    except ValueError:
        match = _STATUS_FIELD_RE.search(excerpt)
        status = match.group(1) if match else None
    if status and _STATUS_LABEL_RE.fullmatch(status):
        return status
    return None


def extract_provider_message(exc: Exception, api_key: str | None = None) -> str | None:
    """Sanitized, bounded excerpt of the provider's error body, or ``None``.

    Prefers the JSON error ``message`` (+ ``status``/``code``); a body the
    500-char excerpt truncated mid-object still yields its ``message`` field.
    A JSON body without a message, or a truncated one carrying request-echo
    fields (``input``/``messages``/``content``/``detail``), yields ``None``:
    only a non-JSON body (e.g. an HTML error page) falls back to the raw
    excerpt. Inline ``input=...`` echoes are dropped. Always goes through
    ``sanitize_provider_text``. Shown to the user by the setup speed test
    (fixed prompt) only; the chat path never logs it (no raw chat in logs).
    """
    excerpt = getattr(exc, "body_excerpt", None)
    if not excerpt or not isinstance(excerpt, str):
        return None
    message = None
    try:
        message = _message_from_json(json.loads(excerpt))
        if not message:
            return None
    except ValueError:
        match = _MESSAGE_FIELD_RE.search(excerpt)
        if match:
            message = match.group(1).replace('\\"', '"')
        elif excerpt.lstrip()[:1] in ("{", "[") or _ECHO_FIELD_RE.search(excerpt):
            return None
    text = message if message else excerpt
    text = _INPUT_ECHO_RE.sub(lambda m: f"{m.group(1)}={_OMITTED}", text)
    cleaned = sanitize_provider_text(text, api_key)
    return cleaned or None


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
