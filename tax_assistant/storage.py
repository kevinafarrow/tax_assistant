"""Filesystem layout and the operations that keep files, sidecars, and the
SQLite ledger consistent.

Layout (under DATA_DIR):
    receipts/<year>/<NN-Category>/<date>_<vendor>_<amount>.<ext>   image/pdf
    receipts/<year>/<NN-Category>/<date>_<vendor>_<amount>.json    sidecar
    quarantine/<timestamp>_<name>.<ext> (+ .json with the reason)
    ledger.db

Files + sidecars are the source of truth; the DB is a rebuildable index.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .categories import SCHEDULE_C_CATEGORIES, category_dir
from .extractor import Extraction

log = logging.getLogger(__name__)

SIDECAR_VERSION = 1

EXT_FOR_MEDIA_TYPE = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "application/pdf": "pdf",
}


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:max_len].rstrip("-") or "unknown"


def _receipt_rel_dir(receipt_date: str, category: str) -> Path:
    year = receipt_date[:4]
    return Path("receipts") / year / category_dir(category)


def _unique_stem(directory: Path, stem: str, ext: str) -> str:
    candidate = stem
    n = 2
    while (directory / f"{candidate}.{ext}").exists() or (directory / f"{candidate}.json").exists():
        candidate = f"{stem}-{n}"
        n += 1
    return candidate


def store_receipt(data_dir: Path, payload: bytes, media_type: str,
                  extraction: Extraction, receipt_date: str,
                  email_meta: dict) -> int:
    """Write the receipt file + sidecar and insert the ledger row.

    receipt_date is the resolved date (extraction date, falling back to the
    email date). Returns the new receipt id.
    """
    ext = EXT_FOR_MEDIA_TYPE[media_type]
    rel_dir = _receipt_rel_dir(receipt_date, extraction.category)
    abs_dir = data_dir / rel_dir
    abs_dir.mkdir(parents=True, exist_ok=True)

    stem = _unique_stem(
        abs_dir, f"{receipt_date}_{slugify(extraction.vendor)}_{extraction.amount:.2f}", ext
    )
    file_rel = rel_dir / f"{stem}.{ext}"
    sidecar_rel = rel_dir / f"{stem}.json"

    record = {
        "sha256": sha256_hex(payload),
        "vendor": extraction.vendor,
        "date": receipt_date,
        "amount": extraction.amount,
        "category": extraction.category,
        "notes": extraction.notes,
        "email_from": email_meta.get("from", ""),
        "email_subject": email_meta.get("subject", ""),
        "email_date": email_meta.get("date", ""),
        "email_body": email_meta.get("body", ""),
        "file_path": str(file_rel),
        "sidecar_path": str(sidecar_rel),
    }

    (data_dir / file_rel).write_bytes(payload)
    _write_sidecar(data_dir / sidecar_rel, record)

    with db.connect(data_dir) as conn:
        return db.insert_receipt(conn, record)


def _write_sidecar(path: Path, record: dict) -> None:
    payload = {"sidecar_version": SIDECAR_VERSION, **record}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))


def quarantine(data_dir: Path, payload: bytes | None, suggested_name: str,
               reason: str, email_meta: dict) -> Path:
    """Store a failed item under quarantine/ with a JSON note explaining why."""
    qdir = data_dir / "quarantine"
    qdir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", suggested_name or "attachment")[:80]
    safe_name = safe_name.strip("._") or "attachment"
    stem = _unique_stem(qdir, f"{ts}_{Path(safe_name).stem}", Path(safe_name).suffix.lstrip(".") or "bin")
    ext = Path(safe_name).suffix.lstrip(".") or "bin"

    if payload is not None:
        (qdir / f"{stem}.{ext}").write_bytes(payload)
    note = {
        "reason": reason,
        "original_filename": suggested_name,
        "quarantined_at": db.now_iso(),
        "email": email_meta,
        "payload_stored": payload is not None,
    }
    note_path = qdir / f"{stem}.json"
    note_path.write_text(json.dumps(note, indent=2, ensure_ascii=False))
    log.warning("Quarantined %s: %s", suggested_name, reason)
    return note_path


EDITABLE_FIELDS = ("vendor", "date", "amount", "category", "notes")


def apply_edit(data_dir: Path, receipt_id: int, changes: dict) -> None:
    """Apply a correction: update the ledger row, rewrite the sidecar, and
    move/rename the files if date/vendor/amount/category changed. Records an
    audit entry per changed field."""
    with db.connect(data_dir) as conn:
        row = db.get_receipt(conn, receipt_id)
        if row is None:
            raise KeyError(f"no receipt with id {receipt_id}")
        record = dict(row)

        changed = {}
        for field in EDITABLE_FIELDS:
            if field not in changes:
                continue
            new = changes[field]
            if field == "amount":
                new = round(float(new), 2)
                if new <= 0:
                    raise ValueError("amount must be positive")
            elif field == "date":
                new = datetime.strptime(str(new), "%Y-%m-%d").date().isoformat()
            elif field == "category":
                if new not in SCHEDULE_C_CATEGORIES:
                    raise ValueError(f"unknown category: {new}")
            else:
                new = str(new).strip()
                if field == "vendor" and not new:
                    raise ValueError("vendor must not be empty")
            if new != record[field]:
                changed[field] = (record[field], new)
                record[field] = new

        if not changed:
            return

        # Relocate the file and sidecar if any naming-relevant field changed.
        old_file = data_dir / record["file_path"]
        old_sidecar = data_dir / record["sidecar_path"]
        if any(f in changed for f in ("vendor", "date", "amount", "category")):
            ext = old_file.suffix.lstrip(".")
            rel_dir = _receipt_rel_dir(record["date"], record["category"])
            abs_dir = data_dir / rel_dir
            abs_dir.mkdir(parents=True, exist_ok=True)
            stem = _unique_stem(
                abs_dir, f"{record['date']}_{slugify(record['vendor'])}_{record['amount']:.2f}", ext
            )
            new_file_rel = rel_dir / f"{stem}.{ext}"
            new_sidecar_rel = rel_dir / f"{stem}.json"
            if old_file.exists():
                shutil.move(old_file, data_dir / new_file_rel)
            record["file_path"] = str(new_file_rel)
            record["sidecar_path"] = str(new_sidecar_rel)
            if old_sidecar.exists():
                old_sidecar.unlink()

        record["updated_at"] = db.now_iso()
        sidecar_record = {k: record[k] for k in (
            "sha256", "vendor", "date", "amount", "category", "notes",
            "email_from", "email_subject", "email_date", "email_body",
            "file_path", "sidecar_path",
        )}
        _write_sidecar(data_dir / record["sidecar_path"], sidecar_record)

        conn.execute(
            """UPDATE receipts SET vendor=:vendor, date=:date, amount=:amount,
               category=:category, notes=:notes, file_path=:file_path,
               sidecar_path=:sidecar_path, updated_at=:updated_at WHERE id=:id""",
            record,
        )
        for field, (old, new) in changed.items():
            db.record_audit(conn, receipt_id, field, str(old), str(new))


def rebuild_db(data_dir: Path) -> int:
    """Rebuild the receipts table from the JSON sidecars on disk. Returns the
    number of receipts indexed. processed_emails and audit_log are left as-is."""
    sidecars = sorted((data_dir / "receipts").rglob("*.json")) if (data_dir / "receipts").exists() else []
    with db.connect(data_dir) as conn:
        conn.execute("DELETE FROM receipts")
        count = 0
        for path in sidecars:
            try:
                data = json.loads(path.read_text())
                rec = {k: data[k] for k in (
                    "sha256", "vendor", "date", "amount", "category", "notes",
                    "email_from", "email_subject", "email_date", "email_body",
                    "file_path", "sidecar_path",
                )}
                db.insert_receipt(conn, rec)
                count += 1
            except Exception:
                log.exception("Skipping unreadable sidecar %s", path)
        return count


def list_quarantine(data_dir: Path) -> list[dict]:
    qdir = data_dir / "quarantine"
    items = []
    if not qdir.exists():
        return items
    for note_path in sorted(qdir.glob("*.json"), reverse=True):
        try:
            note = json.loads(note_path.read_text())
            note["note_file"] = note_path.name
            items.append(note)
        except Exception:
            items.append({"note_file": note_path.name, "reason": "(unreadable note)"})
    return items
