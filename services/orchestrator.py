"""
services/orchestrator.py — Pipeline Orchestrator

The orchestrator is the heart of the system. It coordinates all four stages:
  Stage 1: Apollo.io  — find similar companies
  Stage 2: Prospeo    — find decision-makers per company
  Stage 3: Prospeo    — resolve name+domain → work email (replaces EazyReach)
  Stage 4: Brevo      — send personalised outreach

Key design decisions:
  • asyncio.gather with semaphores for controlled parallelism
  • Per-company failure isolation (one bad company doesn't stop the run)
  • Resumable checkpoints after each expensive stage
  • Deduplication between stages

Interview talking point:
  "The orchestrator separates 'what to do' from 'how to do it'.
   Each stage delegates to a client; the orchestrator only cares
   about ordering, error isolation, and data hand-off."
"""

import asyncio
import csv
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from rich.progress import Progress, TaskID

from clients.apollo_client import ApolloClient
from clients.brevo_client import BrevoClient
from clients.prospeo_client import ProspeoClient
from config.settings import Settings
from models.pipeline import Company, Contact, Lead
from services.email_generator import EmailGeneratorService
from utils.dedup import deduplicate_companies, deduplicate_contacts, deduplicate_leads
from utils.exceptions import (
    ApolloError,
    BrevoError,
    CircuitOpenError,
    PipelineError,
    ProspeoError,
)
from utils.logger import get_logger
from utils.metrics import PipelineMetrics
from utils.resume import ResumableRun

logger = get_logger(__name__)


@dataclass
class PipelineRunResult:
    leads: list[Lead]
    metrics: PipelineMetrics
    failures: list[dict]
    seed_domain: str


@dataclass
class SendResult:
    sent: int
    failed: int


class PipelineOrchestrator:
    def __init__(
        self,
        settings: Settings,
        progress: Progress,
        resumable: ResumableRun,
        max_companies: int = 25,
        mock_enrich: bool = False,
    ) -> None:
        self._settings = settings
        self._progress = progress
        self._resumable = resumable
        self._max_companies = max_companies
        self._mock_enrich = mock_enrich
        self._metrics = PipelineMetrics()
        self._failures: list[dict] = []
        # Semaphore limits concurrent API calls to avoid hammering rate limits
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_requests)
        self._email_gen = EmailGeneratorService(settings)

    # ── Public interface ──────────────────────────────────────────────────────

    async def execute(self, seed_domain: str) -> PipelineRunResult:
        """Run stages 1-3 and return a PipelineRunResult (without sending emails)."""
        logger.info("Orchestrator: starting pipeline", extra={"seed": seed_domain})

        companies = await self._stage1_find_companies(seed_domain)
        contacts = await self._stage2_find_contacts(companies)

        # Delay before Stage 3 to respect Prospeo rate limits
        delay = self._settings.prospeo_enrich_delay_seconds
        if contacts and delay > 0:
            logger.info(
                f"Pausing {delay}s before email enrichment to respect rate limits"
            )
            await asyncio.sleep(delay)

        leads = await self._stage3_resolve_emails(contacts)

        logger.info(
            "Orchestrator: stages 1-3 complete",
            extra={
                "companies": len(companies),
                "contacts": len(contacts),
                "leads": len(leads),
            },
        )
        return PipelineRunResult(
            leads=leads,
            metrics=self._metrics,
            failures=self._failures,
            seed_domain=seed_domain,
        )

    async def send_emails(self, leads: list[Lead]) -> SendResult:
        """Stage 4: generate copy and send emails for all verified leads."""
        return await self._stage4_send_emails(leads)

    # ── Stage implementations ──────────────────────────────────────────────────

    async def _stage1_find_companies(self, seed_domain: str) -> list[Company]:
        stage = self._metrics.add_stage("[1/4] Apollo.io — Find similar companies")
        stage.start()
        task: TaskID = self._progress.add_task(
            "[cyan][1/4] Apollo.io[/cyan]  Finding similar companies…",
            total=1,
        )

        # Check resume checkpoint
        if self._resumable.get_stage_done("apollo"):
            logger.info("Stage 1: resuming from checkpoint")
            raw = self._resumable.get_stage_data("apollo")
            companies = [Company(**c) for c in raw]
            self._progress.update(task, completed=1)
            stage.input_count = 1
            stage.output_count = len(companies)
            stage.finish()
            self._metrics.companies_found = len(companies)
            return companies

        try:
            async with ApolloClient(self._settings) as client:
                companies = await client.find_similar_companies(
                    seed_domain=seed_domain,
                    limit=self._max_companies,
                )
        except (ApolloError, PipelineError) as exc:
            logger.error(f"Stage 1 failed: {exc}")
            self._record_failure("apollo", str(exc))
            stage.success = False
            stage.finish()
            self._progress.update(task, completed=1)
            return []

        companies = deduplicate_companies(companies)
        self._resumable.mark_stage_done("apollo", [c.model_dump() for c in companies])

        stage.input_count = 1
        stage.output_count = len(companies)
        stage.finish()
        self._metrics.companies_found = len(companies)
        self._progress.update(task, completed=1, total=1)
        logger.info(f"Stage 1 complete — {len(companies)} companies")
        return companies

    async def _stage2_find_contacts(self, companies: list[Company]) -> list[Contact]:
        if not companies:
            return []

        stage = self._metrics.add_stage("[2/4] Prospeo — Find decision-makers")
        stage.start()
        stage.input_count = len(companies)
        task = self._progress.add_task(
            "[magenta][2/4] Prospeo[/magenta]  Finding decision-makers…",
            total=len(companies),
        )

        # Check full-stage resume checkpoint
        if self._resumable.get_stage_done("prospeo"):
            logger.info("Stage 2: resuming from full-stage checkpoint")
            raw = self._resumable.get_stage_data("prospeo")
            contacts = [Contact(**c) for c in raw]
            self._progress.update(task, completed=len(companies))
            stage.output_count = len(contacts)
            stage.finish()
            self._metrics.contacts_found = len(contacts)
            return contacts

        # Check per-item checkpoint — skip companies already processed
        pending_companies = [
            c for c in companies
            if not self._resumable.is_item_processed("prospeo", c.domain)
        ]
        if len(pending_companies) < len(companies):
            logger.info(
                f"Stage 2: resuming {len(pending_companies)}/{len(companies)} companies"
            )
            all_contacts = [
                Contact(**c)
                for c in self._resumable.get_item_results("prospeo")
            ]
            self._progress.update(task, completed=len(companies) - len(pending_companies))
        else:
            all_contacts = []

        async def _fetch_for_company(company: Company) -> None:
            """Isolated per-company fetch — one company failing doesn't stop others."""
            async with self._semaphore:
                try:
                    async with ProspeoClient(self._settings) as client:
                        company_contacts = await client.search_decision_makers(
                            domain=company.domain,
                            limit=self._settings.max_contacts_per_company,
                        )
                except (ProspeoError, CircuitOpenError) as exc:
                    logger.warning(f"Stage 2: failed for {company.domain}: {exc}")
                    self._record_failure("prospeo", str(exc), context=company.domain)
                    company_contacts = []
                except Exception as exc:
                    logger.error(f"Stage 2: unexpected error for {company.domain}: {exc}")
                    company_contacts = []

            # Checkpoint per company immediately
            for contact in company_contacts:
                all_contacts.append(contact)
            self._resumable.mark_item_processed(
                "prospeo", company.domain,
                data=[c.model_dump() for c in company_contacts]
            )
            self._progress.advance(task)

        # Run all pending company lookups in parallel, bounded by semaphore
        tasks = [_fetch_for_company(c) for c in pending_companies]
        await asyncio.gather(*tasks)

        all_contacts = deduplicate_contacts(all_contacts)
        # Clear per-item checkpoint and save full stage checkpoint
        self._resumable.clear_item_checkpoint("prospeo")
        self._resumable.mark_stage_done("prospeo", [c.model_dump() for c in all_contacts])

        stage.output_count = len(all_contacts)
        stage.finish()
        self._metrics.contacts_found = len(all_contacts)
        logger.info(f"Stage 2 complete — {len(all_contacts)} unique contacts")
        return all_contacts

    async def _stage3_resolve_emails(self, contacts: list[Contact]) -> list[Lead]:
        if not contacts:
            return []

        stage = self._metrics.add_stage("[3/4] Prospeo — Bulk enrich emails")
        stage.start()
        stage.input_count = len(contacts)
        task = self._progress.add_task(
            "[yellow][3/4] Prospeo[/yellow]  Resolving work emails…",
            total=len(contacts),
        )

        # Check full-stage resume checkpoint
        if self._resumable.get_stage_done("prospeo_email"):
            logger.info("Stage 3: resuming from full-stage checkpoint")
            raw = self._resumable.get_stage_data("prospeo_email")
            leads = [Lead(**l) for l in raw]
            self._progress.update(task, completed=len(contacts))
            stage.output_count = len(leads)
            stage.finish()
            self._metrics.verified_emails = len(leads)
            return leads

        # Separate contacts that already have email from Stage 2
        contacts_with_email = [c for c in contacts if c.email]
        contacts_needing_email = [c for c in contacts if not c.email and c.person_id]

        leads: list[Lead] = []
        for contact in contacts_with_email:
            leads.append(Lead(contact=contact, email=contact.email))

        # Bulk enrich all contacts needing emails in one (or few) API calls
        if contacts_needing_email:
            try:
                if self._mock_enrich:
                    # Demo mode: synthesise emails without hitting Prospeo rate limits
                    logger.info("Stage 3: MOCK enrich — generating synthetic emails for demo")
                    email_map = {
                        c.person_id: f"{c.name.lower().replace(' ', '.')}@{c.company_domain}"
                        for c in contacts_needing_email
                        if c.person_id
                    }
                else:
                    async with ProspeoClient(self._settings) as client:
                        email_map = await client.bulk_enrich_emails(contacts_needing_email)

                for contact in contacts_needing_email:
                    email = email_map.get(contact.person_id)
                    if email:
                        lead = Lead(contact=contact, email=email)
                        leads.append(lead)
                    self._progress.advance(task)
            except (ProspeoError, CircuitOpenError) as exc:
                logger.warning(f"Stage 3: bulk enrich failed: {exc}")
                self._record_failure("prospeo_email", str(exc))
                # Advance progress for all pending contacts
                for _ in contacts_needing_email:
                    self._progress.advance(task)
            except Exception as exc:
                logger.error(f"Stage 3: unexpected error: {exc}")
                for _ in contacts_needing_email:
                    self._progress.advance(task)
        else:
            self._progress.update(task, completed=len(contacts))

        leads = deduplicate_leads(leads)
        # Only checkpoint Stage 3 if we actually got some emails.
        # If bulk enrich failed, we want resume to retry Stage 3.
        if leads:
            self._resumable.mark_stage_done(
                "prospeo_email",
                [l.model_dump(mode="json") for l in leads],
            )

        stage.output_count = len(leads)
        stage.finish()
        self._metrics.verified_emails = len(leads)
        logger.info(f"Stage 3 complete — {len(leads)} verified leads")
        return leads

    async def _stage4_send_emails(self, leads: list[Lead]) -> SendResult:
        stage = self._metrics.add_stage("[4/4] Brevo — Send outreach emails")
        stage.start()
        stage.input_count = len(leads)
        task = self._progress.add_task(
            "[green][4/4] Brevo[/green]  Sending outreach emails…",
            total=len(leads),
        )

        sent, failed = 0, 0

        async def _send_one(lead: Lead) -> bool:
            nonlocal sent, failed
            async with self._semaphore:
                try:
                    # Generate personalised copy via OpenAI
                    subject, body = await self._email_gen.generate(
                        name=lead.contact.name,
                        title=lead.contact.title,
                        company=lead.contact.company_domain,
                    )
                    lead.email_subject = subject
                    lead.email_body = body

                    async with BrevoClient(self._settings) as client:
                        result = await client.send_email(
                            to_email=lead.email,
                            to_name=lead.contact.name,
                            subject=subject,
                            html_body=body,
                        )
                    lead.email_sent = result.success
                    if result.success:
                        logger.info(
                            "Email sent",
                            extra={
                                "to": lead.email,
                                "contact": lead.contact.name,
                                "message_id": result.message_id,
                            },
                        )
                        return True
                    else:
                        logger.warning(
                            "Email failed", extra={"to": lead.email}
                        )
                        return False

                except (BrevoError, CircuitOpenError) as exc:
                    lead.email_send_error = str(exc)
                    logger.error(f"Stage 4: send failed for {lead.email}: {exc}")
                    self._record_failure("brevo", str(exc), context=lead.email)
                    return False
                except Exception as exc:
                    lead.email_send_error = str(exc)
                    logger.error(f"Stage 4: unexpected error for {lead.email}: {exc}")
                    return False

        tasks = [_send_one(lead) for lead in leads]
        for coro in asyncio.as_completed(tasks):
            success = await coro
            if success:
                sent += 1
            else:
                failed += 1
            self._progress.advance(task)

        stage.output_count = sent
        stage.finish()
        logger.info(
            f"Stage 4 complete — {sent} sent, {failed} failed"
        )
        return SendResult(sent=sent, failed=failed)

    # ── Output helpers ─────────────────────────────────────────────────────────

    def write_csv(self, leads: list[Lead], path: Path) -> None:
        """Write all leads to a CSV report."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            "company", "contact", "title", "linkedin", "email",
            "email_subject", "email_body",
            "email_sent", "timestamp",
        ]
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for lead in leads:
                writer.writerow(lead.to_csv_row())
        logger.info(f"CSV report written: {path} ({len(leads)} rows)")

    def write_json(self, leads: list[Lead], path: Path) -> None:
        """Write all leads to a JSON export."""
        path.parent.mkdir(parents=True, exist_ok=True)
        data = [l.model_dump(mode="json") for l in leads]
        path.write_text(json.dumps(data, indent=2, default=str))
        logger.info(f"JSON export written: {path}")

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _record_failure(
        self, stage: str, error: str, context: Optional[str] = None
    ) -> None:
        entry = {"stage": stage, "error": error}
        if context:
            entry["context"] = context
        self._failures.append(entry)
