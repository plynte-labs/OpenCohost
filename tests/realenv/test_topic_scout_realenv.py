"""Real-env opt-in test for Topic Scout (topic_scout_llm_20260629), AC6.

Gated by ``tests/realenv/conftest.py`` (``OPENCOHOST_REALENV_TESTS=1``); placing
the file under ``tests/realenv/`` is sufficient — no ``pytestmark`` needed. Also
auto-skips without Ollama / the target model.

Exercises the REAL ``scout_digest`` end to end against a real NON-thinking model:
a real dedicated short-timeout client, a real capped generation, and the real
parse / preamble / echo / sanitize pipeline. A mock would only re-assert the
hand-built shape, so a real generation is the right tool here.

Target model: ``gemma4:e2b`` — low latency and, per ``ollama.show``, NOT
thinking-capable, so the scout's capability-based value gate
(``_check_capabilities_reasoning``, NOT the broad name heuristic) lets it
through. This is exactly track decision #1.
"""

from __future__ import annotations

import queue

from opencohost.core import llm_engine
from opencohost.core.llm_engine import MotorVocalIA
from tests.realenv._helpers import require_model, run_bounded

MODEL = "gemma4:e2b"

SEED_HOST = "hablamos de LLMs y modelos de lenguaje"
SEED_KIRA = "si, los modelos de lenguaje están en todos lados"


def _build_real_scout_motor():
    import ollama

    motor = MotorVocalIA(queue.Queue(), lambda e: None)
    motor.ollama = ollama
    motor._ollama_scout_client = motor._create_ollama_scout_client(ollama)
    motor._loaded_model = MODEL
    motor.current_model = MODEL
    motor._pending_model_switch = None
    motor._awaiting_first_success_after_switch = False
    motor.historial.append({"role": "user", "content": SEED_HOST})
    motor.historial.append({"role": "assistant", "content": SEED_KIRA})
    return motor


def test_scout_digest_real_generation_returns_adjacent_titles(monkeypatch):
    require_model(MODEL)
    monkeypatch.setattr(llm_engine, "SCOUT_ENABLED", True)

    motor = _build_real_scout_motor()

    # Warm the model so the resident-model gate passes and the generation is fast.
    run_bounded(
        lambda: motor.ollama.chat(
            model=MODEL,
            messages=[{"role": "user", "content": "hola"}],
            options={"num_predict": 1},
        ),
        seconds=60,
    )

    titles = run_bounded(motor.scout_digest, seconds=30)

    assert isinstance(titles, list)
    assert titles, "real scout produced no adjacent titles"
    assert len(titles) <= 3

    seed_block_cf = f"host: {SEED_HOST}\nkira: {SEED_KIRA}".casefold()
    for item in titles:
        assert item["source"] == "scout"
        assert item["confidence"] == "LOW"
        title = item["title"]
        assert title.strip(), "empty scout title"
        # Non-echo: a surviving title is never a case-folded substring of the seed.
        assert title.casefold() not in seed_block_cf
