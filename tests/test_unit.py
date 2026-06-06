"""
tests/test_unit.py — Unit Tests

These tests mock all external API calls and focus on pure logic:
  • Pydantic model validation
  • Deduplication logic
  • Circuit breaker state machine
  • Email generation parsing fallback

Run with:
  pytest tests/test_unit.py -v
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from models.pipeline import Company, Contact, Lead
from utils.circuit_breaker import CircuitBreaker, CircuitState
from utils.dedup import deduplicate_companies, deduplicate_contacts, deduplicate_leads
from utils.exceptions import (
    CircuitOpenError,
    OceanError,
    ServiceUnavailableError,
    ValidationError,
)


# ── Model Tests ───────────────────────────────────────────────────────────────

class TestCompanyModel:
    def test_domain_normalisation(self):
        """Domains should be stripped of protocol and lowercased."""
        c = Company(domain="HTTPS://www.Stripe.COM/")
        assert c.domain == "stripe.com"

    def test_domain_with_path_stripped(self):
        c = Company(domain="https://stripe.com")
        assert c.domain == "stripe.com"

    def test_dedup_equality(self):
        c1 = Company(domain="stripe.com")
        c2 = Company(domain="stripe.com", name="Stripe Inc")
        assert c1 == c2
        assert len({c1, c2}) == 1

    def test_minimal_fields(self):
        c = Company(domain="example.com")
        assert c.name is None
        assert c.industry is None


class TestContactModel:
    def test_linkedin_url_normalised(self):
        """LinkedIn URL should be prefixed with https if missing."""
        c = Contact(
            name="Jane", title="CTO", linkedin_url="linkedin.com/in/jane",
            company_domain="acme.com"
        )
        assert c.linkedin_url.startswith("https://")

    def test_empty_linkedin_raises(self):
        with pytest.raises(Exception):
            Contact(name="Jane", title="CTO", linkedin_url="", company_domain="acme.com")

    def test_dedup_by_linkedin(self):
        url = "https://linkedin.com/in/jane"
        c1 = Contact(name="Jane Smith", title="CTO", linkedin_url=url, company_domain="acme.com")
        c2 = Contact(name="Jane Smith", title="CTO", linkedin_url=url, company_domain="other.com")
        assert c1 == c2
        assert len({c1, c2}) == 1


class TestLeadModel:
    def test_email_normalised(self):
        contact = Contact(
            name="Jane", title="CEO", linkedin_url="https://linkedin.com/in/jane",
            company_domain="acme.com"
        )
        lead = Lead(contact=contact, email=" Jane@Acme.COM ")
        assert lead.email == "jane@acme.com"

    def test_invalid_email_raises(self):
        contact = Contact(
            name="Jane", title="CEO", linkedin_url="https://linkedin.com/in/jane",
            company_domain="acme.com"
        )
        with pytest.raises(Exception):
            Lead(contact=contact, email="not-an-email")

    def test_to_csv_row_keys(self):
        contact = Contact(
            name="Jane", title="CEO", linkedin_url="https://linkedin.com/in/jane",
            company_domain="acme.com"
        )
        lead = Lead(contact=contact, email="jane@acme.com")
        row = lead.to_csv_row()
        assert set(row.keys()) == {
            "company", "contact", "title", "linkedin", "email", "email_sent", "timestamp"
        }


# ── Deduplication Tests ───────────────────────────────────────────────────────

class TestDeduplication:
    def test_dedup_companies(self):
        companies = [
            Company(domain="stripe.com"),
            Company(domain="stripe.com", name="Stripe"),
            Company(domain="shopify.com"),
        ]
        result = deduplicate_companies(companies)
        assert len(result) == 2
        assert result[0].domain == "stripe.com"

    def test_dedup_contacts_by_linkedin(self):
        url = "https://linkedin.com/in/jane"
        contacts = [
            Contact(name="Jane", title="CTO", linkedin_url=url, company_domain="a.com"),
            Contact(name="Jane Smith", title="CTO", linkedin_url=url, company_domain="b.com"),
            Contact(
                name="Bob", title="CEO",
                linkedin_url="https://linkedin.com/in/bob",
                company_domain="a.com",
            ),
        ]
        result = deduplicate_contacts(contacts)
        assert len(result) == 2

    def test_dedup_leads_by_email(self):
        def make_lead(email: str) -> Lead:
            contact = Contact(
                name="Test", title="CEO",
                linkedin_url=f"https://linkedin.com/in/{email.split('@')[0]}",
                company_domain="test.com"
            )
            return Lead(contact=contact, email=email)

        leads = [
            make_lead("jane@acme.com"),
            make_lead("jane@acme.com"),  # duplicate
            make_lead("bob@acme.com"),
        ]
        result = deduplicate_leads(leads)
        assert len(result) == 2

    def test_dedup_preserves_order(self):
        domains = ["c.com", "a.com", "b.com", "a.com"]
        companies = [Company(domain=d) for d in domains]
        result = deduplicate_companies(companies)
        assert [c.domain for c in result] == ["c.com", "a.com", "b.com"]


# ── Circuit Breaker Tests ─────────────────────────────────────────────────────

class TestCircuitBreaker:
    def _make_breaker(self) -> CircuitBreaker:
        return CircuitBreaker(
            name="test-service",
            failure_threshold=3,
            recovery_timeout=60.0,
            expected_exception=ServiceUnavailableError,
        )

    @pytest.mark.asyncio
    async def test_closed_on_success(self):
        cb = self._make_breaker()
        async def ok():
            return "ok"
        result = await cb.call(ok)
        assert result == "ok"
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_opens_after_threshold(self):
        cb = self._make_breaker()
        async def fail():
            raise ServiceUnavailableError("boom")

        for _ in range(3):
            try:
                await cb.call(fail)
            except ServiceUnavailableError:
                pass

        assert cb.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_open_raises_circuit_error(self):
        cb = self._make_breaker()
        async def fail():
            raise ServiceUnavailableError("boom")

        # Trigger OPEN
        for _ in range(3):
            try:
                await cb.call(fail)
            except ServiceUnavailableError:
                pass

        # Next call should fast-fail with CircuitOpenError
        with pytest.raises(CircuitOpenError):
            await cb.call(fail)

    def test_manual_reset(self):
        cb = self._make_breaker()
        cb._failure_count = 10
        cb._state = CircuitState.OPEN
        cb.reset()
        assert cb.state == CircuitState.CLOSED
        assert cb._failure_count == 0


# ── Email Generator Tests ─────────────────────────────────────────────────────

class TestEmailGenerator:
    def _make_settings(self):
        s = MagicMock()
        s.openai_api_key = "sk-test"
        s.openai_model = "gpt-4o-mini"
        s.email_max_words = 120
        s.request_timeout_seconds = 30
        return s

    @pytest.mark.asyncio
    async def test_generate_returns_subject_and_body(self):
        from services.email_generator import EmailGeneratorService

        settings = self._make_settings()
        gen = EmailGeneratorService(settings)

        mock_response_json = {
            "choices": [{
                "message": {
                    "content": '{"subject": "Quick question", "body": "Hi Jane, nice to meet you."}'
                }
            }]
        }

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_response = MagicMock()
            mock_response.json.return_value = mock_response_json
            mock_response.raise_for_status = MagicMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_class.return_value = mock_client

            subject, body = await gen.generate(
                name="Jane Smith", title="CTO", company="acme.com"
            )

        assert subject == "Quick question"
        assert "Jane" in body or "<p>" in body

    @pytest.mark.asyncio
    async def test_fallback_on_bad_json(self):
        from services.email_generator import EmailGeneratorService

        settings = self._make_settings()
        gen = EmailGeneratorService(settings)

        mock_response_json = {
            "choices": [{"message": {"content": "this is not json"}}]
        }

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_response = MagicMock()
            mock_response.json.return_value = mock_response_json
            mock_response.raise_for_status = MagicMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_class.return_value = mock_client

            subject, body = await gen.generate(
                name="Bob Jones", title="CEO", company="example.com"
            )

        assert subject  # fallback subject present
        assert body     # fallback body present
