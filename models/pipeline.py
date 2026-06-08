"""
models/pipeline.py — Pydantic data models

Using Pydantic v2 for:
  • Runtime type validation (catches malformed API responses early)
  • Automatic serialization to dict/JSON for CSV/JSON export
  • Self-documenting data contracts between stages

Interview talking point:
  "Each stage communicates through typed Pydantic models.
   This acts as a contract between stages — if Ocean.io changes
   its response shape, the validator raises immediately instead
   of silently propagating bad data downstream."
"""

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class Company(BaseModel):
    """Output of Stage 1 (Ocean.io)."""

    domain: str = Field(..., description="Cleaned company domain, e.g. stripe.com")
    name: Optional[str] = Field(None, description="Company display name if available")
    industry: Optional[str] = None
    employee_count: Optional[int] = None
    country: Optional[str] = None

    @field_validator("domain")
    @classmethod
    def clean_domain(cls, v: str) -> str:
        # Strip protocol and trailing slash so we have a canonical form
        v = v.strip().lower()
        for prefix in ("https://", "http://", "www."):
            if v.startswith(prefix):
                v = v[len(prefix):]
        return v.rstrip("/")

    def __hash__(self) -> int:
        return hash(self.domain)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Company):
            return self.domain == other.domain
        return False


class Contact(BaseModel):
    """Output of Stage 2 (Prospeo) — a decision-maker at a company."""

    name: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    linkedin_url: str = Field(..., description="LinkedIn profile URL")
    company_domain: str
    company_name: Optional[str] = None
    email: Optional[str] = Field(None, description="Work email if already known")
    person_id: Optional[str] = Field(None, description="Prospeo person ID for enrichment")

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip().lower()
        if "@" not in v:
            return None
        return v

    @field_validator("linkedin_url")
    @classmethod
    def validate_linkedin(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("linkedin_url must not be empty")
        # Normalise to https scheme
        if not v.startswith("http"):
            v = "https://" + v
        return v

    def __hash__(self) -> int:
        # Deduplicate contacts by LinkedIn URL (canonical identity)
        return hash(self.linkedin_url)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Contact):
            return self.linkedin_url == other.linkedin_url
        return False


class Lead(BaseModel):
    """
    A verified lead — the output of Stage 3 (EazyReach) and input to Stage 4 (Brevo).
    Aggregates all data collected across stages.
    """

    contact: Contact
    email: str = Field(..., description="Verified work email address")
    email_subject: Optional[str] = None
    email_body: Optional[str] = None
    email_sent: bool = False
    email_send_error: Optional[str] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        v = v.strip().lower()
        if "@" not in v:
            raise ValueError(f"Invalid email: {v!r}")
        return v

    def to_csv_row(self) -> dict:
        """Flat dict suitable for csv.DictWriter."""
        return {
            "company": self.contact.company_domain,
            "contact": self.contact.name,
            "title": self.contact.title,
            "linkedin": self.contact.linkedin_url,
            "email": self.email,
            "email_subject": self.email_subject or "",
            "email_body": self.email_body or "",
            "email_sent": self.email_sent,
            "timestamp": self.timestamp.isoformat(),
        }


class EmailResult(BaseModel):
    """Result of a single Brevo email send attempt."""

    lead_email: str
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None
    sent_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PipelineResult(BaseModel):
    """Aggregated output of the full pipeline run."""

    seed_domain: str
    leads: list[Lead] = Field(default_factory=list)
    metrics: "PipelineMetricsSnapshot"
    failures: list[dict] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: Optional[datetime] = None


class PipelineMetricsSnapshot(BaseModel):
    """Lightweight snapshot embedded in PipelineResult."""

    companies_found: int = 0
    contacts_found: int = 0
    verified_emails: int = 0
    emails_sent: int = 0
    emails_failed: int = 0
    total_duration_seconds: float = 0.0


# Resolve forward reference
PipelineResult.model_rebuild()
