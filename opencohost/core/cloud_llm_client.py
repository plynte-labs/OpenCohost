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
    """Malformed or unexpected 2xx response body from the cloud provider.

    Subclasses ``requests.exceptions.RequestException`` (rather than a bare
    ``Exception``) so the engine's existing transport-error classifier
    (``LlmEngine._is_ollama_transport_error``, which already checks
    ``isinstance(exc, requests.exceptions.RequestException)``) catches it
    with zero changes required on the engine side.
    """


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
    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        # Extract only the plain status/reason strings here; exiting this
        # except block (rather than raising inside it) is what keeps the
        # original HTTPError -- whose .response.request.headers carries the
        # Authorization bearer key -- out of __context__, not just __cause__.
        http_error_detail = f"cloud chat HTTP {exc.response.status_code}: {exc.response.reason}"
    if http_error_detail is not None:
        raise CloudLLMResponseError(http_error_detail)
    try:
        data = resp.json()
    except ValueError:
        raise CloudLLMResponseError("cloud LLM response was not valid JSON") from None
    if not isinstance(data, dict):
        raise CloudLLMResponseError("cloud LLM response body was not a JSON object")
    return _adapt_response(data)
