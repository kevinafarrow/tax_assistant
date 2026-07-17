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

CREATE TABLE IF NOT EXISTS api_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_input_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL,                 -- NULL when the model had no known pricing
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS balance_anchors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    balance_usd REAL NOT NULL,     -- what the Anthropic Console showed
    noted_at TEXT NOT NULL         -- when it was synced (cli set-balance)
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


def record_api_call(conn: sqlite3.Connection, model: str, input_tokens: int,
                    output_tokens: int, cache_creation_input_tokens: int,
                    cache_read_input_tokens: int, cost_usd: float | None) -> None:
    conn.execute(
        """INSERT INTO api_calls
           (model, input_tokens, output_tokens, cache_creation_input_tokens,
            cache_read_input_tokens, cost_usd, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (model, input_tokens, output_tokens, cache_creation_input_tokens,
         cache_read_input_tokens, cost_usd, now_iso()),
    )


def api_spend_since(conn: sqlite3.Connection, since: str) -> float:
    """Total recorded API cost since an ISO date/timestamp ('' = all time)."""
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM api_calls WHERE created_at >= ?",
        (since,),
    ).fetchone()
    return float(row["total"])


def record_balance_anchor(conn: sqlite3.Connection, balance_usd: float) -> None:
    conn.execute(
        "INSERT INTO balance_anchors (balance_usd, noted_at) VALUES (?, ?)",
        (balance_usd, now_iso()),
    )


def latest_balance_anchor(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT balance_usd, noted_at FROM balance_anchors ORDER BY noted_at DESC, id DESC LIMIT 1"
    ).fetchone()


def api_call_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM api_calls").fetchone()["n"]


def record_audit(conn: sqlite3.Connection, receipt_id: int, field: str,
                 old_value: str, new_value: str) -> None:
    conn.execute(
        """INSERT INTO audit_log (receipt_id, field, old_value, new_value, changed_at)
           VALUES (?, ?, ?, ?, ?)""",
        (receipt_id, field, old_value, new_value, now_iso()),
    )
