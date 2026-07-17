"""Claude API cost tracking.

Anthropic does not expose the account's credit balance over the API, so the
"remaining balance" figure is derived locally: every call's token usage is
priced from the table below and recorded in the api_calls ledger table, and
remaining = the most recent balance anchor minus spend recorded since it.

An anchor is a "the Console showed $X at time T" fact. Two sources, newest
wins: `cli set-balance <usd>` (stored in the balance_anchors table — the
everyday way to re-sync after a top-up or drift) and the optional
CLAUDE_BALANCE_USD / CLAUDE_BALANCE_AS_OF pair in .env. The figure only
accounts for this app's usage — if the same API key is used elsewhere, it
drifts optimistic until the next sync.
"""
from __future__ import annotations

import logging

from . import db

log = logging.getLogger(__name__)

# USD per million tokens (input, output), keyed by model-ID prefix; the
# longest matching prefix wins. Sticker prices as of 2026-07 — check
# https://platform.claude.com/docs/en/pricing when changing models.
PRICES_PER_MTOK = {
    "claude-fable-5": (10.00, 50.00),
    "claude-opus-4": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
# Cache writes/reads bill as multiples of the input price. This app sends a
# unique image per call so these stay 0, but price them in case that changes.
CACHE_WRITE_MULT = 1.25
CACHE_READ_MULT = 0.10


def estimate_cost_usd(model: str, usage) -> float | None:
    """Price one response's usage block; None if the model isn't in the table."""
    prefix = max((p for p in PRICES_PER_MTOK if model.startswith(p)), key=len, default=None)
    if prefix is None:
        return None
    in_price, out_price = PRICES_PER_MTOK[prefix]
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    return (
        usage.input_tokens * in_price
        + cache_write * in_price * CACHE_WRITE_MULT
        + cache_read * in_price * CACHE_READ_MULT
        + usage.output_tokens * out_price
    ) / 1_000_000


def record_call(cfg, model: str, usage) -> float | None:
    """Record one API call in the ledger and return its estimated cost.

    Never raises — cost tracking must not break receipt processing. The call
    is recorded even when extraction later fails, because Anthropic bills it
    either way.
    """
    cost = None
    try:
        cost = estimate_cost_usd(model, usage)
        if cost is None:
            log.warning("No pricing known for model %r; call recorded without a cost", model)
        with db.connect(cfg.data_dir) as conn:
            db.record_api_call(
                conn,
                model,
                usage.input_tokens,
                usage.output_tokens,
                getattr(usage, "cache_creation_input_tokens", 0) or 0,
                getattr(usage, "cache_read_input_tokens", 0) or 0,
                cost,
            )
    except Exception:
        log.exception("Failed to record API call cost")
    return cost


def _current_anchor(cfg, conn) -> tuple[float, str] | None:
    """(balance_usd, as_of) from the newest of the .env anchor and the last
    `set-balance` sync; None when neither is configured. ISO-8601 strings
    compare chronologically, and the .env pair with no as-of date ("") loses
    to any synced anchor."""
    candidates: list[tuple[str, float]] = []
    if cfg.claude_balance_usd is not None:
        candidates.append((cfg.claude_balance_as_of, cfg.claude_balance_usd))
    row = db.latest_balance_anchor(conn)
    if row is not None:
        candidates.append((row["noted_at"], row["balance_usd"]))
    if not candidates:
        return None
    as_of, usd = max(candidates)
    return usd, as_of


def sync_balance(cfg, balance_usd: float) -> None:
    """Record the real balance as read off the Anthropic Console just now."""
    with db.connect(cfg.data_dir) as conn:
        db.record_balance_anchor(conn, balance_usd)


def remaining_balance_usd(cfg) -> float | None:
    """Current anchor minus tracked spend since it; None if no anchor exists
    or the lookup fails."""
    try:
        with db.connect(cfg.data_dir) as conn:
            anchor = _current_anchor(cfg, conn)
            if anchor is None:
                return None
            usd, as_of = anchor
            return usd - db.api_spend_since(conn, as_of)
    except Exception:
        log.exception("Failed to compute remaining balance")
        return None


def lifetime_spend_usd(cfg) -> float:
    """Everything this tool has ever spent (well, since cost tracking began)."""
    try:
        with db.connect(cfg.data_dir) as conn:
            return db.api_spend_since(conn, "")
    except Exception:
        log.exception("Failed to compute lifetime API spend")
        return 0.0


def summary(cfg) -> dict:
    """Everything the `costs` CLI command reports, in one dict."""
    with db.connect(cfg.data_dir) as conn:
        anchor = _current_anchor(cfg, conn)
        lifetime = db.api_spend_since(conn, "")
        return {
            "calls": db.api_call_count(conn),
            "lifetime_usd": lifetime,
            "anchor_usd": anchor[0] if anchor else None,
            "anchor_as_of": anchor[1] if anchor else None,
            "spent_since_anchor_usd": db.api_spend_since(conn, anchor[1]) if anchor else None,
            "remaining_usd": anchor[0] - db.api_spend_since(conn, anchor[1]) if anchor else None,
        }
