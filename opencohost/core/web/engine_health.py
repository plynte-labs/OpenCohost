"""Circuit breakers and cooldown tracking for search engines (web-search-api-fallback-20261010 T4).

Health policies:
  * DuckDuckGo cooldown: after anti_bot on BOTH html and lite, mark DDG cooled down
    for DDG_COOLDOWN_S (600s). During cooldown, if a search API key is configured,
    skip DDG and jump straight to the API fallback. If no search API key is configured,
    DDG is NOT cooled down (retried every turn).
  * Provider breakers:
      - auth (401/403): disable provider until restart, log single warning.
      - quota (432/402): disable provider until next month.
      - rate_limit (429): back off for 60s.
  * In-memory only, thread-safe, supports injectable clock and month resolver.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Callable, Optional

from opencohost.config import settings
from opencohost.core.web import search, search_api

logger = logging.getLogger("OpenCohost")

RATE_LIMIT_BACKOFF_S = 60.0


def _current_month_str() -> str:
    return datetime.now().strftime("%Y-%m")


class EngineHealth:
    """In-memory circuit breaker and cooldown registry."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        month_resolver: Optional[Callable[[], str]] = None,
        cooldown_s: Optional[float] = None,
    ) -> None:
        self._clock = clock
        self._month_resolver = month_resolver
        self._cooldown_s = cooldown_s
        self._lock = threading.Lock()

        # DDG cooldown state
        self._ddg_cooldown_until: float = 0.0
        self._ddg_anti_bot_seen: set[str] = set()

        # Provider breaker state
        self._disabled_auth: set[str] = set()
        self._warned_auth: set[str] = set()
        self._quota_disabled_months: dict[str, str] = {}  # provider -> month_str
        self._rate_limit_until: dict[str, float] = {}  # provider -> timestamp

    def _get_cooldown_s(self) -> float:
        if self._cooldown_s is not None:
            return self._cooldown_s
        return float(getattr(settings, "DDG_COOLDOWN_S", 600.0))

    def _get_month(self) -> str:
        if self._month_resolver is not None:
            return self._month_resolver()
        return _current_month_str()

    def record_ddg_attempt(self, engine: str, reason: str) -> None:
        """Record an attempt result on a DDG endpoint (html or lite)."""
        with self._lock:
            if reason == search.REASON_ANTI_BOT:
                self._ddg_anti_bot_seen.add(engine)
                if "html" in self._ddg_anti_bot_seen and "lite" in self._ddg_anti_bot_seen:
                    self._ddg_cooldown_until = self._clock() + self._get_cooldown_s()
            else:
                self._ddg_anti_bot_seen.discard(engine)

    def is_ddg_cooled_down(self, has_api_keys: bool) -> bool:
        """DDG cooldown is active ONLY when an API key is configured and cooldown window has not elapsed."""
        if not has_api_keys:
            return False
        with self._lock:
            return self._clock() < self._ddg_cooldown_until

    def is_provider_available(self, provider: str) -> tuple[bool, str]:
        """Check if provider is available. Returns (available, reason_if_not)."""
        with self._lock:
            if provider in self._disabled_auth:
                return False, search_api.REASON_AUTH

            current_month = self._get_month()
            if self._quota_disabled_months.get(provider) == current_month:
                return False, search_api.REASON_QUOTA

            rl_until = self._rate_limit_until.get(provider, 0.0)
            if self._clock() < rl_until:
                return False, search_api.REASON_RATE_LIMIT

            return True, ""

    def record_provider_failure(self, provider: str, reason: str) -> None:
        """Update circuit breakers based on failure reason."""
        with self._lock:
            if reason == search_api.REASON_AUTH:
                self._disabled_auth.add(provider)
                if provider not in self._warned_auth:
                    logger.warning("[WEB] provider=%s disabled reason=auth", provider)
                    self._warned_auth.add(provider)
            elif reason == search_api.REASON_QUOTA:
                self._quota_disabled_months[provider] = self._get_month()
            elif reason == search_api.REASON_RATE_LIMIT:
                self._rate_limit_until[provider] = self._clock() + RATE_LIMIT_BACKOFF_S


_DEFAULT_HEALTH: Optional[EngineHealth] = None
_HEALTH_LOCK = threading.Lock()


def get_health() -> EngineHealth:
    global _DEFAULT_HEALTH
    with _HEALTH_LOCK:
        if _DEFAULT_HEALTH is None:
            _DEFAULT_HEALTH = EngineHealth()
        return _DEFAULT_HEALTH


def reset_health() -> None:
    """Reset the global health instance (for tests)."""
    global _DEFAULT_HEALTH
    with _HEALTH_LOCK:
        _DEFAULT_HEALTH = EngineHealth()
