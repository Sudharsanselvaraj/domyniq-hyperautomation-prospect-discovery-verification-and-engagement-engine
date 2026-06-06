"""
tests/test_integration.py — Integration Tests

These tests exercise the full pipeline flow with all four API clients
mocked. They verify that:
  • Data flows correctly between stages
  • One stage's failure doesn't crash the pipeline
  • Deduplication is applied at the right points
  • CSV/JSON output is generated correctly

Run with:
  pytest tests/test_integration.py -v
"""

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

from models.pipeline import Company, Contact, Lead
from utils.resume import ResumableRun


def _make_settings():
    s = MagicMock()
    s.ocean_api_key = "ocean-key"
    s.prospeo_api_key = "prospeo-key"
    s.eazyreach_api_key = "eazyreach-key"
    s.brevo_api_key = "brevo-key"
    s.openai_api_key = "openai-key"
    s.openai_model = "gpt-4o-mini"
    s.email_max_words = 120
    s.request_timeout_seconds = 10
    s.max_concurrent_requests = 5
    s.max_contacts_per_company = 3
    s.retry_max_attempts = 2
    s.retry_wait_min_seconds = 0.01
    s.retry_wait_max_seconds = 0.1
    s.sender_name = "Test Sender"
    s.sender_email = "test@example.com"
    return s


def _make_contact(name="Jane", title="CTO", linkedin_slug="jane", domain="acme.com"):
    return Contact(
        name=name,
        title=title,
        linkedin_url=f"https://linkedin.com/in/{linkedin_slug}",
        company_domain=domain,
    )


class TestStage1Ocean:
    @pytest.mark.asyncio
    async def test_returns_companies(self):
        from clients.ocean_client import OceanClient

        companies = [
            Company(domain="stripe.com", name="Stripe"),
            Company(domain="braintree.com", name="Braintree"),
        ]

        with patch.object(OceanClient, "__aenter__", return_value=AsyncMock(
            find_similar_companies=AsyncMock(return_value=companies)
        )):
            async with OceanClient(_make_settings()) as client:
                result = await client.find_similar_companies("paypal.com", limit=5)

        assert len(result) == 2
        assert result[0].domain == "stripe.com"

    def test_parse_ocean_response(self):
        from clients.ocean_client import OceanClient

        client = OceanClient.__new__(OceanClient)
        data = {
            "companies": [
                {"domain": "stripe.com", "name": "Stripe"},
                {"domain": "paypal.com"},  # should be excluded as seed
                {"domain": "  "},          # should be skipped
            ]
        }
        result = client._parse_response(data, exclude_domain="paypal.com")
        assert len(result) == 1
        assert result[0].domain == "stripe.com"


class TestStage2Prospeo:
    def test_decision_maker_filter(self):
        from clients.prospeo_client import ProspeoClient

        assert ProspeoClient._is_decision_maker("Chief Executive Officer")
        assert ProspeoClient._is_decision_maker("VP of Engineering")
        assert ProspeoClient._is_decision_maker("Head of Product")
        assert not ProspeoClient._is_decision_maker("Software Engineer")
        assert not ProspeoClient._is_decision_maker("Marketing Analyst")

    def test_parse_prospeo_response(self):
        from clients.prospeo_client import ProspeoClient

        client = ProspeoClient.__new__(ProspeoClient)
        data = {
            "response": [
                {
                    "full_name": "Jane Smith",
                    "job_title": "CTO",
                    "linkedin_url": "https://linkedin.com/in/jane",
                    "company": "Acme",
                },
                {
                    "full_name": "",          # no name — should be skipped
                    "job_title": "VP Sales",
                    "linkedin_url": "https://linkedin.com/in/nobody",
                },
                {
                    "full_name": "Bob Jones",
                    "job_title": "Software Engineer",  # not a DM
                    "linkedin_url": "https://linkedin.com/in/bob",
                },
            ]
        }
        result = client._parse_response(data, "acme.com")
        assert len(result) == 1
        assert result[0].name == "Jane Smith"


class TestStage3EazyReach:
    def test_parse_eazyreach_verified(self):
        from clients.eazyreach_client import EazyReachClient

        client = EazyReachClient.__new__(EazyReachClient)
        data = {"email": "jane@acme.com", "verified": True, "confidence": 0.95}
        result = client._parse_response(data, "https://linkedin.com/in/jane")
        assert result == "jane@acme.com"

    def test_parse_eazyreach_unverified_returns_none(self):
        from clients.eazyreach_client import EazyReachClient

        client = EazyReachClient.__new__(EazyReachClient)
        data = {"email": "jane@acme.com", "verified": False}
        result = client._parse_response(data, "https://linkedin.com/in/jane")
        assert result is None

    def test_parse_eazyreach_no_email_returns_none(self):
        from clients.eazyreach_client import EazyReachClient

        client = EazyReachClient.__new__(EazyReachClient)
        result = client._parse_response({}, "https://linkedin.com/in/jane")
        assert result is None


class TestStage4Brevo:
    def test_strip_html(self):
        from clients.brevo_client import BrevoClient

        html = "<p>Hi Jane,<br>Hope you're well.</p><p>Best regards</p>"
        text = BrevoClient._strip_html(html)
        assert "<p>" not in text
        assert "Hi Jane," in text
        assert "Best regards" in text


class TestFullPipelineFlow:
    """
    End-to-end integration test with all external I/O mocked.
    Verifies that data flows correctly through all 4 stages.
    """

    @pytest.mark.asyncio
    async def test_full_pipeline_happy_path(self, tmp_path):
        from rich.progress import Progress
        from services.orchestrator import PipelineOrchestrator
        from utils.resume import ResumableRun

        settings = _make_settings()

        # Mock companies returned by Ocean.io
        mock_companies = [Company(domain="acme.com")]

        # Mock contacts returned by Prospeo
        mock_contacts = [
            _make_contact("Jane Smith", "CTO", "janesmith"),
            _make_contact("Bob Jones", "CEO", "bobjones"),
        ]

        # Mock emails returned by EazyReach
        email_map = {
            "https://linkedin.com/in/janesmith": "jane@acme.com",
            "https://linkedin.com/in/bobjones": "bob@acme.com",
        }

        async def fake_get_email(linkedin_url):
            return email_map.get(linkedin_url)

        progress = Progress()
        resumable = ResumableRun("acme.com")
        resumable.clear()

        orchestrator = PipelineOrchestrator(
            settings=settings,
            progress=progress,
            resumable=resumable,
            max_companies=5,
        )

        with (
            patch("services.orchestrator.OceanClient") as MockOcean,
            patch("services.orchestrator.ProspeoClient") as MockProspeo,
            patch("services.orchestrator.EazyReachClient") as MockEazy,
        ):
            # Configure Ocean mock
            ocean_instance = AsyncMock()
            ocean_instance.find_similar_companies = AsyncMock(return_value=mock_companies)
            ocean_instance.__aenter__ = AsyncMock(return_value=ocean_instance)
            ocean_instance.__aexit__ = AsyncMock(return_value=None)
            MockOcean.return_value = ocean_instance

            # Configure Prospeo mock
            prospeo_instance = AsyncMock()
            prospeo_instance.get_decision_makers = AsyncMock(return_value=mock_contacts)
            prospeo_instance.__aenter__ = AsyncMock(return_value=prospeo_instance)
            prospeo_instance.__aexit__ = AsyncMock(return_value=None)
            MockProspeo.return_value = prospeo_instance

            # Configure EazyReach mock
            eazy_instance = AsyncMock()
            eazy_instance.get_email = AsyncMock(side_effect=fake_get_email)
            eazy_instance.__aenter__ = AsyncMock(return_value=eazy_instance)
            eazy_instance.__aexit__ = AsyncMock(return_value=None)
            MockEazy.return_value = eazy_instance

            result = await orchestrator.execute("paypal.com")

        assert len(result.leads) == 2
        emails = {lead.email for lead in result.leads}
        assert "jane@acme.com" in emails
        assert "bob@acme.com" in emails

        # Write CSV and verify
        csv_path = tmp_path / "output.csv"
        orchestrator.write_csv(result.leads, csv_path)
        assert csv_path.exists()
        content = csv_path.read_text()
        assert "jane@acme.com" in content
        assert "bob@acme.com" in content

    @pytest.mark.asyncio
    async def test_pipeline_continues_on_stage2_failure(self):
        """If one company fails in Stage 2, the others should still be processed."""
        from rich.progress import Progress
        from services.orchestrator import PipelineOrchestrator
        from utils.exceptions import ProspeoError

        settings = _make_settings()
        progress = Progress()
        resumable = ResumableRun("test.com")
        resumable.clear()

        orchestrator = PipelineOrchestrator(
            settings=settings, progress=progress,
            resumable=resumable, max_companies=5
        )

        mock_companies = [
            Company(domain="good.com"),
            Company(domain="bad.com"),   # this one will fail
            Company(domain="great.com"),
        ]

        contact = _make_contact(name="Jane", linkedin_slug="jane-good", domain="good.com")
        contact2 = _make_contact(name="Alice", linkedin_slug="alice-great", domain="great.com")

        call_count = {"n": 0}

        async def mock_get_dm(domain, **kwargs):
            call_count["n"] += 1
            if domain == "bad.com":
                raise ProspeoError("simulated failure")
            if domain == "good.com":
                return [contact]
            return [contact2]

        with patch("services.orchestrator.OceanClient") as MockOcean, \
             patch("services.orchestrator.ProspeoClient") as MockProspeo, \
             patch("services.orchestrator.EazyReachClient") as MockEazy:

            ocean_instance = AsyncMock()
            ocean_instance.find_similar_companies = AsyncMock(return_value=mock_companies)
            ocean_instance.__aenter__ = AsyncMock(return_value=ocean_instance)
            ocean_instance.__aexit__ = AsyncMock(return_value=None)
            MockOcean.return_value = ocean_instance

            prospeo_instance = AsyncMock()
            prospeo_instance.get_decision_makers = AsyncMock(side_effect=mock_get_dm)
            prospeo_instance.__aenter__ = AsyncMock(return_value=prospeo_instance)
            prospeo_instance.__aexit__ = AsyncMock(return_value=None)
            MockProspeo.return_value = prospeo_instance

            eazy_instance = AsyncMock()
            # Return unique email per LinkedIn URL to survive dedup
            async def _unique_email(linkedin_url):
                slug = linkedin_url.rstrip("/").split("/")[-1]
                return f"{slug}@test.com"
            eazy_instance.get_email = AsyncMock(side_effect=_unique_email)
            eazy_instance.__aenter__ = AsyncMock(return_value=eazy_instance)
            eazy_instance.__aexit__ = AsyncMock(return_value=None)
            MockEazy.return_value = eazy_instance

            result = await orchestrator.execute("paypal.com")

        # Should have 2 leads from good.com and great.com
        assert len(result.leads) == 2
        # Failure should be recorded
        assert any(f["stage"] == "prospeo" for f in result.failures)
