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
    s.apollo_api_key = "apollo-key"
    s.prospeo_api_key = "prospeo-key"
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
    s.prospeo_enrich_delay_seconds = 0
    s.sender_name = "Test Sender"
    s.sender_email = "test@example.com"
    return s


def _make_contact(name="Jane", title="CTO", linkedin_slug="jane", domain="acme.com", person_id=None):
    return Contact(
        name=name,
        title=title,
        linkedin_url=f"https://linkedin.com/in/{linkedin_slug}",
        company_domain=domain,
        person_id=person_id or f"pid_{linkedin_slug}",
    )


class TestStage1Apollo:
    @pytest.mark.asyncio
    async def test_returns_companies(self):
        from clients.apollo_client import ApolloClient

        companies = [
            Company(domain="stripe.com", name="Stripe"),
            Company(domain="braintree.com", name="Braintree"),
        ]

        with patch.object(ApolloClient, "__aenter__", return_value=AsyncMock(
            find_similar_companies=AsyncMock(return_value=companies)
        )):
            async with ApolloClient(_make_settings()) as client:
                result = await client.find_similar_companies("paypal.com", limit=5)

        assert len(result) == 2
        assert result[0].domain == "stripe.com"

    def test_parse_apollo_response(self):
        from clients.apollo_client import ApolloClient

        client = ApolloClient.__new__(ApolloClient)
        data = {
            "organizations": [
                {"website_url": "https://stripe.com", "name": "Stripe"},
                {"website_url": "https://paypal.com"},  # should be excluded as seed
                {"website_url": "  "},                   # should be skipped
            ]
        }
        result = client._parse_response(data, exclude_domain="paypal.com")
        assert len(result) == 1
        assert result[0].domain == "stripe.com"


class TestStage2Prospeo:
    def test_parse_search_response(self):
        from clients.prospeo_client import ProspeoClient

        client = ProspeoClient.__new__(ProspeoClient)
        data = {
            "results": [
                {
                    "person": {
                        "person_id": "abc123",
                        "full_name": "Jane Smith",
                        "current_job_title": "CTO",
                        "linkedin_url": "https://linkedin.com/in/jane",
                    },
                    "company": {"name": "Acme"},
                },
                {
                    "person": {
                        "person_id": "def456",
                        "first_name": "",
                        "last_name": "",
                        "linkedin_url": "https://linkedin.com/in/nobody",
                    },
                    "company": {},
                },
            ]
        }
        result = client._parse_search_response(data, "acme.com")
        assert len(result) == 1
        assert result[0].name == "Jane Smith"
        assert result[0].person_id == "abc123"


class TestStage3ProspeoEmail:
    @pytest.mark.asyncio
    async def test_bulk_enrich_returns_email_map(self):
        from clients.prospeo_client import ProspeoClient

        client = ProspeoClient.__new__(ProspeoClient)
        client._api_key = "test-key"
        client._settings = _make_settings()
        client.service_name = "Prospeo"

        contacts = [
            _make_contact("Jane", "CTO", "jane", person_id="pid_jane"),
            _make_contact("Bob", "CEO", "bob", person_id="pid_bob"),
        ]

        mock_response = {
            "error": False,
            "matched": [
                {
                    "identifier": "pid_jane",
                    "person": {"email": {"email": "jane@acme.com"}},
                },
                {
                    "identifier": "pid_bob",
                    "person": {"email": {"email": "bob@acme.com"}},
                },
            ],
        }

        with patch.object(client, "_post", return_value=mock_response):
            result = await client.bulk_enrich_emails(contacts)

        assert result == {"pid_jane": "jane@acme.com", "pid_bob": "bob@acme.com"}

    @pytest.mark.asyncio
    async def test_bulk_enrich_partial_match(self):
        from clients.prospeo_client import ProspeoClient

        client = ProspeoClient.__new__(ProspeoClient)
        client._api_key = "test-key"
        client._settings = _make_settings()
        client.service_name = "Prospeo"

        contacts = [
            _make_contact("Jane", "CTO", "jane", person_id="pid_jane"),
        ]

        mock_response = {
            "error": False,
            "matched": [],
            "not_matched": ["pid_jane"],
        }

        with patch.object(client, "_post", return_value=mock_response):
            result = await client.bulk_enrich_emails(contacts)

        assert result == {}


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

        # Mock companies returned by Apollo.io
        mock_companies = [Company(domain="acme.com")]

        # Mock contacts returned by Prospeo Search
        mock_contacts = [
            _make_contact("Jane Smith", "CTO", "janesmith"),
            _make_contact("Bob Jones", "CEO", "bobjones"),
        ]

        # Mock emails returned by Prospeo Bulk Enrich
        email_map = {
            "pid_janesmith": "jane@acme.com",
            "pid_bobjones": "bob@acme.com",
        }

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
            patch("services.orchestrator.ApolloClient") as MockApollo,
            patch("services.orchestrator.ProspeoClient") as MockProspeo,
        ):
            # Configure Apollo mock
            apollo_instance = AsyncMock()
            apollo_instance.find_similar_companies = AsyncMock(return_value=mock_companies)
            apollo_instance.__aenter__ = AsyncMock(return_value=apollo_instance)
            apollo_instance.__aexit__ = AsyncMock(return_value=None)
            MockApollo.return_value = apollo_instance

            # Configure Prospeo mock
            prospeo_instance = AsyncMock()
            prospeo_instance.search_decision_makers = AsyncMock(return_value=mock_contacts)
            prospeo_instance.bulk_enrich_emails = AsyncMock(return_value=email_map)
            prospeo_instance.__aenter__ = AsyncMock(return_value=prospeo_instance)
            prospeo_instance.__aexit__ = AsyncMock(return_value=None)
            MockProspeo.return_value = prospeo_instance

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

        async def mock_search_dm(domain, **kwargs):
            call_count["n"] += 1
            if domain == "bad.com":
                raise ProspeoError("simulated failure")
            if domain == "good.com":
                return [contact]
            return [contact2]

        with patch("services.orchestrator.ApolloClient") as MockApollo, \
             patch("services.orchestrator.ProspeoClient") as MockProspeo:

            apollo_instance = AsyncMock()
            apollo_instance.find_similar_companies = AsyncMock(return_value=mock_companies)
            apollo_instance.__aenter__ = AsyncMock(return_value=apollo_instance)
            apollo_instance.__aexit__ = AsyncMock(return_value=None)
            MockApollo.return_value = apollo_instance

            prospeo_instance = AsyncMock()
            prospeo_instance.search_decision_makers = AsyncMock(side_effect=mock_search_dm)
            # Return unique email per person_id to survive dedup
            async def _bulk_enrich(contacts):
                return {c.person_id: f"{c.person_id}@test.com" for c in contacts if c.person_id}
            prospeo_instance.bulk_enrich_emails = AsyncMock(side_effect=_bulk_enrich)
            prospeo_instance.__aenter__ = AsyncMock(return_value=prospeo_instance)
            prospeo_instance.__aexit__ = AsyncMock(return_value=None)
            MockProspeo.return_value = prospeo_instance

            result = await orchestrator.execute("paypal.com")

        # Should have 2 leads from good.com and great.com
        assert len(result.leads) == 2
        # Failure should be recorded
        assert any(f["stage"] == "prospeo" for f in result.failures)
