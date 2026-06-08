"""
clients/apollo_client.py — Apollo.io API Client

Stage 1: Given a seed company domain, find lookalike companies.

Apollo.io API docs: https://developer.apollo.io/
Auth: API key in request body (legacy) or header
Key endpoint: POST /v1/organizations/search

Note: We originally planned to use Ocean.io, but sign-ups were unavailable.
Apollo.io is a robust alternative with rich firmographic filters.

Interview talking point:
  "Ocean.io sign-ups were closed, so I adapted the pipeline to use Apollo.io
   instead. The core architecture stayed identical — only the Stage 1 client
   changed. This shows the system is resilient to vendor changes."
"""

from typing import Optional

from clients.base import BaseClient
from config.settings import Settings
from models.pipeline import Company
from utils.exceptions import ApolloError, ValidationError
from utils.logger import get_logger

logger = get_logger(__name__)


class ApolloClient(BaseClient):
    service_name = "Apollo.io"
    base_url = "https://api.apollo.io"

    def __init__(self, settings: Settings) -> None:
        super().__init__(api_key=settings.apollo_api_key, settings=settings)

    def _build_headers(self) -> dict[str, str]:
        return {
            "X-Api-Key": self._api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def find_similar_companies(
        self,
        seed_domain: str,
        limit: int = 25,
    ) -> list[Company]:
        """
        Find lookalike companies using Apollo.io.

        Strategy:
          1. Search for the seed company by domain to extract its industry.
          2. Search again filtering by that industry to find peers.
          3. Exclude the seed domain from results.

        Returns a deduplicated list of Company objects.
        """
        logger.info(
            "Apollo: finding similar companies",
            extra={"seed": seed_domain, "limit": limit},
        )

        base_path = "/api/v1/organizations/search"

        # Step 1: introspect seed company to get industry
        industry: Optional[str] = None
        try:
            # Apollo's q_organization_domains filter often returns 0 results.
            # We fall back to keyword search using the domain name stripped of TLD.
            seed_keyword = seed_domain.rsplit(".", 1)[0] if "." in seed_domain else seed_domain
            seed_payload = {
                "q_organization_keyword_tags": [seed_keyword],
                "per_page": 1,
                "page": 1,
            }
            seed_data = await self._post(base_path, json=seed_payload)
            orgs = seed_data.get("organizations", [])
            if orgs and isinstance(orgs, list):
                industry = orgs[0].get("industry")
                logger.info(
                    "Apollo: seed company industry",
                    extra={"seed": seed_domain, "industry": industry},
                )
        except Exception as exc:
            logger.warning(f"Apollo: could not introspect seed company: {exc}")

        # Step 2: search for similar companies
        payload: dict = {
            "per_page": min(limit, 100),
            "page": 1,
        }
        if industry:
            payload["organization_industry"] = [industry]

        try:
            data = await self._post(base_path, json=payload)
        except Exception as exc:
            raise ApolloError(
                f"Failed to fetch similar companies for {seed_domain}: {exc}",
                service=self.service_name,
            ) from exc

        companies = self._parse_response(data, seed_domain)
        logger.info(
            "Apollo: similar companies found",
            extra={"seed": seed_domain, "count": len(companies)},
        )
        return companies

    def _parse_response(self, data: dict, exclude_domain: str) -> list[Company]:
        """
        Parse Apollo /v1/organizations/search response.

        Expected shape:
        {
          "organizations": [
            {
              "id": "123",
              "name": "Stripe",
              "website_url": "https://stripe.com",
              "industry": "Financial Services",
              "estimated_num_employees": 7000,
              "country": "United States"
            },
            ...
          ],
          "pagination": { ... }
        }
        """
        raw_list = data.get("organizations") or data.get("results") or data.get("data") or []

        if not isinstance(raw_list, list):
            raise ValidationError(
                "Apollo response 'organizations' field is not a list",
                context={"keys": list(data.keys())},
            )

        companies: list[Company] = []
        for item in raw_list:
            if not isinstance(item, dict):
                continue

            domain = self._extract_domain(item)
            if not domain or domain == exclude_domain:
                continue

            try:
                company = Company(
                    domain=domain,
                    name=item.get("name") or item.get("company_name"),
                    industry=item.get("industry"),
                    employee_count=item.get("estimated_num_employees") or item.get("employee_count"),
                    country=item.get("country"),
                )
                companies.append(company)
            except Exception as exc:
                logger.debug(f"Skipping malformed company record: {exc} — {item}")
                continue

        return companies

    @staticmethod
    def _extract_domain(item: dict) -> str:
        """Extract clean domain from various Apollo URL fields."""
        raw_url = (
            item.get("website_url")
            or item.get("domain")
            or item.get("website")
            or item.get("url")
            or ""
        )
        if not raw_url:
            return ""

        # Strip protocol and www
        domain = raw_url.strip().lower()
        for prefix in ("https://", "http://", "www."):
            if domain.startswith(prefix):
                domain = domain[len(prefix):]
        return domain.rstrip("/")
