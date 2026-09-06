"""Empirical inference telemetry and decode throughput profiler (ADR-056 WU1).

Captures generation decode metrics (eval_count, eval_duration_ns) from Ollama
and computes partition-isolated Exponentially Weighted Moving Average (EWMA)
throughput profiles. Strictly observational — zero impact on generation budgets,
context limits, or model behavior.
"""

from __future__ import annotations

import collections
import enum
import math
import threading
import time
from dataclasses import dataclass
from typing import Deque, List, Optional, Tuple


class CalibrationState(str, enum.Enum):
    """Lifecycle of a partition's throughput profile calibration."""

    COLD = "COLD"          # < MIN_SAMPLES_WARMING valid samples
    WARMING = "WARMING"    # MIN_SAMPLES_WARMING .. MIN_SAMPLES_CALIBRATED - 1
    CALIBRATED = "CALIBRATED"  # >= MIN_SAMPLES_CALIBRATED samples


MIN_SAMPLES_WARMING: int = 3
MIN_SAMPLES_CALIBRATED: int = 10
DEFAULT_EWMA_ALPHA: float = 0.3
MAX_RECENT_SAMPLES: int = 50
MAX_PARTITIONS: int = 64

# Partition key: (provider, model_id, model_digest, allocated_context)
PartitionKey = Tuple[str, str, str, int]


@dataclass(frozen=True)
class GenerationTelemetrySample:
    """Immutable record of one successful decode generation turn."""

    provider: str
    model_id: str
    model_digest: str
    allocated_context: int
    eval_count: int
    eval_duration_ns: int
    tps: float
    size_bytes: int
    size_vram_bytes: int
    residency_ratio: Optional[float]  # None = UNKNOWN, 1.0 = FULLY_RESIDENT, 0.0..1.0 = PARTIAL
    spill_bytes: Optional[int]
    timestamp: float


@dataclass(frozen=True)
class ThroughputProfile:
    """Snapshot of calibrated throughput for a specific partition."""

    provider: str
    model_id: str
    model_digest: str
    allocated_context: int
    sample_count: int
    ewma_tps: float
    state: CalibrationState
    last_sample_time: float


class InferenceTelemetryTracker:
    """Thread-safe, in-memory observer for inference decode performance.

    Partitions EWMA metrics by (provider, model_id, model_digest, allocated_context)
    so different models, quantization digests, or context windows never pollute
    each other even when cryptographic digest is unknown.
    """

    def __init__(
        self,
        alpha: float = DEFAULT_EWMA_ALPHA,
        min_warming: int = MIN_SAMPLES_WARMING,
        min_calibrated: int = MIN_SAMPLES_CALIBRATED,
        max_history: int = MAX_RECENT_SAMPLES,
        max_partitions: int = MAX_PARTITIONS,
    ) -> None:
        self._alpha = max(0.01, min(1.0, float(alpha)))
        self._min_warming = max(1, int(min_warming))
        self._min_calibrated = max(self._min_warming, int(min_calibrated))
        self._max_partitions = max(1, int(max_partitions))
        self._lock = threading.Lock()
        self._partitions: collections.OrderedDict[PartitionKey, dict[str, object]] = collections.OrderedDict()
        self._recent_samples: Deque[GenerationTelemetrySample] = collections.deque(
            maxlen=max(10, int(max_history))
        )

    def record_turn(
        self,
        *,
        provider: str = "local",
        model_id: str = "unknown",
        model_digest: Optional[str] = None,
        allocated_context: int = 0,
        eval_count: int = 0,
        eval_duration_ns: int = 0,
        size_bytes: int = 0,
        size_vram_bytes: int = 0,
        timestamp: Optional[float] = None,
    ) -> Optional[GenerationTelemetrySample]:
        """Record one generation's metrics. Returns the sample if valid, else None.

        Fail-open guarantee: strictly catches any conversion/numeric anomalies
        (TypeError, ValueError, OverflowError, ArithmeticError) and returns None without raising.
        """
        try:
            cnt = int(eval_count)
            dur_ns = int(eval_duration_ns)
            if cnt <= 0 or dur_ns <= 0:
                return None

            dur_s = float(dur_ns) / 1e9
            if dur_s <= 0.0 or math.isnan(dur_s) or math.isinf(dur_s):
                return None
            tps = float(cnt) / dur_s
            if math.isnan(tps) or math.isinf(tps) or tps <= 0.0:
                return None

            safe_ctx = max(0, int(allocated_context or 0))
            safe_size = max(0, int(size_bytes or 0))
            safe_vram = max(0, min(safe_size, int(size_vram_bytes or 0))) if safe_size > 0 else 0

            # Hardware residency normalization: None = UNKNOWN if size <= 0
            if safe_size > 0:
                ratio: Optional[float] = max(0.0, min(1.0, float(safe_vram) / float(safe_size)))
                spill: Optional[int] = max(0, safe_size - safe_vram)
            else:
                ratio = None
                spill = None

            now = time.time()
            ts = float(now if timestamp is None else timestamp)
            if math.isnan(ts) or math.isinf(ts):
                ts = now

            clean_provider = str(provider or "local").strip().lower()
            clean_model_id = str(model_id or "unknown").strip().lower()
            clean_digest = str(model_digest).strip() if model_digest else "unknown"

            sample = GenerationTelemetrySample(
                provider=clean_provider,
                model_id=clean_model_id,
                model_digest=clean_digest,
                allocated_context=safe_ctx,
                eval_count=cnt,
                eval_duration_ns=dur_ns,
                tps=tps,
                size_bytes=safe_size,
                size_vram_bytes=safe_vram,
                residency_ratio=ratio,
                spill_bytes=spill,
                timestamp=ts,
            )

            # PartitionKey incorporates model_id so missing digests never collide across different models
            partition_key: PartitionKey = (clean_provider, clean_model_id, clean_digest, safe_ctx)

            with self._lock:
                self._recent_samples.append(sample)
                part = self._partitions.get(partition_key)
                if part is None:
                    # LRU eviction if capacity exceeded
                    while len(self._partitions) >= self._max_partitions:
                        self._partitions.popitem(last=False)
                    part = {
                        "count": 1,
                        "ewma": tps,
                        "last_time": ts,
                    }
                    self._partitions[partition_key] = part
                else:
                    self._partitions.move_to_end(partition_key)
                    count = int(part["count"]) + 1
                    prev_ewma = float(part["ewma"])
                    new_ewma = self._alpha * tps + (1.0 - self._alpha) * prev_ewma
                    part["count"] = count
                    part["ewma"] = new_ewma
                    part["last_time"] = ts

            return sample
        except (TypeError, ValueError, OverflowError, ArithmeticError):
            return None

    def get_profile(
        self,
        provider: str,
        model_id: str,
        model_digest: Optional[str] = None,
        allocated_context: int = 0,
    ) -> ThroughputProfile:
        """Get the current calibrated profile for a specific partition.

        Supports both 4-argument and 3-argument legacy calls:
        - 4 args: get_profile(provider, model_id, model_digest, allocated_context)
        - 3 args: get_profile(provider, model_digest, allocated_context)
        """
        clean_provider = str(provider or "local").strip().lower()

        # Handle backward compatibility if called with (provider, model_digest, allocated_context)
        if isinstance(model_digest, int) and allocated_context == 0:
            safe_ctx = max(0, model_digest)
            clean_digest = str(model_id or "unknown").strip()
            clean_model_id = clean_digest
        else:
            safe_ctx = max(0, int(allocated_context or 0))
            clean_model_id = str(model_id or "unknown").strip().lower()
            clean_digest = str(model_digest).strip() if model_digest else "unknown"

        partition_key: PartitionKey = (clean_provider, clean_model_id, clean_digest, safe_ctx)
        matched_key: Optional[PartitionKey] = None

        with self._lock:
            part = self._partitions.get(partition_key)
            if part is not None:
                matched_key = partition_key
            else:
                # Secondary lookup by (provider, model_id, context) or (provider, digest, context)
                for k, v in self._partitions.items():
                    if k[0] != clean_provider:
                        continue
                    if safe_ctx > 0 and k[3] != safe_ctx:
                        continue
                    if clean_digest == "unknown" and clean_model_id != "unknown":
                        if k[1] == clean_model_id:
                            part = v
                            matched_key = k
                            clean_digest = k[2]
                            break
                    elif clean_model_id == "unknown" and clean_digest != "unknown":
                        if k[2] == clean_digest:
                            part = v
                            matched_key = k
                            clean_model_id = k[1]
                            break
                    elif (clean_model_id != "unknown" and k[1] == clean_model_id) or (clean_digest != "unknown" and k[2] == clean_digest):
                        part = v
                        matched_key = k
                        clean_model_id = k[1]
                        clean_digest = k[2]
                        break

            if part is None or matched_key is None:
                return ThroughputProfile(
                    provider=clean_provider,
                    model_id=clean_model_id,
                    model_digest=clean_digest,
                    allocated_context=safe_ctx,
                    sample_count=0,
                    ewma_tps=0.0,
                    state=CalibrationState.COLD,
                    last_sample_time=0.0,
                )

            self._partitions.move_to_end(matched_key, last=True)
            count = int(part["count"])
            ewma = float(part["ewma"])
            last_ts = float(part["last_time"])

            if count < self._min_warming:
                state = CalibrationState.COLD
            elif count < self._min_calibrated:
                state = CalibrationState.WARMING
            else:
                state = CalibrationState.CALIBRATED

            return ThroughputProfile(
                provider=clean_provider,
                model_id=clean_model_id,
                model_digest=clean_digest,
                allocated_context=safe_ctx,
                sample_count=count,
                ewma_tps=ewma,
                state=state,
                last_sample_time=last_ts,
            )

    def get_recent_samples(self, limit: int = 10) -> List[GenerationTelemetrySample]:
        """Return a read-only list of the most recent samples."""
        with self._lock:
            samples = list(self._recent_samples)
        if limit > 0:
            return samples[-limit:]
        return samples

    def clear(self) -> None:
        """Reset all in-memory partitions and recent history."""
        with self._lock:
            self._partitions.clear()
            self._recent_samples.clear()
