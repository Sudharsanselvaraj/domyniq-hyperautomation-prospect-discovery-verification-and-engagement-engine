"""
clients/ocean_client.py — Ocean.io API Client

⚠️  ARCHIVAL — ORIGINAL IMPLEMENTATION
This was the Stage 1 client in the first iteration of the pipeline.
It was replaced by ApolloClient (clients/apollo_client.py) because
Ocean.io rejected new sign-ups during the assignment window.

The file is kept in the repo to show:
  1. The original architectural intent (industry-based lookalike search)
  2. How a Stage 1 client is structured (seed domain → similar companies)
  3. Interview evidence that a pivot was made deliberately, not by accident

Stage 1: Given a seed company domain, find lookalike companies.

Ocean.io API docs: https://docs.ocean.io/
Auth: Bearer token in Authorization header
Key endpoint: POST /v1/similar-companies
"""

from typing import Optional

from clients.base import BaseClient
from config.settings import Settings
from models.pipeline import Company
from utils.exceptions import OceanError, ValidationError
from utils.logger import get_logger

logger = get_logger(__name__)


class OceanClient(BaseClient):
    service_name = "Ocean.io"
    base_url = "https://api.ocean.io"

    def __init__(self, settings: Settings) -> None:
        super().__init__(api_key=settings.ocean_api_key, settings=settings)

    def _build_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def find_similar_companies(
        self,
        seed_domain: str,
        limit: int = 25,
    ) -> list[Company]:
        """
        POST /v1/similar  — find lookalike companies for a seed domain.

        Returns a deduplicated list of Company objects.
        The seed domain itself is excluded from the results.
        """
        logger.info(
            "Ocean.io: finding similar companies",
            extra={"seed": seed_domain, "limit": limit},
        )

        payload = {
            "domain": seed_domain,
            "num_results": min(limit, 200),  # API hard cap
        }

        try:
            data = await self._post("/v1/similar", json=payload)
        except Exception as exc:
            raise OceanError(
                f"Failed to fetch similar companies for {seed_domain}: {exc}",
                service=self.service_name,
            ) from exc

        companies = self._parse_response(data, seed_domain)
        logger.info(
            "Ocean.io: similar companies found",
            extra={"seed": seed_domain, "count": len(companies)},
        )
        return companies

    def _parse_response(self, data: dict, exclude_domain: str) -> list[Company]:
        """
        Parse the Ocean.io similar companies response.

        The real API shape (as of 2024):
        {
          "companies": [
            {
              "domain": "stripe.com",
              "name": "Stripe",
              "industry": "Fintech",
              "number_of_employees": 7000,
              "country": "US"
            },
            ...
          ]
        }
        """
        raw_list = data.get("companies") or data.get("results") or data.get("data") or []

        if not isinstance(raw_list, list):
            raise ValidationError(
                "Ocean.io response 'companies' field is not a list",
                context={"keys": list(data.keys())},
            )

        companies: list[Company] = []
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            domain = (
                item.get("domain")
                or item.get("website")
                or item.get("url")
                or ""
            ).strip()
            if not domain or domain == exclude_domain:
                continue
            try:
                company = Company(
                    domain=domain,
                    name=item.get("name") or item.get("company_name"),
                    industry=item.get("industry"),
                    employee_count=item.get("number_of_employees") or item.get("employee_count"),
                    country=item.get("country"),
                )
                companies.append(company)
            except Exception as exc:
                logger.debug(f"Skipping malformed company record: {exc} — {item}")
                continue

        return companies
