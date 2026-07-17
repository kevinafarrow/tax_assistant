"""Configuration loaded from environment variables (.env via docker compose)."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    data_dir: Path

    imap_host: str
    imap_user: str
    imap_password: str
    imap_folder_processed: str
    imap_folder_rejected: str

    allowed_senders: tuple[str, ...]
    bearer_token: str

    anthropic_model: str
    # Balance anchor for cost reporting: what the Console said your credit
    # balance was, and when. None/"" disables the "remaining" figure.
    claude_balance_usd: float | None
    claude_balance_as_of: str  # ISO date (YYYY-MM-DD); "" counts all recorded spend

    pushover_user_key: str
    pushover_app_token: str
    healthchecks_url: str

    poll_interval_seconds: int
    max_emails_per_day: int
    max_attachment_mb: int
    max_attachments_per_email: int

    web_password: str
    session_secret: str
    session_max_age_days: int


def load() -> Config:
    env = os.environ
    senders = tuple(
        s.strip().lower() for s in env.get("ALLOWED_SENDERS", "").split(",") if s.strip()
    )
    return Config(
        data_dir=Path(env.get("DATA_DIR", "/data")),
        imap_host=env.get("IMAP_HOST", "imap.purelymail.com"),
        imap_user=env.get("IMAP_USER", ""),
        imap_password=env.get("IMAP_PASSWORD", ""),
        imap_folder_processed=env.get("IMAP_FOLDER_PROCESSED", "Processed"),
        imap_folder_rejected=env.get("IMAP_FOLDER_REJECTED", "Rejected"),
        allowed_senders=senders,
        bearer_token=env.get("BEARER_TOKEN", ""),
        anthropic_model=env.get("ANTHROPIC_MODEL", "claude-opus-4-8"),
        claude_balance_usd=(
            float(env["CLAUDE_BALANCE_USD"]) if env.get("CLAUDE_BALANCE_USD", "").strip() else None
        ),
        claude_balance_as_of=env.get("CLAUDE_BALANCE_AS_OF", "").strip(),
        pushover_user_key=env.get("PUSHOVER_USER_KEY", ""),
        pushover_app_token=env.get("PUSHOVER_APP_TOKEN", ""),
        healthchecks_url=env.get("HEALTHCHECKS_URL", "").rstrip("/"),
        poll_interval_seconds=int(env.get("POLL_INTERVAL_SECONDS", "300")),
        max_emails_per_day=int(env.get("MAX_EMAILS_PER_DAY", "100")),
        max_attachment_mb=int(env.get("MAX_ATTACHMENT_MB", "15")),
        max_attachments_per_email=int(env.get("MAX_ATTACHMENTS_PER_EMAIL", "10")),
        web_password=env.get("WEB_PASSWORD", ""),
        # Sessions reset on restart if no secret is pinned in .env.
        session_secret=env.get("SESSION_SECRET") or secrets.token_hex(32),
        session_max_age_days=int(env.get("SESSION_MAX_AGE_DAYS", "30")),
    )


def validate_for_polling(cfg: Config) -> list[str]:
    """Return a list of problems that prevent the mail poller from running."""
    problems = []
    if not cfg.imap_user:
        problems.append("IMAP_USER is not set")
    if not cfg.imap_password:
        problems.append("IMAP_PASSWORD is not set")
    if not cfg.allowed_senders:
        problems.append("ALLOWED_SENDERS is not set")
    if not cfg.bearer_token:
        problems.append("BEARER_TOKEN is not set")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        problems.append("ANTHROPIC_API_KEY is not set")
    return problems
