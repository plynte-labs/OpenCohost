"""Immutable cloud sampling params: 400 `must be X` -> correct + retry once.

Evidence 2026-09-09 (VM, NVIDIA NIM): every turn to moonshotai/kimi-k3 burned
instantly — ``400: Validation: `top_p` is immutable for this model and must be
0.95, got 0.9`` — because the engine always sends its own ``LLM_TOP_P`` (0.9).
"""

from __future__ import annotations

import pytest

from opencohost.core.engine.llm_inference_service import LLMInferenceService
from opencohost.core.providers.cloud.cloud_llm_client import CloudLLMResponseError

CFG = {
    "active_provider": "nvidia_nim",
    "profiles": {
        "nvidia_nim": {
            "base_url": "https://integrate.api.nvidia.com/v1",
            "model": "moonshotai/kimi-k3",
        }
    },
}

TOP_P_400 = (
    '{"message":"Validation: `top_p` is immutable for this model '
    'and must be 0.95, got 0.9","type":"Bad Request","code":400}'
)


def _err(status, excerpt):
    return CloudLLMResponseError(
        f"cloud chat HTTP {status}", status_code=status, headers={}, body_excerpt=excerpt
    )


class FakeCloud:
    """Fails like NIM until top_p arrives as 0.95."""

    CloudLLMResponseError = CloudLLMResponseError

    def __init__(self):
        self.calls: list[dict] = []

    def send_chat_completion(self, **kwargs):
        self.calls.append(kwargs)
        if (kwargs.get("options") or {}).get("top_p", 0.9) != 0.95:
            raise _err(400, TOP_P_400)
        return {"message": {"content": "pong", "thinking": ""}}


def _service(fake):
    return LLMInferenceService(
        cloud_client_module=fake, api_key_resolver=lambda _: "k"
    )


def test_retries_400_with_corrected_top_p():
    fake = FakeCloud()
    out = _service(fake).cloud_chat(
        provider_cfg=CFG,
        messages=[{"role": "user", "content": "ping"}],
        options={"temperature": 0.7, "top_p": 0.9},
        timeout=5.0,
    )
    assert out["message"]["content"] == "pong"
    assert len(fake.calls) == 2
    assert fake.calls[0]["options"]["top_p"] == 0.9
    assert fake.calls[1]["options"]["top_p"] == 0.95
    # Untouched params survive the correction.
    assert fake.calls[1]["options"]["temperature"] == 0.7


def test_cache_skips_400_on_next_turn():
    fake = FakeCloud()
    service = _service(fake)
    kwargs = dict(
        provider_cfg=CFG,
        messages=[{"role": "user", "content": "ping"}],
        options={"temperature": 0.7, "top_p": 0.9},
        timeout=5.0,
    )
    service.cloud_chat(**kwargs)
    service.cloud_chat(**kwargs)
    # Second turn goes out already fixed: 2 + 1 calls, no second 400.
    assert len(fake.calls) == 3
    assert fake.calls[2]["options"]["top_p"] == 0.95


def test_non_400_reraises_without_retry():
    class Deny401(FakeCloud):
        def send_chat_completion(self, **kwargs):
            self.calls.append(kwargs)
            raise _err(401, '{"message":"Unauthorized"}')

    fake = Deny401()
    with pytest.raises(CloudLLMResponseError):
        _service(fake).cloud_chat(
            provider_cfg=CFG, messages=[], options={"top_p": 0.9}, timeout=5.0
        )
    assert len(fake.calls) == 1


def test_400_without_pattern_reraises():
    class Vague400(FakeCloud):
        def send_chat_completion(self, **kwargs):
            self.calls.append(kwargs)
            raise _err(400, '{"message":"Bad Request"}')

    fake = Vague400()
    with pytest.raises(CloudLLMResponseError):
        _service(fake).cloud_chat(
            provider_cfg=CFG, messages=[], options={"top_p": 0.9}, timeout=5.0
        )
    assert len(fake.calls) == 1


def test_max_tokens_requirement_maps_to_num_predict():
    seen: list[dict] = []

    class CapMax(FakeCloud):
        def send_chat_completion(self, **kwargs):
            seen.append(kwargs)
            if (kwargs.get("options") or {}).get("num_predict", 16384) != 4096:
                raise _err(
                    400,
                    '{"message":"Validation: `max_tokens` is immutable '
                    'and must be 4096, got 16384"}',
                )
            return {"message": {"content": "ok", "thinking": ""}}

    out = _service(CapMax()).cloud_chat(
        provider_cfg=CFG, messages=[], options={"num_predict": 16384}, timeout=5.0
    )
    assert out["message"]["content"] == "ok"
    assert seen[1]["options"]["num_predict"] == 4096


def test_already_correct_value_does_not_loop():
    class Always400(FakeCloud):
        def send_chat_completion(self, **kwargs):
            self.calls.append(kwargs)
            raise _err(400, TOP_P_400)

    fake = Always400()
    with pytest.raises(CloudLLMResponseError):
        _service(fake).cloud_chat(
            provider_cfg=CFG, messages=[], options={"top_p": 0.95}, timeout=5.0
        )
    assert len(fake.calls) == 1
