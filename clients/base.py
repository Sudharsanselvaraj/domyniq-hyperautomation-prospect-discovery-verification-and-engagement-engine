"""
clients/base.py — Abstract base HTTP client

Every API client inherits from BaseClient, which handles:
  • httpx async session management
  • Tenacity retry with exponential backoff on 429/5xx
  • Circuit breaker integration
  • Structured error mapping to our exception hierarchy

Interview talking point:
  "The base class encodes all the retry and error-handling
   conventions once. Individual clients only need to describe
   their endpoints, not repeat the retry logic."
"""

import asyncio
from abc import ABC
from typing import Any, Optional

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
    RetryError,
)
import logging

from config.settings import Settings
from utils.circuit_breaker import CircuitBreaker
from utils.exceptions import (
    APIError,
    AuthenticationError,
    RateLimitError,
    ServiceUnavailableError,
)
from utils.logger import get_logger

logger = get_logger(__name__)

# Statuses that warrant a retry
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


def _is_retryable(exc: BaseException) -> bool:
    """
    Tenacity predicate: retry on RateLimitError and ServiceUnavailableError.
    We do NOT retry AuthenticationError (wrong key won't fix itself).
    """
    return isinstance(exc, (RateLimitError, ServiceUnavailableError))


class BaseClient(ABC):
    """
    Async HTTP client base with retry, circuit-breaker, and structured errors.

    Subclasses must set:
      service_name: str          — used in error messages and circuit breaker
      base_url: str              — API root URL
    """

    service_name: str = "unknown"
    base_url: str = ""

    def __init__(self, api_key: str, settings: Settings) -> None:
        self._api_key = api_key
        self._settings = settings
        self._client: Optional[httpx.AsyncClient] = None
        self._circuit = CircuitBreaker(
            name=self.service_name,
            failure_threshold=5,
            recovery_timeout=60.0,
            expected_exception=ServiceUnavailableError,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def __aenter__(self) -> "BaseClient":
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(self._settings.request_timeout_seconds),
            headers=self._build_headers(),
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *_) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    # ── To be implemented by subclasses ──────────────────────────────────────

    def _build_headers(self) -> dict[str, str]:
        """Return default auth headers. Override in subclasses."""
        return {}

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    async def _get(self, path: str, **kwargs) -> dict:
        return await self._request("GET", path, **kwargs)

    async def _post(self, path: str, **kwargs) -> dict:
        return await self._request("POST", path, **kwargs)

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        """
        Execute an HTTP request through the circuit breaker and retry layer.
        Maps HTTP status codes to our exception hierarchy.
        """
        assert self._client is not None, "Client not initialised — use async with"

        async def _do_request() -> dict:
            try:
                resp = await self._client.request(method, path, **kwargs)
            except httpx.TimeoutException as exc:
                raise ServiceUnavailableError(
                    f"Timeout calling {self.service_name} {method} {path}",
                    status_code=None,
                    service=self.service_name,
                ) from exc
            except httpx.RequestError as exc:
                raise ServiceUnavailableError(
                    f"Network error calling {self.service_name}: {exc}",
                    status_code=None,
                    service=self.service_name,
                ) from exc

            return self._handle_response(resp)

        # Retry wrapper — only fires for retryable exceptions
        @retry(
            retry=retry_if_exception(_is_retryable),
            stop=stop_after_attempt(self._settings.retry_max_attempts),
            wait=wait_exponential(
                multiplier=1,
                min=self._settings.retry_wait_min_seconds,
                max=self._settings.retry_wait_max_seconds,
            ),
            before_sleep=before_sleep_log(logger, logging.WARNING),
            reraise=True,
        )
        async def _with_retry() -> dict:
            return await self._circuit.call(_do_request)

        try:
            return await _with_retry()
        except RetryError as exc:
            raise ServiceUnavailableError(
                f"{self.service_name} failed after {self._settings.retry_max_attempts} retries",
                service=self.service_name,
            ) from exc

    def _handle_response(self, resp: httpx.Response) -> dict:
        """Map HTTP response to dict or raise typed exception."""
        if resp.status_code == 401 or resp.status_code == 403:
            raise AuthenticationError(
                f"{self.service_name}: authentication failed",
                status_code=resp.status_code,
                service=self.service_name,
            )
        if resp.status_code == 429:
            raise RateLimitError(
                f"{self.service_name}: rate limit hit",
                status_code=429,
                service=self.service_name,
            )
        if resp.status_code in {500, 502, 503, 504}:
            raise ServiceUnavailableError(
                f"{self.service_name}: server error {resp.status_code}",
                status_code=resp.status_code,
                service=self.service_name,
            )
        if not resp.is_success:
            raise APIError(
                f"{self.service_name}: unexpected status {resp.status_code}",
                status_code=resp.status_code,
                service=self.service_name,
                context={"body_preview": resp.text[:200]},
            )

        if not resp.content:
            return {}

        try:
            return resp.json()
        except Exception:
            # Some endpoints return plain text on success
            return {"raw": resp.text}
