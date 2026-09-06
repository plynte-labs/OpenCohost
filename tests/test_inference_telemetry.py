"""Unit tests for inference decode throughput telemetry and calibration lifecycle (ADR-056 WU1)."""

import threading
import pytest

from opencohost.core.engine.inference_telemetry import (
    CalibrationState,
    GenerationTelemetrySample,
    InferenceTelemetryTracker,
    ThroughputProfile,
)


def test_telemetry_sample_immutable_and_clean():
    tracker = InferenceTelemetryTracker()
    sample = tracker.record_turn(
        provider="local",
        model_id="gemma4:e4b",
        model_digest="sha256:abc12345",
        allocated_context=4096,
        eval_count=100,
        eval_duration_ns=2_000_000_000,  # 2.0s
        size_bytes=10_000_000_000,
        size_vram_bytes=10_000_000_000,
    )
    assert sample is not None
    assert isinstance(sample, GenerationTelemetrySample)
    assert sample.eval_count == 100
    assert sample.eval_duration_ns == 2_000_000_000
    assert pytest.approx(sample.tps, rel=1e-3) == 50.0
    assert sample.residency_ratio == 1.0
    assert sample.spill_bytes == 0

    # Ensure no prompt or content text fields exist
    fields = set(sample.__dataclass_fields__.keys())
    assert "content" not in fields
    assert "prompt" not in fields
    assert "thinking" not in fields


def test_invalid_samples_rejected_without_pollution():
    tracker = InferenceTelemetryTracker()

    # Zero count or duration
    assert tracker.record_turn(
        provider="local",
        model_id="gemma4:e4b",
        model_digest="sha256:abc12345",
        allocated_context=4096,
        eval_count=0,
        eval_duration_ns=1_000_000_000,
    ) is None

    assert tracker.record_turn(
        provider="local",
        model_id="gemma4:e4b",
        model_digest="sha256:abc12345",
        allocated_context=4096,
        eval_count=50,
        eval_duration_ns=0,
    ) is None

    assert tracker.record_turn(
        provider="local",
        model_id="gemma4:e4b",
        model_digest="sha256:abc12345",
        allocated_context=4096,
        eval_count=-10,
        eval_duration_ns=-100,
    ) is None

    # Partition remains COLD and unvisited
    profile = tracker.get_profile("local", "sha256:abc12345", 4096)
    assert profile.sample_count == 0
    assert profile.ewma_tps == 0.0
    assert profile.state == CalibrationState.COLD


def test_residency_and_spill_defensive_normalization():
    tracker = InferenceTelemetryTracker()

    # Case 1: Partial VRAM spill
    s1 = tracker.record_turn(
        provider="local",
        model_id="gemma4:e4b",
        model_digest="sha256:partial",
        allocated_context=4096,
        eval_count=50,
        eval_duration_ns=1_000_000_000,
        size_bytes=10_000,
        size_vram_bytes=6_000,
    )
    assert s1 is not None
    assert pytest.approx(s1.residency_ratio, rel=1e-3) == 0.6
    assert s1.spill_bytes == 4_000

    # Case 2: VRAM > size (anomalous probe read handled defensibly)
    s2 = tracker.record_turn(
        provider="local",
        model_id="gemma4:e4b",
        model_digest="sha256:overflow",
        allocated_context=4096,
        eval_count=50,
        eval_duration_ns=1_000_000_000,
        size_bytes=8_000,
        size_vram_bytes=12_000,
    )
    assert s2 is not None
    assert s2.size_vram_bytes == 8_000
    assert s2.residency_ratio == 1.0
    assert s2.spill_bytes == 0

    # Case 3: Zero size (probe unavailable) fails open to UNKNOWN (None), never 1.0
    s3 = tracker.record_turn(
        provider="local",
        model_id="gemma4:e4b",
        model_digest="sha256:zerosize",
        allocated_context=4096,
        eval_count=50,
        eval_duration_ns=1_000_000_000,
        size_bytes=0,
        size_vram_bytes=0,
    )
    assert s3 is not None
    assert s3.residency_ratio is None  # UNKNOWN, not 1.0 (ADR-056 Block 1)
    assert s3.spill_bytes is None


def test_calibration_lifecycle_cold_warming_calibrated():
    tracker = InferenceTelemetryTracker(alpha=0.5, min_warming=3, min_calibrated=5)
    key_args = dict(
        provider="local",
        model_id="qwen3:1.7b",
        model_digest="sha256:qwen17",
        allocated_context=2048,
    )

    # Initial state: COLD
    prof0 = tracker.get_profile("local", "sha256:qwen17", 2048)
    assert prof0.state == CalibrationState.COLD
    assert prof0.sample_count == 0

    # Sample 1 (100 tok/s): COLD
    tracker.record_turn(**key_args, eval_count=100, eval_duration_ns=1_000_000_000)
    prof1 = tracker.get_profile("local", "sha256:qwen17", 2048)
    assert prof1.state == CalibrationState.COLD
    assert prof1.sample_count == 1
    assert pytest.approx(prof1.ewma_tps) == 100.0

    # Sample 2 (80 tok/s): COLD (ewma: 0.5*80 + 0.5*100 = 90)
    tracker.record_turn(**key_args, eval_count=80, eval_duration_ns=1_000_000_000)
    prof2 = tracker.get_profile("local", "sha256:qwen17", 2048)
    assert prof2.state == CalibrationState.COLD
    assert prof2.sample_count == 2
    assert pytest.approx(prof2.ewma_tps) == 90.0

    # Sample 3 (60 tok/s): transitions to WARMING (ewma: 0.5*60 + 0.5*90 = 75)
    tracker.record_turn(**key_args, eval_count=60, eval_duration_ns=1_000_000_000)
    prof3 = tracker.get_profile("local", "sha256:qwen17", 2048)
    assert prof3.state == CalibrationState.WARMING
    assert prof3.sample_count == 3
    assert pytest.approx(prof3.ewma_tps) == 75.0

    # Sample 4: still WARMING
    tracker.record_turn(**key_args, eval_count=75, eval_duration_ns=1_000_000_000)
    prof4 = tracker.get_profile("local", "sha256:qwen17", 2048)
    assert prof4.state == CalibrationState.WARMING
    assert prof4.sample_count == 4

    # Sample 5: reaches CALIBRATED (threshold = 5)
    tracker.record_turn(**key_args, eval_count=75, eval_duration_ns=1_000_000_000)
    prof5 = tracker.get_profile("local", "sha256:qwen17", 2048)
    assert prof5.state == CalibrationState.CALIBRATED
    assert prof5.sample_count == 5


def test_partition_isolation():
    tracker = InferenceTelemetryTracker()

    # Turn for Gemma 4K context
    tracker.record_turn(
        provider="local",
        model_id="gemma4:e4b",
        model_digest="sha256:gemma_d1",
        allocated_context=4096,
        eval_count=40,
        eval_duration_ns=1_000_000_000,
    )

    # Turn for Gemma 8K context (different context allocation)
    tracker.record_turn(
        provider="local",
        model_id="gemma4:e4b",
        model_digest="sha256:gemma_d1",
        allocated_context=8192,
        eval_count=20,
        eval_duration_ns=1_000_000_000,
    )

    # Turn for Llama 4K context
    tracker.record_turn(
        provider="local",
        model_id="llama3",
        model_digest="sha256:llama_d1",
        allocated_context=4096,
        eval_count=80,
        eval_duration_ns=1_000_000_000,
    )

    gemma_4k = tracker.get_profile("local", "sha256:gemma_d1", 4096)
    gemma_8k = tracker.get_profile("local", "sha256:gemma_d1", 8192)
    llama_4k = tracker.get_profile("local", "sha256:llama_d1", 4096)

    assert gemma_4k.sample_count == 1
    assert pytest.approx(gemma_4k.ewma_tps) == 40.0

    assert gemma_8k.sample_count == 1
    assert pytest.approx(gemma_8k.ewma_tps) == 20.0

    assert llama_4k.sample_count == 1
    assert pytest.approx(llama_4k.ewma_tps) == 80.0


def test_thread_safety_concurrent_recording():
    tracker = InferenceTelemetryTracker()
    threads = []
    num_threads = 10
    turns_per_thread = 20

    def worker(tid: int):
        for i in range(turns_per_thread):
            tracker.record_turn(
                provider="local",
                model_id="concurrent_model",
                model_digest=f"sha256:digest_{tid % 2}",  # 2 partitions
                allocated_context=4096,
                eval_count=50 + i,
                eval_duration_ns=1_000_000_000,
            )

    for tid in range(num_threads):
        t = threading.Thread(target=worker, args=(tid,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    p0 = tracker.get_profile("local", "sha256:digest_0", 4096)
    p1 = tracker.get_profile("local", "sha256:digest_1", 4096)

    assert p0.sample_count == (num_threads // 2) * turns_per_thread
    assert p1.sample_count == (num_threads // 2) * turns_per_thread
    assert p0.state == CalibrationState.CALIBRATED
    assert p1.state == CalibrationState.CALIBRATED


def test_motor_vocal_ia_telemetry_seam_integration():
    """Verify that MotorVocalIA._generar_dialogo actually records an observational sample on success."""
    import queue
    from unittest.mock import MagicMock
    from opencohost.core import llm_engine

    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor.current_model = "llama3"
    motor.use_system_role = True
    motor.ollama = MagicMock()
    motor.ollama.chat.return_value = {
        "message": {"content": "Respuesta observada."},
        "eval_count": 50,
        "eval_duration": 1_000_000_000,
        "prompt_eval_count": 12,
    }

    # Setup health monitor mock with real OllamaResidencySnapshot
    from opencohost.core.observability.health_monitor import OllamaResidencySnapshot
    import time
    mock_snap = OllamaResidencySnapshot(
        model="llama3",
        digest="sha256:llama3_digest_abc123",
        size_bytes=8192 * 1024 * 1024,
        size_vram_bytes=6144 * 1024 * 1024,
        context_length=4096,
        observed_at=time.time(),
    )
    mock_monitor = MagicMock()
    mock_monitor.residency_snapshot = mock_snap
    motor.health_monitor = mock_monitor

    # Verify initial state is empty
    assert len(motor.inference_telemetry.get_recent_samples()) == 0

    # Actually execute dialogue generation
    reply = motor._generar_dialogo("hola mundo", source="direct", commit_history=False)
    assert reply == "Respuesta observada."

    # Assert that _generar_dialogo implicitly recorded the turn into inference_telemetry
    samples = motor.inference_telemetry.get_recent_samples()
    assert len(samples) == 1
    sample = samples[0]
    assert sample.eval_count == 50
    assert sample.eval_duration_ns == 1_000_000_000
    assert pytest.approx(sample.tps) == 50.0
    assert sample.model_id == "llama3"
    assert sample.model_digest == "sha256:llama3_digest_abc123"
    assert sample.size_bytes == 8192 * 1024 * 1024
    assert sample.size_vram_bytes == 6144 * 1024 * 1024
    assert pytest.approx(sample.residency_ratio) == 0.75
    assert sample.spill_bytes == 2048 * 1024 * 1024

    # Verify throughput profile was updated under the cryptographic digest partition
    prof = motor.inference_telemetry.get_profile("local", "sha256:llama3_digest_abc123", sample.allocated_context)
    assert prof.sample_count == 1
    assert pytest.approx(prof.ewma_tps) == 50.0
    assert prof.state == CalibrationState.COLD


def test_fail_open_invalid_types_never_raises():
    """ADR-056 Block 3: record_turn() must return None on invalid types, never raise."""
    tracker = InferenceTelemetryTracker()

    # None values
    assert tracker.record_turn(eval_count=None, eval_duration_ns=1_000_000_000) is None
    assert tracker.record_turn(eval_count=50, eval_duration_ns=None) is None

    # Invalid strings
    assert tracker.record_turn(eval_count="not_a_number", eval_duration_ns=1_000_000_000) is None
    assert tracker.record_turn(eval_count=50, eval_duration_ns="banana") is None

    # NaN / Inf / float overflow
    assert tracker.record_turn(eval_count=50, eval_duration_ns=float("nan")) is None
    assert tracker.record_turn(eval_count=50, eval_duration_ns=float("inf")) is None
    assert tracker.record_turn(eval_count=50, eval_duration_ns=1_000_000_000, timestamp=float("nan")) is not None


def test_partition_isolation_without_digest_never_pollutes():
    """ADR-056 Block 2: Missing digests must not collide across different models."""
    tracker = InferenceTelemetryTracker()

    # Record turn for llama3 with unknown digest at 4096 ctx (100 tps)
    tracker.record_turn(
        provider="local",
        model_id="llama3",
        model_digest=None,
        allocated_context=4096,
        eval_count=100,
        eval_duration_ns=1_000_000_000,
    )

    # Record turn for gemma4:e4b with unknown digest at 4096 ctx (20 tps)
    tracker.record_turn(
        provider="local",
        model_id="gemma4:e4b",
        model_digest=None,
        allocated_context=4096,
        eval_count=20,
        eval_duration_ns=1_000_000_000,
    )

    # Verify each model has its own isolated profile
    llama_prof = tracker.get_profile("local", "llama3", "unknown", 4096)
    gemma_prof = tracker.get_profile("local", "gemma4:e4b", "unknown", 4096)

    assert llama_prof.sample_count == 1
    assert pytest.approx(llama_prof.ewma_tps) == 100.0

    assert gemma_prof.sample_count == 1
    assert pytest.approx(gemma_prof.ewma_tps) == 20.0  # Gemma never inherited Llama's 100 tps!


def test_lru_partitions_bounded():
    """ADR-056 Block 4: _partitions must be strictly bounded with LRU eviction."""
    tracker = InferenceTelemetryTracker(max_partitions=4)

    # Populate 4 distinct partitions
    for i in range(4):
        tracker.record_turn(
            provider="local",
            model_id=f"model_{i}",
            model_digest=f"digest_{i}",
            allocated_context=4096,
            eval_count=50,
            eval_duration_ns=1_000_000_000,
        )

    assert len(tracker._partitions) == 4
    # model_0 should exist
    assert tracker.get_profile("local", "model_0", "digest_0", 4096).sample_count == 1

    # Add 5th partition -> oldest (model_1, since model_0 was just refreshed) should be evicted
    tracker.record_turn(
        provider="local",
        model_id="model_4",
        model_digest="digest_4",
        allocated_context=4096,
        eval_count=60,
        eval_duration_ns=1_000_000_000,
    )

    assert len(tracker._partitions) <= 4
    # model_1 was evicted (COLD, sample_count=0)
    assert tracker.get_profile("local", "model_1", "digest_1", 4096).sample_count == 0
    # model_4 exists
    assert tracker.get_profile("local", "model_4", "digest_4", 4096).sample_count == 1


def test_streaming_telemetry_gate_success_and_cancellation():
    """ADR-056 Block 9: Streaming completion produces exactly 1 sample; abort produces 0."""
    import queue
    from unittest.mock import MagicMock
    from opencohost.core import llm_engine

    class FakeJob:
        def __init__(self, job_id):
            self.job_id = job_id
            self.sealed = False

    class FakeRouter:
        def __init__(self):
            self.jobs = []
            self.appends = []
            self.sealed = []

        def submit_streaming(self, source, priority):
            job = FakeJob(len(self.jobs) + 1)
            self.jobs.append((job, source, priority))
            return job

        def append_chunks(self, job, chunks):
            self.appends.append((job, list(chunks)))
            return True

        def seal(self, job):
            job.sealed = True
            self.sealed.append(job)

    def _arm_stream(m, chunks):
        def _streaming(*, timeout, **kwargs):
            def _gen():
                for item in chunks:
                    if isinstance(item, BaseException):
                        raise item
                    yield item
            return _gen()
        m._ollama_chat_streaming = _streaming

    class FakeMessage:
        def __init__(self, content="", thinking=None):
            self.content = content
            self.thinking = thinking

    class FakeChunk:
        def __init__(self, content="", done=False, **stats):
            self.message = FakeMessage(content)
            self.done = done
            for key, value in stats.items():
                setattr(self, key, value)

        def get(self, key, default=None):
            return getattr(self, key, default)

    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor.current_model = "llama3"
    motor.use_system_role = True
    motor._speech_router_enabled = True
    router = FakeRouter()
    motor._ensure_router = lambda: router
    motor._speech_router = router

    # Case A: Successful stream completion with done chunk
    chunk1 = FakeChunk("Hola ", done=False)
    chunk2 = FakeChunk(
        "mundo.",
        done=True,
        eval_count=45,
        eval_duration=900_000_000,
        prompt_eval_count=15,
        digest="sha256:stream_done_digest",
    )

    _arm_stream(motor, [chunk1, chunk2])

    reply = motor._generar_dialogo("hola", source="direct", commit_history=True)
    assert reply == "Hola mundo."

    samples = motor.inference_telemetry.get_recent_samples()
    assert len(samples) == 1
    assert samples[0].eval_count == 45
    assert samples[0].eval_duration_ns == 900_000_000

    # Case B: Stream that errors or aborts with 0 tokens
    motor.inference_telemetry.clear()
    assert len(motor.inference_telemetry.get_recent_samples()) == 0

    _arm_stream(motor, [])

    motor._generar_dialogo("hola vacio", source="direct", commit_history=True)
    # 0 tokens -> 0 calibrated samples (no EWMA corruption)
    assert len(motor.inference_telemetry.get_recent_samples()) == 0


def test_get_profile_matches_by_model_name_when_digest_unknown():
    """Verify get_profile secondary lookup resolves real profile by model name
    when callers like /api/status query without knowing the cryptographic digest."""
    tracker = InferenceTelemetryTracker()
    # Recorded with real digest
    tracker.record_turn(
        provider="local",
        model_id="llama3",
        model_digest="sha256:fedcba987654321",
        allocated_context=4096,
        eval_count=60,
        eval_duration_ns=2_000_000_000,
    )

    # Queried without digest (as done by /api/routers/status.py)
    prof = tracker.get_profile("local", "llama3", allocated_context=4096)
    assert prof.sample_count == 1
    assert pytest.approx(prof.ewma_tps, rel=1e-2) == 30.0
    assert prof.model_digest == "sha256:fedcba987654321"

    # Add 2 more samples to reach WARMING
    tracker.record_turn(
        provider="local",
        model_id="llama3",
        model_digest="sha256:fedcba987654321",
        allocated_context=4096,
        eval_count=80,
        eval_duration_ns=2_000_000_000,
    )
    tracker.record_turn(
        provider="local",
        model_id="llama3",
        model_digest="sha256:fedcba987654321",
        allocated_context=4096,
        eval_count=70,
        eval_duration_ns=2_000_000_000,
    )
    prof_warm = tracker.get_profile("local", "llama3", allocated_context=4096)
    assert prof_warm.sample_count == 3
    assert prof_warm.state == CalibrationState.WARMING
    assert prof_warm.model_digest == "sha256:fedcba987654321"


