"""Application configuration — environment only (GHAYATH_ prefix). Never hard-coded secrets."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GHAYATH_", env_file=".env", extra="ignore")

    # Database
    database_url: str = "postgresql://ghayath:CHANGE_ME@localhost:5432/ghayath"
    db_pool_size: int = 10

    # Redis (rate limiting). When unset the in-memory limiter is used and health
    # reports redis as down (honest degradation).
    redis_url: str | None = None

    # Auth
    auth_jwt_secret: str = ""
    access_token_ttl_seconds: int = 3600
    refresh_token_ttl_days: int = 7

    # One-time OWNER bootstrap (idempotent)
    seed_owner_email: str | None = None
    seed_owner_password: str | None = None

    # WhatsApp
    # Owner's phone for CRITICAL notification routing (E.164).
    notification_whatsapp_recipient: str | None = None
    whatsapp_webhook_secret: str | None = None
    whatsapp_allowed_senders: str | None = None  # comma-separated E.164
    whatsapp_provider_base_url: str | None = None
    whatsapp_provider_token: str | None = None

    # Email provider
    email_provider_base_url: str | None = None
    email_provider_token: str | None = None

    # GitHub
    github_base_url: str = "https://api.github.com"
    github_token: str | None = None

    # Rate limiting (contract: auth 5/min, agent 60/min)
    rate_limit_enabled: bool = True

    log_level: str = "INFO"

    @property
    def allowed_senders(self) -> list[str]:
        if not self.whatsapp_allowed_senders:
            return []
        return [s.strip() for s in self.whatsapp_allowed_senders.split(",") if s.strip()]
