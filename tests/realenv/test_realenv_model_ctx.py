"""R1 — REAL-env context-discovery check (documents a production ctx bug).

Runs only with ``OPENCOHOST_REALENV_TESTS=1`` and auto-skips when Ollama or the
required model is absent. Calls the REAL ``ollama.show`` (cheap metadata RPC, no
generation) and feeds the real ``ShowResponse`` into the production ctx-discovery
logic ``opencohost.core.context_budget.parse_model_ctx`` — the exact object type
the engine's ``_discover_model_ctx`` -> ``_fetch_show`` path passes in production.

EXPECTED RED — DOCUMENTED, NOT FLAKY.
ctx-discovery currently returns ``CTX_FALLBACK_DEFAULT`` (4096) for EVERY real
model instead of the model's native context length. Two stacked production bugs
cause this, so every parametrized case is marked ``xfail(strict=True)``: the
opt-in suite stays green while the bug is recorded, and a real fix flips
XFAIL -> XPASS (strict) which FAILS the run and forces the xfail to be removed.

FLAG-SDD (production change, do NOT fix here):
  1. ``parse_model_ctx`` reads attribute ``model_info`` from the ShowResponse,
     but ollama exposes the dict under the field ``modelinfo`` (``model_info`` is
     only a pydantic *alias*, not reachable via ``getattr``). So
     ``getattr(resp, "model_info", None)`` is ``None`` -> the arch-key loop sees
     nothing -> fallback 4096. Affects ALL architectures (llama/gemma/qwen3/...).
  2. Even after (1) is fixed, ``_ARCH_CTX_KEYS`` lists ``gemma`` and ``qwen2``,
     but the real models expose ``gemma4.context_length`` and
     ``qwen3.context_length``; those keys are missing, so gemma4 and qwen3 stay
     on the fallback. (``llama.context_length`` IS listed, so llama3 would go
     green once bug 1 is fixed.)

Observed real values (flux_env, ollama 0.6.2):
  llama3 -> 8192 | gemma4:e2b -> 131072 | qwen3:1.7b -> 40960 ; parse -> 4096 all.
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


# Shared root cause (bug 1) + per-model arch-key gap (bug 2). strict=True so a
# real fix turns XPASS into a failure and forces these marks to be deleted.
_ATTR_BUG = (
    "ctx-discovery returns CTX_FALLBACK_DEFAULT for real models: parse_model_ctx "
    "reads attr 'model_info' but ollama exposes 'modelinfo' (alias-only). "
    "FLAG-SDD -- see module docstring."
)

_CASES = [
    pytest.param(
        "llama3",
        id="llama3",
        marks=pytest.mark.xfail(strict=True, reason=_ATTR_BUG),
    ),
    pytest.param(
        "gemma4:e2b",
        id="gemma4-e2b",
        marks=pytest.mark.xfail(
            strict=True,
            reason="gemma4.context_length missing from _ARCH_CTX_KEYS. " + _ATTR_BUG,
        ),
    ),
    pytest.param(
        "qwen3:1.7b",
        id="qwen3-1.7b",
        marks=pytest.mark.xfail(
            strict=True,
            reason="qwen3.context_length missing from _ARCH_CTX_KEYS. " + _ATTR_BUG,
        ),
    ),
]


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
