"""
clients/brevo_client.py — Brevo (Sendinblue) API Client

Stage 4: Send personalised outreach emails via Brevo's transactional
         email API.

Brevo API docs: https://developers.brevo.com/docs
Auth: api-key header
Key endpoint: POST /v3/smtp/email

Interview talking point:
  "I chose Brevo's transactional API over marketing campaigns
   because it gives per-email tracking and doesn't require a
   pre-built contact list. Each send is a single API call with
   subject, body, from/to — simple and auditable."
"""

from clients.base import BaseClient
from config.settings import Settings
from models.pipeline import EmailResult
from utils.exceptions import BrevoError
from utils.logger import get_logger

logger = get_logger(__name__)


class BrevoClient(BaseClient):
    service_name = "Brevo"
    base_url = "https://api.brevo.com"

    def __init__(self, settings: Settings) -> None:
        super().__init__(api_key=settings.brevo_api_key, settings=settings)
        self._sender_name = settings.sender_name
        self._sender_email = settings.sender_email

    def _build_headers(self) -> dict[str, str]:
        return {
            "api-key": self._api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def send_email(
        self,
        to_email: str,
        to_name: str,
        subject: str,
        html_body: str,
    ) -> EmailResult:
        """
        Send a single transactional email via POST /v3/smtp/email.

        Returns an EmailResult indicating success/failure with message_id.
        """
        logger.debug(
            "Brevo: sending email",
            extra={"to": to_email, "subject": subject[:60]},
        )

        payload = {
            "sender": {
                "name": self._sender_name,
                "email": self._sender_email,
            },
            "to": [{"email": to_email, "name": to_name}],
            "subject": subject,
            "htmlContent": html_body,
            # Text fallback for clients that don't render HTML
            "textContent": self._strip_html(html_body),
        }

        try:
            data = await self._post("/v3/smtp/email", json=payload)
        except BrevoError:
            raise
        except Exception as exc:
            raise BrevoError(
                f"Failed to send email to {to_email}: {exc}",
                service=self.service_name,
            ) from exc

        message_id = data.get("messageId") or data.get("message_id")
        logger.info(
            "Brevo: email sent",
            extra={"to": to_email, "message_id": message_id},
        )
        return EmailResult(
            lead_email=to_email,
            success=True,
            message_id=str(message_id) if message_id else None,
        )

    @staticmethod
    def _strip_html(html: str) -> str:
        """
        Minimal HTML-to-text for the textContent fallback.
        A full parser is overkill here — we just need readable plain text.
        """
        import re
        text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
