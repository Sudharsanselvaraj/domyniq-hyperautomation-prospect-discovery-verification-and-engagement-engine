"""
clients/eazyreach_client.py — EazyReach API Client

⚠️  ARCHIVAL — ORIGINAL IMPLEMENTATION
This was the Stage 3 client in the first iteration of the pipeline.
It was replaced by ProspeoClient.bulk_enrich_emails() (clients/prospeo_client.py)
because EazyReach suspended free credits during the assignment window.

The file is kept in the repo to show:
  1. The original architectural intent (LinkedIn URL → work email resolution)
  2. How a single-contact enrich client is structured
  3. Interview evidence that a pivot was made deliberately, not by accident

Stage 3: Resolve LinkedIn profile URLs to verified work email addresses.

EazyReach docs: https://eazyreach.app
Auth: API key in X-API-Key header
Key endpoint: POST /api/v1/email  (single resolve)
"""

from clients.base import BaseClient
from config.settings import Settings
from utils.exceptions import EazyReachError, ValidationError
from utils.logger import get_logger

logger = get_logger(__name__)


class EazyReachClient(BaseClient):
    service_name = "EazyReach"
    base_url = "https://app.eazyreach.app"

    def __init__(self, settings: Settings) -> None:
        super().__init__(api_key=settings.eazyreach_api_key, settings=settings)

    def _build_headers(self) -> dict[str, str]:
        return {
            "X-API-Key": self._api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def get_email(self, linkedin_url: str) -> str | None:
        """
        Resolve a single LinkedIn profile URL to a verified work email.

        Returns:
            The email string if found and verified, else None.
        """
        logger.debug(
            "EazyReach: resolving email",
            extra={"linkedin": linkedin_url},
        )

        payload = {"linkedin_url": linkedin_url}

        try:
            data = await self._post("/api/v1/email", json=payload)
        except Exception as exc:
            raise EazyReachError(
                f"Failed to resolve email for {linkedin_url}: {exc}",
                service=self.service_name,
            ) from exc

        email = self._parse_response(data, linkedin_url)
        if email:
            logger.debug(
                "EazyReach: email resolved",
                extra={"linkedin": linkedin_url, "email": email},
            )
        else:
            logger.debug(
                "EazyReach: no email found",
                extra={"linkedin": linkedin_url},
            )
        return email

    def _parse_response(self, data: dict, linkedin_url: str) -> str | None:
        """
        Parse EazyReach email resolution response.

        Expected shape:
        {
          "email": "jane@acme.com",
          "verified": true,
          "confidence": 0.95
        }

        Or if not found:
        {
          "email": null,
          "verified": false
        }
        """
        if not data:
            return None

        email = (
            data.get("email")
            or data.get("work_email")
            or data.get("email_address")
        )

        if not email:
            return None

        # Respect the verified flag if present; include if absent (trust API)
        verified = data.get("verified")
        if verified is False:  # explicitly false, not missing
            logger.debug(
                f"EazyReach: email unverified for {linkedin_url}: {email}"
            )
            return None

        email = str(email).strip().lower()
        if "@" not in email:
            raise ValidationError(
                f"EazyReach returned malformed email: {email!r}",
                context={"linkedin": linkedin_url},
            )

        return email
