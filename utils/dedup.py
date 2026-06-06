"""
utils/dedup.py — Deduplication utilities

Deduplication is applied at three points in the pipeline:
  1. After Stage 1: deduplicate company domains
  2. After Stage 2: deduplicate LinkedIn URLs (same person found via multiple companies)
  3. After Stage 3: deduplicate email addresses (belt-and-suspenders)

Interview talking point:
  "Without dedup, the same person could receive multiple emails
   in the same blast — a great way to get marked as spam and
   ruin the sender domain's reputation."
"""

from typing import TypeVar

from models.pipeline import Company, Contact, Lead

T = TypeVar("T")


def deduplicate_companies(companies: list[Company]) -> list[Company]:
    """
    Remove duplicate companies by domain (case-insensitive).
    Preserves the first occurrence (usually the most data-rich).
    """
    seen: set[str] = set()
    result: list[Company] = []
    for company in companies:
        key = company.domain.lower()
        if key not in seen:
            seen.add(key)
            result.append(company)
    return result


def deduplicate_contacts(contacts: list[Contact]) -> list[Contact]:
    """
    Remove duplicate contacts by LinkedIn URL.
    The same executive may appear at multiple company lookups.
    """
    seen: set[str] = set()
    result: list[Contact] = []
    for contact in contacts:
        key = contact.linkedin_url.lower().rstrip("/")
        if key not in seen:
            seen.add(key)
            result.append(contact)
    return result


def deduplicate_leads(leads: list[Lead]) -> list[Lead]:
    """
    Remove duplicate leads by email address.
    Email is the final deliverable — one address should receive exactly one email.
    """
    seen: set[str] = set()
    result: list[Lead] = []
    for lead in leads:
        key = lead.email.lower()
        if key not in seen:
            seen.add(key)
            result.append(lead)
    return result
