"""R1 — REAL-env context-discovery check.

Runs only with ``OPENCOHOST_REALENV_TESTS=1`` and auto-skips when Ollama or the
required model is absent. Calls the REAL ``ollama.show`` (cheap metadata RPC, no
generation) and feeds the real ``ShowResponse`` into the production ctx-discovery
logic ``opencohost.core.context_budget.parse_model_ctx`` — the exact object type
the engine's ``_discover_model_ctx`` -> ``_fetch_show`` path passes in production.

Guards the ctx-discovery fix: ``parse_model_ctx`` must read the real ``modelinfo``
field and know the real per-arch keys (gemma4/qwen3/...), returning each model's
native context length instead of ``CTX_FALLBACK_DEFAULT``. This is the only check
that proves the static ``_ARCH_CTX_KEYS`` strings match what real Ollama emits —
the unit tests in test_context_budget.py feed synthetic keys, which is circular
for that claim.

Observed real values (flux_env, ollama 0.6.2):
  llama3 -> 8192 | gemma4:e2b -> 131072 | qwen3:1.7b -> 40960.
"""
from __future__ import annotations

import pytest

from tests.realenv._helpers import require_model, run_bounded

pytestmark = pytest.mark.realenv


def _real_ctx_from_show(resp) -> int:
    """Ground-truth native context length from a REAL ollama ``ShowResponse``.

    Reads the real ``modelinfo`` field (and the ``model_info`` alias / dict shape
    defensively), then returns the first ``*.context_length`` int. This is what
    the production parser is SUPPOSED to return.
    """
    model_info = None
    for attr in ("modelinfo", "model_info"):
        cand = resp.get(attr) if isinstance(resp, dict) else getattr(resp, attr, None)
        if isinstance(cand, dict):
            model_info = cand
            break
    assert model_info, f"real ollama.show exposed no model_info dict (resp={type(resp)})"
    for key, value in model_info.items():
        if key.endswith(".context_length") and isinstance(value, int) and not isinstance(value, bool):
            return value
    raise AssertionError(f"no *.context_length key in real model_info: {sorted(model_info)}")


_CASES = ["llama3", "gemma4:e2b", "qwen3:1.7b"]


@pytest.mark.parametrize("tag", _CASES)
def test_ctx_discovery_matches_real_model_ctx(tag):
    """REAL ctx-discovery must return the model's native ctx, not the fallback."""
    require_model(tag)
    import ollama

    from opencohost.config.settings import CTX_FALLBACK_DEFAULT
    from opencohost.core import context_budget

    resp = run_bounded(lambda: ollama.show(tag), seconds=20)
    real_ctx = _real_ctx_from_show(resp)

    got = context_budget.parse_model_ctx(resp, fallback=CTX_FALLBACK_DEFAULT)

    assert got == real_ctx, (
        f"{tag}: ctx-discovery returned {got}, but the real native ctx is "
        f"{real_ctx} (CTX_FALLBACK_DEFAULT={CTX_FALLBACK_DEFAULT})"
    )
