"""The trust gate. An email is processed only if ALL of the following hold:

1. The From address is on the configured allowlist.
2. The receiving server's Authentication-Results header records an SPF pass
   (From headers are trivially forged; SPF verifies the sending server).
3. The email contains the secret bearer token in its subject or body.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_SPF_PASS = re.compile(r"\bspf\s*=\s*pass\b", re.IGNORECASE)


@dataclass(frozen=True)
class GateResult:
    ok: bool
    reason: str


def check(from_addr: str, auth_results_headers: tuple[str, ...],
          subject: str, body: str,
          allowed_senders: tuple[str, ...], bearer_token: str) -> GateResult:
    sender = (from_addr or "").strip().lower()
    if sender not in allowed_senders:
        return GateResult(False, f"sender not on allowlist: {sender or '(empty)'}")

    if not any(_SPF_PASS.search(h) for h in auth_results_headers):
        return GateResult(False, "no SPF pass in Authentication-Results")

    if not bearer_token:
        return GateResult(False, "bearer token not configured")
    if bearer_token not in (subject or "") and bearer_token not in (body or ""):
        return GateResult(False, "bearer token missing")

    return GateResult(True, "ok")


def strip_token(text: str, bearer_token: str) -> str:
    """Remove the bearer token before the text is stored or sent anywhere."""
    if not text or not bearer_token:
        return text or ""
    return text.replace(bearer_token, "[token]").strip()
