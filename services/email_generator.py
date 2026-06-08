"""
services/email_generator.py — AI-Powered Email Copy Generation

Uses OpenAI (GPT-4o-mini) to generate personalised subject lines and
email bodies for each contact.

The prompt is engineered to produce:
  • A compelling, personalised subject line
  • A concise body (≤120 words) that references the contact's role
  • JSON-structured output for reliable parsing

Interview talking point:
  "I ask the model to return JSON rather than free-form text.
   This decouples parsing from prompt wording — I can tweak
   the copy guidance without changing the parsing code."
"""

import json
import re
from typing import Optional

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
    RetryError,
)
import logging

from config.settings import Settings
from utils.exceptions import EmailGenerationError, ServiceUnavailableError
from utils.logger import get_logger

logger = get_logger(__name__)

SYSTEM_PROMPT_TEMPLATE = """You are an expert B2B outreach copywriter.
Your task: write a cold outreach email from a sales professional to a senior decision-maker.

Rules:
- Professional and respectful tone
- Personalised to their specific role
- NEVER mention their company name in a generic way; make it feel tailored
- Under {max_words} words for the body
- No spam words (FREE, guaranteed, limited-time, etc.)
- End with a single, low-friction CTA (e.g. "Would you have 15 minutes this week?")

Output ONLY valid JSON in this exact format:
{{
  "subject": "...",
  "body": "..."
}}
No preamble, no markdown fences, only the JSON object."""

USER_TEMPLATE = """Write a cold outreach email for the following contact:

Contact name: {name}
Job title: {title}
Company domain: {company}

Generate a subject line and email body. Remember: JSON only."""


class EmailGeneratorService:
    """
    Wraps the OpenAI Chat Completions API to generate email copy.

    We call the API directly via httpx rather than the openai SDK
    to keep dependencies lean and make mocking trivial in tests.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # Prefer xAI Grok if key is provided, fallback to OpenAI
        if settings.xai_api_key:
            self._api_key = settings.xai_api_key
            self._model = settings.xai_model
            self._base_url = "https://api.x.ai/v1/chat/completions"
            self._provider = "xAI"
        elif settings.openai_api_key:
            self._api_key = settings.openai_api_key
            self._model = settings.openai_model
            self._base_url = "https://api.openai.com/v1/chat/completions"
            self._provider = "OpenAI"
        else:
            raise EmailGenerationError("No AI API key configured. Set OPENAI_API_KEY or XAI_API_KEY in .env")
        self._max_words = settings.email_max_words
        self._system_prompt = SYSTEM_PROMPT_TEMPLATE.format(max_words=self._max_words)

    async def generate(
        self,
        name: str,
        title: str,
        company: str,
    ) -> tuple[str, str]:
        """
        Generate a (subject, body) pair for one contact.

        Returns:
            (subject_line, html_body)
        Raises:
            EmailGenerationError if the API call fails or response is unparseable.
        """
        user_content = USER_TEMPLATE.format(
            name=name,
            title=title,
            company=company,
        )

        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.7,
            "max_tokens": 400,
        }

        @retry(
            retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.RequestError)),
            stop=stop_after_attempt(self._settings.retry_max_attempts),
            wait=wait_exponential(
                multiplier=1,
                min=self._settings.retry_wait_min_seconds,
                max=self._settings.retry_wait_max_seconds,
            ),
            before_sleep=before_sleep_log(logger, logging.WARNING),
            reraise=True,
        )
        async def _call_api() -> httpx.Response:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self._settings.request_timeout_seconds)
            ) as client:
                resp = await client.post(
                    self._base_url,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                resp.raise_for_status()
                return resp

        try:
            resp = await _call_api()
        except RetryError as exc:
            raise EmailGenerationError(f"{self._provider} failed after retries: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            raise EmailGenerationError(
                f"{self._provider} API error {exc.response.status_code}: {exc.response.text[:200]}"
            ) from exc
        except httpx.RequestError as exc:
            raise EmailGenerationError(f"{self._provider} network error: {exc}") from exc

        data = resp.json()
        raw_text = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )

        return self._parse_email_json(raw_text, name=name, title=title)

    def _parse_email_json(
        self, raw: str, *, name: str, title: str
    ) -> tuple[str, str]:
        """
        Parse the model's JSON output into (subject, body).
        Falls back to sensible defaults if parsing fails.
        """
        # Strip any accidental markdown fences
        cleaned = re.sub(r"^```(?:json)?\s*|```$", "", raw.strip(), flags=re.MULTILINE).strip()

        try:
            parsed = json.loads(cleaned)
            subject = str(parsed.get("subject", "")).strip()
            body = str(parsed.get("body", "")).strip()
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning(
                f"EmailGenerator: JSON parse failed, using fallback — {exc}"
            )
            subject, body = self._fallback_email(name, title)

        if not subject:
            subject = f"Quick question, {name.split()[0]}"
        if not body:
            _, body = self._fallback_email(name, title)

        # Wrap plain text body in minimal HTML
        html_body = self._to_html(body)
        return subject, html_body

    @staticmethod
    def _fallback_email(name: str, title: str) -> tuple[str, str]:
        first = name.split()[0] if name else "there"
        subject = f"Quick question, {first}"
        body = (
            f"Hi {first},\n\n"
            f"I came across your profile and noticed your work as {title}. "
            f"We help companies like yours solve key operational challenges — "
            f"I thought there might be a relevant fit worth a quick conversation.\n\n"
            f"Would you have 15 minutes this week to chat?\n\n"
            f"Best regards"
        )
        return subject, body

    @staticmethod
    def _to_html(text: str) -> str:
        """Convert newline-delimited plain text to simple HTML."""
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        html_parts = ["<p>" + p.replace("\n", "<br>") + "</p>" for p in paragraphs]
        return "\n".join(html_parts)
