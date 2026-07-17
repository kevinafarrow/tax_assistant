"""The IMAP poll loop: fetch INBOX, run the trust gate, extract, store, notify.

Runs in a daemon thread started by main.py. Each fully successful cycle pings
healthchecks.io; any cycle-level failure pings the /fail endpoint. Per-message
failures never fail the cycle — the message is recorded/quarantined and life
goes on, so one poison email can't take down monitoring.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from imap_tools import MailBox

from . import db, gate, storage
from .extractor import (SUPPORTED_MEDIA_TYPES, Extraction, ExtractionError,
                        TransientAPIError, extract)
from .notify import hc_ping, pushover

log = logging.getLogger(__name__)


def run_forever(cfg, stop_event: threading.Event) -> None:
    log.info("Poller starting: %s every %ss", cfg.imap_user, cfg.poll_interval_seconds)
    while not stop_event.is_set():
        try:
            summary = poll_once(cfg)
            hc_ping(cfg, ok=True, message=summary)
        except Exception as exc:
            log.exception("Poll cycle failed")
            hc_ping(cfg, ok=False, message=f"poll cycle failed: {exc}")
        stop_event.wait(cfg.poll_interval_seconds)


def _ensure_folders(mailbox: MailBox, cfg) -> None:
    existing = {f.name for f in mailbox.folder.list()}
    for name in (cfg.imap_folder_processed, cfg.imap_folder_rejected):
        if name not in existing:
            mailbox.folder.create(name)
            # IMAP-created folders aren't auto-subscribed; without this,
            # webmail clients hide them.
            mailbox.client.subscribe(name)


def poll_once(cfg) -> str:
    """One poll cycle. Returns a short summary string for the healthcheck ping."""
    with db.connect(cfg.data_dir) as conn:
        already_today = db.emails_processed_today(conn)
    if already_today >= cfg.max_emails_per_day:
        log.warning("Daily email cap reached (%s); skipping cycle", cfg.max_emails_per_day)
        return f"daily cap reached ({already_today})"

    accepted = rejected = 0
    with MailBox(cfg.imap_host).login(cfg.imap_user, cfg.imap_password, "INBOX") as mailbox:
        _ensure_folders(mailbox, cfg)
        messages = list(mailbox.fetch(mark_seen=True, bulk=True))
        for msg in messages:
            if already_today + accepted + rejected >= cfg.max_emails_per_day:
                pushover(cfg, "Tax assistant: daily cap",
                         "Daily email cap reached; remaining mail left in INBOX.", priority=1)
                break
            try:
                ok = _handle_message(cfg, mailbox, msg)
                accepted += 1 if ok else 0
                rejected += 0 if ok else 1
            except TransientAPIError:
                # API outage / rate limit / out of credits: leave the message
                # in INBOX untouched and fail the cycle — healthchecks alerts,
                # and the next cycle retries everything still pending.
                raise
            except Exception as exc:
                # Unexpected per-message failure: record, alert, park in Rejected
                # so the same message can't wedge every future cycle.
                log.exception("Failed handling message uid=%s", msg.uid)
                with db.connect(cfg.data_dir) as conn:
                    db.record_email(conn, msg.headers.get("message-id", ("",))[0],
                                    msg.from_, msg.subject, "error", str(exc)[:500])
                pushover(cfg, "Tax assistant: processing error",
                         f"Email from {msg.from_} failed: {exc}", priority=1)
                mailbox.move([msg.uid], cfg.imap_folder_rejected)

    return f"cycle ok: {accepted} accepted, {rejected} rejected"


def _handle_message(cfg, mailbox: MailBox, msg) -> bool:
    """Process one message. Returns True if it passed the gate."""
    message_id = msg.headers.get("message-id", ("",))[0]
    auth_results = msg.headers.get("authentication-results", ())
    body_text = msg.text or msg.html or ""

    result = gate.check(msg.from_, auth_results, msg.subject, body_text,
                        cfg.allowed_senders, cfg.bearer_token)
    if not result.ok:
        log.warning("Gate rejected message from %s: %s", msg.from_, result.reason)
        with db.connect(cfg.data_dir) as conn:
            db.record_email(conn, message_id, msg.from_, msg.subject, "rejected", result.reason)
        pushover(cfg, "⚠️ Tax assistant: BAD email rejected",
                 f"From: {msg.from_}\nSubject: {msg.subject[:100]}\nReason: {result.reason}\n\n"
                 "If this wasn't you, consider re-rolling the inbox UUID and bearer token.",
                 priority=1)
        mailbox.move([msg.uid], cfg.imap_folder_rejected)
        return False

    clean_body = gate.strip_token(body_text, cfg.bearer_token)
    clean_subject = gate.strip_token(msg.subject, cfg.bearer_token)
    email_meta = {
        "from": msg.from_,
        "subject": clean_subject,
        "date": msg.date.isoformat() if msg.date else "",
        "body": clean_body,
    }

    lines = _process_attachments(cfg, msg, clean_subject, clean_body, email_meta)

    with db.connect(cfg.data_dir) as conn:
        db.record_email(conn, message_id, msg.from_, clean_subject, "accepted",
                        " | ".join(lines)[:500])
    pushover(cfg, "✅ Receipt email processed", "\n".join(lines) or "(no lines)")
    mailbox.move([msg.uid], cfg.imap_folder_processed)
    return True


def _process_attachments(cfg, msg, subject: str, body: str, email_meta: dict) -> list[str]:
    """Handle each supported attachment; returns human-readable result lines
    for the Pushover summary."""
    lines: list[str] = []
    supported = [a for a in msg.attachments if a.content_type in SUPPORTED_MEDIA_TYPES]
    unsupported = [a for a in msg.attachments if a.content_type not in SUPPORTED_MEDIA_TYPES]

    for att in unsupported:
        lines.append(f"⏭ {att.filename or '(unnamed)'}: unsupported type {att.content_type} "
                     "(send JPEG/PNG/PDF)")

    if not supported:
        if not unsupported:
            lines.append("⚠️ No attachments found — nothing stored.")
        return lines

    if len(supported) > cfg.max_attachments_per_email:
        skipped = supported[cfg.max_attachments_per_email:]
        supported = supported[: cfg.max_attachments_per_email]
        lines.append(f"⏭ {len(skipped)} attachment(s) over the per-email cap were skipped.")

    for att in supported:
        name = att.filename or "attachment"
        payload = att.payload
        if len(payload) > cfg.max_attachment_mb * 1024 * 1024:
            storage.quarantine(cfg.data_dir, None, name,
                               f"attachment too large ({len(payload)} bytes)", email_meta)
            lines.append(f"🚫 {name}: too large, quarantined (metadata only)")
            continue

        sha = storage.sha256_hex(payload)
        with db.connect(cfg.data_dir) as conn:
            if db.receipt_exists(conn, sha):
                lines.append(f"♻️ {name}: duplicate of an existing receipt, skipped")
                continue

        try:
            extraction = extract(cfg, payload, att.content_type, subject, body)
            receipt_date = _resolve_date(extraction, msg)
            storage.store_receipt(cfg.data_dir, payload, att.content_type,
                                  extraction, receipt_date, email_meta)
            date_note = "" if extraction.date else " (date from email)"
            lines.append(f"✅ {extraction.vendor} — ${extraction.amount:.2f} — "
                         f"{extraction.category} — {receipt_date}{date_note}")
        except ExtractionError as exc:
            storage.quarantine(cfg.data_dir, payload, name, str(exc), email_meta)
            lines.append(f"🚫 {name}: quarantined — {exc}")

    return lines


def _resolve_date(extraction: Extraction, msg) -> str:
    if extraction.date:
        return extraction.date
    if msg.date:
        return msg.date.date().isoformat()
    return datetime.now(timezone.utc).date().isoformat()
