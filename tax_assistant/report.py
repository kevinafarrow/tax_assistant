"""Year-end reporting: per-category totals and the CSV export."""
from __future__ import annotations

import csv
import io
from pathlib import Path

from . import db
from .categories import SCHEDULE_C_CATEGORIES

CSV_COLUMNS = ("id", "date", "vendor", "amount", "category", "notes",
               "email_from", "email_date", "file_path")


def years(data_dir: Path) -> list[str]:
    with db.connect(data_dir) as conn:
        rows = conn.execute(
            "SELECT DISTINCT substr(date, 1, 4) AS y FROM receipts ORDER BY y DESC"
        ).fetchall()
    return [r["y"] for r in rows]


def category_totals(data_dir: Path, year: str) -> list[dict]:
    with db.connect(data_dir) as conn:
        rows = conn.execute(
            """SELECT category, COUNT(*) AS n, ROUND(SUM(amount), 2) AS total
               FROM receipts WHERE date LIKE ? GROUP BY category""",
            (f"{year}-%",),
        ).fetchall()
    by_cat = {r["category"]: r for r in rows}
    out = []
    for name, prefix in SCHEDULE_C_CATEGORIES.items():
        r = by_cat.get(name)
        if r:
            out.append({"category": name, "line": prefix, "count": r["n"], "total": r["total"]})
    return out


def year_total(data_dir: Path, year: str) -> float:
    with db.connect(data_dir) as conn:
        row = conn.execute(
            "SELECT ROUND(SUM(amount), 2) AS total FROM receipts WHERE date LIKE ?",
            (f"{year}-%",),
        ).fetchone()
    return row["total"] or 0.0


def receipts_for_year(data_dir: Path, year: str) -> list[dict]:
    with db.connect(data_dir) as conn:
        rows = conn.execute(
            "SELECT * FROM receipts WHERE date LIKE ? ORDER BY date DESC, id DESC",
            (f"{year}-%",),
        ).fetchall()
    return [dict(r) for r in rows]


def csv_text(data_dir: Path, year: str) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_COLUMNS)
    for r in receipts_for_year(data_dir, year):
        writer.writerow([r[c] for c in CSV_COLUMNS])
    return buf.getvalue()


def write_csv(data_dir: Path, year: str) -> Path:
    out = data_dir / f"report-{year}.csv"
    out.write_text(csv_text(data_dir, year))
    return out
