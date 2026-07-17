"""Receipt data extraction via the Claude API.

Security model: the receipt image and email body are untrusted-ish input (the
gate verifies the sender, but content could still be adversarial). The call
has NO tools, must return JSON matching a strict schema, and every field is
validated here before anything touches the ledger. Images are never decoded
locally — Anthropic's infrastructure does the parsing.
"""
from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime

import anthropic

from .categories import CATEGORY_NAMES

log = logging.getLogger(__name__)

IMAGE_MEDIA_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
PDF_MEDIA_TYPE = "application/pdf"
SUPPORTED_MEDIA_TYPES = IMAGE_MEDIA_TYPES | {PDF_MEDIA_TYPE}

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "vendor": {
            "type": "string",
            "description": "Merchant or vendor name as printed on the receipt",
        },
        "date": {
            "anyOf": [{"type": "string", "format": "date"}, {"type": "null"}],
            "description": "Receipt/transaction date in YYYY-MM-DD, or null if unreadable",
        },
        "amount": {
            "anyOf": [{"type": "number"}, {"type": "null"}],
            "description": "Grand total paid including tax and tip, or null if unreadable",
        },
        "category": {
            "type": "string",
            "enum": list(CATEGORY_NAMES),
            "description": "The IRS Schedule C expense category that best fits this expense",
        },
        "notes": {
            "type": "string",
            "description": "Short summary of what was purchased, plus anything relevant from the sender's notes",
        },
    },
    "required": ["vendor", "date", "amount", "category", "notes"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You extract structured data from business expense receipts for a U.S. sole \
proprietor's Schedule C tax filing.

You are given a receipt (image or PDF) and the notes the business owner wrote when \
submitting it. Extract the vendor, transaction date, grand total, and the best-fitting \
Schedule C category. The owner's notes are context for categorization and may contain \
corrections (e.g. the true business purpose); when the notes conflict with the receipt \
about purpose or category, the notes win. The amount and date should come from the \
receipt itself unless the receipt is unreadable and the notes state them explicitly.

If the date or total genuinely cannot be determined, return null for that field rather \
than guessing. Never invent values."""


class ExtractionError(Exception):
    """Raised when extraction fails or the result does not validate.
    The attachment is quarantined — retrying would not help."""


class TransientAPIError(Exception):
    """Raised when the Claude API is temporarily unavailable (network, rate
    limit, overload, or an out-of-credits account). The email stays in INBOX
    and the whole poll cycle fails so healthchecks alerts; the next cycle
    retries."""


@dataclass(frozen=True)
class Extraction:
    vendor: str
    date: str | None  # YYYY-MM-DD
    amount: float
    category: str
    notes: str


def _content_block(payload: bytes, media_type: str) -> dict:
    data = base64.standard_b64encode(payload).decode("ascii")
    if media_type == PDF_MEDIA_TYPE:
        return {
            "type": "document",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        }
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": data},
    }


def extract(cfg, payload: bytes, media_type: str, subject: str, body: str) -> Extraction:
    if media_type not in SUPPORTED_MEDIA_TYPES:
        raise ExtractionError(f"unsupported attachment type: {media_type}")

    client = anthropic.Anthropic()
    notes_text = f"Sender's notes — subject: {subject or '(none)'}\nBody:\n{body or '(none)'}"

    try:
        response = client.messages.create(
            model=cfg.anthropic_model,
            max_tokens=4096,
            thinking={"type": "adaptive"},
            system=SYSTEM_PROMPT,
            output_config={"format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA}},
            messages=[
                {
                    "role": "user",
                    "content": [
                        _content_block(payload, media_type),
                        {"type": "text", "text": notes_text},
                    ],
                }
            ],
        )
    except anthropic.APIConnectionError as exc:
        raise TransientAPIError(f"cannot reach Anthropic API: {exc}") from exc
    except anthropic.APIStatusError as exc:
        if _is_transient(exc):
            raise TransientAPIError(f"Anthropic API unavailable ({exc.status_code}): "
                                    f"{exc.message}") from exc
        # A definitive 4xx means this particular request is unprocessable.
        raise ExtractionError(f"API rejected request ({exc.status_code}): {exc.message}") from exc

    if response.stop_reason == "refusal":
        raise ExtractionError("model refused the request")
    if response.stop_reason == "max_tokens":
        raise ExtractionError("model output truncated (max_tokens)")

    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        raise ExtractionError("model returned no text content")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"model output is not valid JSON: {exc}") from exc

    return _validate(data)


def _is_transient(exc: anthropic.APIStatusError) -> bool:
    """Retryable API failures: rate limits, server errors/overload, and the
    out-of-credits 400 (clears when the account is topped up)."""
    if exc.status_code == 429 or exc.status_code >= 500:
        return True
    return "credit balance" in str(exc.message).lower()


def _validate(data: dict) -> Extraction:
    vendor = str(data.get("vendor") or "").strip()
    if not vendor:
        raise ExtractionError("vendor is empty")

    category = data.get("category")
    if category not in CATEGORY_NAMES:
        raise ExtractionError(f"category not in Schedule C list: {category!r}")

    amount = data.get("amount")
    if not isinstance(amount, (int, float)) or isinstance(amount, bool):
        raise ExtractionError("amount is missing or not a number")
    amount = round(float(amount), 2)
    if amount <= 0 or amount >= 1_000_000:
        raise ExtractionError(f"amount out of range: {amount}")

    rdate = data.get("date")
    if rdate is not None:
        try:
            parsed = datetime.strptime(str(rdate), "%Y-%m-%d").date()
        except ValueError as exc:
            raise ExtractionError(f"date is not YYYY-MM-DD: {rdate!r}") from exc
        if not (date(2000, 1, 1) <= parsed <= date.today().replace(year=date.today().year + 1)):
            raise ExtractionError(f"date implausible: {rdate}")
        rdate = parsed.isoformat()

    notes = str(data.get("notes") or "").strip()
    return Extraction(vendor=vendor, date=rdate, amount=amount, category=category, notes=notes)
