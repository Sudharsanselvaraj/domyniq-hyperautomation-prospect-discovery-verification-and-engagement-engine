"""
config/settings.py — Centralised configuration

Using pydantic-settings (v2) gives us:
  • Type-validated env-var loading
  • .env file support out of the box
  • Clear error messages when vars are missing
  • Easy to mock in tests

Interview talking point:
  "All configuration is validated at startup, not scattered
   across files. If a key is missing we fail fast with a clear
   message rather than silently using a None API key."
"""

from functools import lru_cache
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── API Keys ──────────────────────────────────────────────────────────────
    apollo_api_key: str = Field(..., description="Apollo.io API key")
    prospeo_api_key: str = Field(..., description="Prospeo API key")
    brevo_api_key: str = Field(..., description="Brevo (Sendinblue) API key")
    openai_api_key: str = Field("", description="OpenAI API key for email copy generation")
    xai_api_key: str = Field("", description="xAI API key for Grok email copy generation")
    openrouter_api_key: str = Field("", description="OpenRouter API key for free LLM access")

    # ── Sender identity (Brevo) ───────────────────────────────────────────────
    sender_name: str = Field("Your Name", description="From name for outreach emails")
    sender_email: str = Field("you@yourdomain.com", description="From address (must be verified in Brevo)")

    # ── Pipeline tuning ───────────────────────────────────────────────────────
    max_similar_companies: int = Field(25, ge=1, le=200)
    max_contacts_per_company: int = Field(5, ge=1, le=50)
    request_timeout_seconds: int = Field(30, ge=5, le=120)
    max_concurrent_requests: int = Field(10, ge=1, le=50)

    # ── Retry settings ────────────────────────────────────────────────────────
    retry_max_attempts: int = Field(3, ge=1, le=10)
    retry_wait_min_seconds: float = Field(1.0, ge=0.1)
    retry_wait_max_seconds: float = Field(60.0, ge=1.0)

    # ── Logging ────────────────────────────────────────────────────────────────
    log_level: str = Field("INFO", description="Python logging level")
    log_file: str = Field("logs/pipeline.log")

    # ── Email generation (OpenAI / xAI / OpenRouter / Ollama) ─────────────────
    openai_model: str = Field("gpt-4o-mini", description="OpenAI model for email copy")
    xai_model: str = Field("grok-beta", description="xAI Grok model for email copy")
    openrouter_model: str = Field("nvidia/nemotron-3-nano-30b-a3b:free", description="OpenRouter free model for email copy")
    ollama_model: str = Field("llama3.1:8b", description="Ollama local model for email copy")
    ollama_base_url: str = Field("http://localhost:11434", description="Ollama API base URL")
    email_max_words: int = Field(120, ge=50, le=300)

    # ── Rate-limit pauses (seconds) ────────────────────────────────────────────
    # Prospeo free plan needs a pause between search and enrich calls
    prospeo_enrich_delay_seconds: int = Field(15, ge=0, le=300)

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"log_level must be one of {allowed}")
        return upper

    @field_validator("sender_email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError(f"sender_email '{v}' does not look like a valid email")
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Cached settings singleton — loaded once and reused everywhere.
    Use get_settings() rather than constructing Settings() directly
    so tests can patch just this function.
    """
    return Settings()
