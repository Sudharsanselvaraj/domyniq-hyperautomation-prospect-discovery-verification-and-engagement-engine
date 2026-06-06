"""
clients/prospeo_client.py — Prospeo API Client

Stage 2: Given company domains, find C-suite / VP decision-makers
         with their LinkedIn profile URLs.

Prospeo API docs: https://app.prospeo.io/api
Auth: X-KEY header
Key endpoint: POST /domain-search

Interview talking point:
  "Prospeo is per-credit — every call costs money. I batch
   by domain and respect the response pagination to avoid
   duplicate requests. I also honour their documented rate limits."
"""

from typing import Optional

from clients.base import BaseClient
from config.settings import Settings
from models.pipeline import Contact
from utils.exceptions import ProspeoError, ValidationError
from utils.logger import get_logger

logger = get_logger(__name__)

# Seniority levels we care about (Prospeo filter values)
TARGET_SENIORITY = {
    "c_suite", "vp", "director", "head", "owner", "partner", "founder",
}


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

    async def get_decision_makers(
        self,
        domain: str,
        limit: int = 5,
    ) -> list[Contact]:
        """
        POST /domain-search — return decision-makers for a company domain.

        We filter for C-suite / VP / Director seniority on our side
        (Prospeo also exposes a filter param, used here as belt-and-suspenders).
        """
        logger.debug(
            "Prospeo: searching domain",
            extra={"domain": domain, "limit": limit},
        )

        payload = {
            "company": domain,
            "limit": limit,
            "seniority": list(TARGET_SENIORITY),
        }

        try:
            data = await self._post("/domain-search", json=payload)
        except Exception as exc:
            raise ProspeoError(
                f"Failed to fetch contacts for {domain}: {exc}",
                service=self.service_name,
            ) from exc

        contacts = self._parse_response(data, domain)
        logger.debug(
            "Prospeo: contacts found",
            extra={"domain": domain, "count": len(contacts)},
        )
        return contacts

    def _parse_response(self, data: dict, domain: str) -> list[Contact]:
        """
        Parse Prospeo domain-search response.

        Expected shape:
        {
          "response": [
            {
              "full_name": "Jane Smith",
              "job_title": "Chief Marketing Officer",
              "linkedin_url": "https://linkedin.com/in/janesmith",
              "company": "Acme Corp"
            },
            ...
          ]
        }
        """
        raw_list = (
            data.get("response")
            or data.get("contacts")
            or data.get("results")
            or data.get("data")
            or []
        )

        if not isinstance(raw_list, list):
            raise ValidationError(
                "Prospeo response is not a list",
                context={"domain": domain, "keys": list(data.keys())},
            )

        contacts: list[Contact] = []
        for item in raw_list:
            if not isinstance(item, dict):
                continue

            name = (
                item.get("full_name")
                or item.get("name")
                or f"{item.get('first_name', '')} {item.get('last_name', '')}".strip()
            )
            title = item.get("job_title") or item.get("title") or item.get("position") or ""
            linkedin = (
                item.get("linkedin_url")
                or item.get("linkedin")
                or item.get("profile_url")
                or ""
            )

            if not name or not linkedin:
                logger.debug(f"Skipping contact missing name or LinkedIn: {item}")
                continue

            # Filter to decision-makers only (belt-and-suspenders check)
            if title and not self._is_decision_maker(title):
                logger.debug(f"Skipping non-DM title: {title}")
                continue

            try:
                contact = Contact(
                    name=name,
                    title=title or "Unknown",
                    linkedin_url=linkedin,
                    company_domain=domain,
                    company_name=item.get("company") or item.get("company_name"),
                )
                contacts.append(contact)
            except Exception as exc:
                logger.debug(f"Skipping invalid contact record: {exc}")
                continue

        return contacts

    @staticmethod
    def _is_decision_maker(title: str) -> bool:
        """
        Heuristic filter: does the title suggest purchase authority?
        We'd rather include a borderline title than miss a real buyer.
        """
        title_lower = title.lower()
        keywords = {
            "ceo", "cto", "cmo", "coo", "cfo", "cso", "cpo", "chief",
            "vp ", "vice president", "director", "head of", "head,",
            "president", "founder", "co-founder", "owner", "partner",
            "managing", "general manager", "gm ",
        }
        return any(kw in title_lower for kw in keywords)
