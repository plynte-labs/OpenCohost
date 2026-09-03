from __future__ import annotations

import time
from pathlib import Path
import pytest

from opencohost.core.memory_v5_shadow.semantic_worker import (
    SemanticWorkerService,
)


def test_semantic_worker_service_embed_and_lifecycle():
    # Use a lightweight mock/dummy mode or actual worker in test
    worker = SemanticWorkerService(use_dummy=True)
    worker.start()
    assert worker.is_alive()

    # Query embedding
    vec = worker.embed_query("test query")
    assert vec is not None
    assert len(vec) == 384

    # Batch embedding
    batch = worker.embed_batch(["text 1", "text 2"])
    assert batch is not None
    assert len(batch) == 2
    assert len(batch[0]) == 384
    assert len(batch[1]) == 384

    worker.shutdown()
    assert not worker.is_alive()


def test_semantic_worker_timeout_fails_open():
    worker = SemanticWorkerService(use_dummy=True, simulated_delay_s=0.6)
    worker.start()

    # Request with 0.1s timeout should fail open (return None) without raising
    res = worker.embed_query("slow query", timeout_s=0.1)
    assert res is None

    worker.shutdown()


def test_semantic_worker_crash_fails_open():
    worker = SemanticWorkerService(use_dummy=True)
    worker.start()

    # Force kill worker subprocess
    if worker._process:
        worker._process.terminate()
        worker._process.join()

    # Worker is dead; should return None immediately and fail open
    res = worker.embed_query("query after crash", timeout_s=0.2)
    assert res is None

    worker.shutdown()


def test_semantic_worker_real_minilm_isolation():
    model_path = Path("E:/VoiceAI/modelos_f5/minilm_l12_onnx/model.onnx")
    if not model_path.exists():
        pytest.skip("Local MiniLM ONNX artifact not found")

    worker = SemanticWorkerService(use_dummy=False)
    worker.start()
    assert worker.is_alive()

    # Query embedding through subprocess
    vec = worker.embed_query("audífonos y música", timeout_s=3.0)
    assert vec is not None
    assert len(vec) == 384
    norm = sum(x * x for x in vec) ** 0.5
    assert abs(norm - 1.0) < 1e-4

    worker.shutdown()
    assert not worker.is_alive()

