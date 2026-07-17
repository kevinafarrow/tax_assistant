"""SQLite ledger. The database is an index over the JSON sidecars on disk and
can always be rebuilt from them (see storage.rebuild_db)."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT NOT NULL UNIQUE,
    vendor TEXT NOT NULL,
    date TEXT NOT NULL,            -- receipt date, YYYY-MM-DD
    amount REAL NOT NULL,
    category TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    email_from TEXT NOT NULL DEFAULT '',
    email_subject TEXT NOT NULL DEFAULT '',
    email_date TEXT NOT NULL DEFAULT '',
    email_body TEXT NOT NULL DEFAULT '',
    file_path TEXT NOT NULL,       -- relative to data dir
    sidecar_path TEXT NOT NULL,    -- relative to data dir
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL DEFAULT '',
    email_from TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,          -- accepted | rejected | error
    detail TEXT NOT NULL DEFAULT '',
    processed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id INTEGER NOT NULL,
    field TEXT NOT NULL,
    old_value TEXT NOT NULL,
    new_value TEXT NOT NULL,
    changed_at TEXT NOT NULL
);
"""


def db_path(data_dir: Path) -> Path:
    return data_dir / "ledger.db"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(data_dir: Path):
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path(data_dir))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def insert_receipt(conn: sqlite3.Connection, rec: dict) -> int:
    ts = now_iso()
    cur = conn.execute(
        """INSERT INTO receipts
           (sha256, vendor, date, amount, category, notes,
            email_from, email_subject, email_date, email_body,
            file_path, sidecar_path, created_at, updated_at)
           VALUES (:sha256, :vendor, :date, :amount, :category, :notes,
                   :email_from, :email_subject, :email_date, :email_body,
                   :file_path, :sidecar_path, :created_at, :updated_at)""",
        {**rec, "created_at": rec.get("created_at", ts), "updated_at": ts},
    )
    return cur.lastrowid


def receipt_exists(conn: sqlite3.Connection, sha256: str) -> bool:
    row = conn.execute("SELECT 1 FROM receipts WHERE sha256 = ?", (sha256,)).fetchone()
    return row is not None


def get_receipt(conn: sqlite3.Connection, receipt_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,)).fetchone()


def record_email(conn: sqlite3.Connection, message_id: str, email_from: str,
                 subject: str, status: str, detail: str) -> None:
    conn.execute(
        """INSERT INTO processed_emails (message_id, email_from, subject, status, detail, processed_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (message_id, email_from, subject, status, detail, now_iso()),
    )


def emails_processed_today(conn: sqlite3.Connection) -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM processed_emails WHERE processed_at >= ?", (today,)
    ).fetchone()
    return row["n"]


def record_audit(conn: sqlite3.Connection, receipt_id: int, field: str,
                 old_value: str, new_value: str) -> None:
    conn.execute(
        """INSERT INTO audit_log (receipt_id, field, old_value, new_value, changed_at)
           VALUES (?, ?, ?, ?, ?)""",
        (receipt_id, field, old_value, new_value, now_iso()),
    )
