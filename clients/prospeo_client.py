"""
clients/prospeo_client.py — Prospeo API Client (New API v2)

Stage 2: Search for decision-makers at a company domain.
Stage 3: Bulk-enrich those contacts to get verified work emails.

Prospeo API docs: https://prospeo.io/api-docs
Auth: X-KEY header

Note: Prospeo recently migrated to a new API. The old /domain-search endpoint
is deprecated. The new flow is:
  1. POST /search-person   — find people by company + seniority (no email)
  2. POST /bulk-enrich-person — enrich up to 50 person_ids at once (get email)

Interview talking point:
  "Prospeo deprecated their old domain-search endpoint. I migrated to their
   new search + bulk-enrich flow. Bulk enrichment is more efficient —
   one call for 50 contacts instead of 50 individual calls."
"""

from typing import Optional

from clients.base import BaseClient
from config.settings import Settings
from models.pipeline import Contact
from utils.exceptions import ProspeoError, ValidationError
from utils.logger import get_logger

logger = get_logger(__name__)

# Seniority values Prospeo accepts (from their ENUM)
TARGET_SENIORITY = [
    "C-Suite",
    "Vice President",
    "Director",
    "Head",
    "Founder/Owner",
    "Partner",
    "Senior",
]


class ProspeoClient(BaseClient):
    service_name = "Prospeo"
    base_url = "https://api.prospeo.io"

    def __init__(self, settings: Settings) -> None:
        super().__init__(api_key=settings.prospeo_api_key, settings=settings)

    def _build_headers(self) -> dict[str, str]:
        return {
            "X-KEY": self._api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def search_decision_makers(
        self,
        domain: str,
        limit: int = 5,
    ) -> list[Contact]:
        """
        POST /search-person — find decision-makers at a company.

        Filters:
          • company.websites.include = [domain]
          • person_seniority.include = [C-Suite, VP, Director, ...]

        Returns Contact objects with person_id (needed for enrichment).
        Email is NOT included — Stage 3 bulk-enrich resolves that.
        """
        logger.debug(
            "Prospeo: searching decision-makers",
            extra={"domain": domain, "limit": limit},
        )

        payload = {
            "page": 1,
            "filters": {
                "company": {
                    "websites": {
                        "include": [domain],
                    }
                },
                "person_seniority": {
                    "include": TARGET_SENIORITY,
                },
            },
        }

        try:
            data = await self._post("/search-person", json=payload)
        except Exception as exc:
            raise ProspeoError(
                f"Failed to search contacts for {domain}: {exc}",
                service=self.service_name,
            ) from exc

        contacts = self._parse_search_response(data, domain)
        # Respect the caller's limit (API returns up to 25 per page)
        contacts = contacts[:limit]
        logger.debug(
            "Prospeo: contacts found",
            extra={"domain": domain, "count": len(contacts)},
        )
        return contacts

    def _parse_search_response(self, data: dict, domain: str) -> list[Contact]:
        """Parse /search-person response into Contact objects."""
        raw_list = data.get("results") or []

        if not isinstance(raw_list, list):
            raise ValidationError(
                "Prospeo search response is not a list",
                context={"domain": domain, "keys": list(data.keys())},
            )

        contacts: list[Contact] = []
        for item in raw_list:
            if not isinstance(item, dict):
                continue

            person = item.get("person") or {}
            company = item.get("company") or {}

            person_id = person.get("person_id") or person.get("id")
            if not person_id:
                continue

            name = person.get("full_name") or ""
            if not name:
                first = person.get("first_name", "")
                last = person.get("last_name", "")
                name = f"{first} {last}".strip()

            title = person.get("current_job_title") or person.get("title") or "Unknown"
            linkedin = person.get("linkedin_url") or ""

            if not name or not linkedin:
                logger.debug(f"Skipping contact missing name or LinkedIn: {person}")
                continue

            try:
                contact = Contact(
                    name=name,
                    title=title,
                    linkedin_url=linkedin,
                    company_domain=domain,
                    company_name=company.get("name"),
                    person_id=person_id,
                )
                contacts.append(contact)
            except Exception as exc:
                logger.debug(f"Skipping invalid contact record: {exc}")
                continue

        return contacts

    async def bulk_enrich_emails(
        self,
        contacts: list[Contact],
    ) -> dict[str, str]:
        """
        POST /bulk-enrich-person — resolve emails for up to 50 contacts at once.

        Args:
            contacts: List of Contact objects with person_id set.

        Returns:
            Mapping of person_id → verified email address.
        """
        if not contacts:
            return {}

        logger.debug(
            "Prospeo: bulk enriching emails",
            extra={"count": len(contacts)},
        )

        # Prospeo allows up to 50 per bulk request
        BATCH_SIZE = 50
        email_map: dict[str, str] = {}

        for i in range(0, len(contacts), BATCH_SIZE):
            batch = contacts[i : i + BATCH_SIZE]
            payload = {
                "only_verified_email": True,
                "data": [
                    {
                        "identifier": c.person_id,
                        "person_id": c.person_id,
                    }
                    for c in batch
                    if c.person_id
                ],
            }

            if not payload["data"]:
                continue

            try:
                data = await self._post("/bulk-enrich-person", json=payload)
            except Exception as exc:
                logger.warning(f"Prospeo bulk enrich failed: {exc}")
                continue

            for match in data.get("matched", []):
                person_id = match.get("identifier")
                person_data = match.get("person") or {}
                email_obj = person_data.get("email") or {}
                email = email_obj.get("email") or email_obj.get("revealed_email")
                if person_id and email and "@" in email:
                    email_map[person_id] = email.strip().lower()

        logger.debug(
            "Prospeo: bulk enrich complete",
            extra={"requested": len(contacts), "resolved": len(email_map)},
        )
        return email_map
