"""
utils/exceptions.py — Custom exception hierarchy

Having a dedicated exception tree means:
  1. Each service can catch only the errors it understands
  2. The orchestrator can catch PipelineError broadly for resilience
  3. Logs include structured context (which API, which status code)

Interview talking point:
  "I never let raw httpx.HTTPStatusError bubble up to the orchestrator.
   Wrapping gives us a single place to add logging, metrics, and
   context without touching business logic."
"""

from typing import Optional


class PipelineError(Exception):
    """Base class for all pipeline errors."""

    def __init__(self, message: str, *, context: Optional[dict] = None):
        super().__init__(message)
        self.context = context or {}

    def __str__(self) -> str:
        base = super().__str__()
        if self.context:
            ctx_str = ", ".join(f"{k}={v}" for k, v in self.context.items())
            return f"{base} [{ctx_str}]"
        return base


# ── API Client Errors ─────────────────────────────────────────────────────────

class APIError(PipelineError):
    """An API call returned an unexpected response."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        service: Optional[str] = None,
        context: Optional[dict] = None,
    ):
        ctx = context or {}
        if status_code is not None:
            ctx["status_code"] = status_code
        if service is not None:
            ctx["service"] = service
        super().__init__(message, context=ctx)
        self.status_code = status_code
        self.service = service


class RateLimitError(APIError):
    """HTTP 429 — we hit the service rate limit."""
    pass


class AuthenticationError(APIError):
    """HTTP 401/403 — bad or missing API credentials."""
    pass


class ServiceUnavailableError(APIError):
    """HTTP 500/502/503/504 — upstream service is down."""
    pass


class ValidationError(PipelineError):
    """API returned data that failed Pydantic validation."""
    pass


# ── Stage-specific Errors ─────────────────────────────────────────────────────

class OceanError(APIError):
    """Ocean.io specific error."""
    pass


class ProspeoError(APIError):
    """Prospeo specific error."""
    pass


class EazyReachError(APIError):
    """EazyReach specific error."""
    pass


class BrevoError(APIError):
    """Brevo specific error."""
    pass


class EmailGenerationError(PipelineError):
    """OpenAI email copy generation failed."""
    pass


# ── Circuit Breaker ───────────────────────────────────────────────────────────

class CircuitOpenError(PipelineError):
    """
    The circuit breaker for a service is OPEN — too many recent failures.
    We stop sending requests to protect both our quota and the downstream service.
    """
    pass


# ── Configuration ─────────────────────────────────────────────────────────────

class ConfigurationError(PipelineError):
    """Missing or invalid configuration."""
    pass
