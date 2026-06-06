"""
utils/circuit_breaker.py — Circuit Breaker Pattern

States:
  CLOSED  → normal operation, requests pass through
  OPEN    → too many failures, requests fail immediately (no network call)
  HALF-OPEN → testing recovery, allow one request; if it succeeds → CLOSED

Why it matters:
  Without a circuit breaker, a down stream API (e.g. EazyReach) causes
  every lead to timeout and wait the full retry window. A circuit breaker
  detects the pattern and fast-fails, saving quota and wall-clock time.

Interview talking point:
  "This is borrowed from microservices resilience patterns. The key
   insight is that failing fast is kinder than waiting for N timeouts
   to expire across hundreds of leads."
"""

import asyncio
import time
from enum import Enum, auto
from typing import Callable, Optional

from utils.exceptions import CircuitOpenError
from utils.logger import get_logger

logger = get_logger(__name__)


class CircuitState(Enum):
    CLOSED = auto()
    OPEN = auto()
    HALF_OPEN = auto()


class CircuitBreaker:
    """
    Simple async-compatible circuit breaker.

    Args:
        name:              Human-readable name for logging.
        failure_threshold: How many consecutive failures trigger OPEN.
        recovery_timeout:  Seconds to wait before entering HALF-OPEN.
        expected_exception: Which exception type counts as a failure.
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        expected_exception: type[Exception] = Exception,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exception = expected_exception

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time: Optional[float] = None
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        return self._state

    async def call(self, func: Callable, *args, **kwargs):
        """
        Wrap an async callable. Raises CircuitOpenError if the circuit is OPEN
        and the recovery timeout hasn't elapsed yet.
        """
        async with self._lock:
            if self._state == CircuitState.OPEN:
                elapsed = time.monotonic() - (self._last_failure_time or 0)
                if elapsed >= self.recovery_timeout:
                    logger.info(
                        f"Circuit breaker [{self.name}] entering HALF-OPEN"
                    )
                    self._state = CircuitState.HALF_OPEN
                else:
                    remaining = self.recovery_timeout - elapsed
                    raise CircuitOpenError(
                        f"Circuit breaker [{self.name}] OPEN — "
                        f"retry in {remaining:.0f}s",
                        context={"service": self.name, "remaining_seconds": remaining},
                    )

        try:
            result = await func(*args, **kwargs)
            await self._on_success()
            return result
        except self.expected_exception as exc:
            await self._on_failure()
            raise exc

    async def _on_success(self) -> None:
        async with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                logger.info(f"Circuit breaker [{self.name}] CLOSED (recovered)")
            self._state = CircuitState.CLOSED
            self._failure_count = 0

    async def _on_failure(self) -> None:
        async with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()
            if self._failure_count >= self.failure_threshold:
                if self._state != CircuitState.OPEN:
                    logger.warning(
                        f"Circuit breaker [{self.name}] OPEN "
                        f"after {self._failure_count} failures"
                    )
                self._state = CircuitState.OPEN

    def reset(self) -> None:
        """Manually reset (useful in tests)."""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time = None
