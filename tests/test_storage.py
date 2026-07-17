import json

import pytest

from tax_assistant import db, report, storage
from tax_assistant.extractor import Extraction, ExtractionError, _validate

EMAIL_META = {
    "from": "kevin@example.com",
    "subject": "lunch",
    "date": "2026-06-14T12:00:00+00:00",
    "body": "client lunch with Sam",
}


def make_extraction(**overrides):
    kwargs = dict(vendor="Delta Airlines", date="2026-06-14", amount=412.5,
                  category="Travel", notes="flight to client site")
    kwargs.update(overrides)
    return Extraction(**kwargs)


def store(tmp_path, **overrides):
    ex = make_extraction(**overrides)
    return storage.store_receipt(
        tmp_path, b"fake-image-bytes" + str(overrides).encode(), "image/jpeg",
        ex, ex.date or "2026-06-14", EMAIL_META,
    )


def test_store_creates_file_sidecar_and_row(tmp_path):
    rid = store(tmp_path)
    files = list(tmp_path.rglob("*.jpg"))
    sidecars = list((tmp_path / "receipts").rglob("*.json"))
    assert len(files) == 1 and len(sidecars) == 1
    assert "2026/24a-Travel/2026-06-14_delta-airlines_412.50.jpg" in str(files[0])
    data = json.loads(sidecars[0].read_text())
    assert data["vendor"] == "Delta Airlines" and data["amount"] == 412.5
    with db.connect(tmp_path) as conn:
        row = db.get_receipt(conn, rid)
    assert row["category"] == "Travel"


def test_filename_collision_gets_suffix(tmp_path):
    store(tmp_path)
    store(tmp_path, notes="second copy, different bytes")
    files = sorted(f.name for f in tmp_path.rglob("*.jpg"))
    assert files == [
        "2026-06-14_delta-airlines_412.50-2.jpg",
        "2026-06-14_delta-airlines_412.50.jpg",
    ]


def test_duplicate_detection(tmp_path):
    payload = b"same-bytes"
    ex = make_extraction()
    storage.store_receipt(tmp_path, payload, "image/jpeg", ex, ex.date, EMAIL_META)
    with db.connect(tmp_path) as conn:
        assert db.receipt_exists(conn, storage.sha256_hex(payload))
        assert not db.receipt_exists(conn, storage.sha256_hex(b"other"))


def test_apply_edit_moves_file_and_audits(tmp_path):
    rid = store(tmp_path)
    storage.apply_edit(tmp_path, rid, {"category": "Meals", "amount": 86.13})
    with db.connect(tmp_path) as conn:
        row = db.get_receipt(conn, rid)
        audit = conn.execute("SELECT * FROM audit_log").fetchall()
    assert row["category"] == "Meals" and row["amount"] == 86.13
    assert "24b-Meals" in row["file_path"]
    assert (tmp_path / row["file_path"]).exists()
    assert (tmp_path / row["sidecar_path"]).exists()
    # old files are gone
    assert not list((tmp_path / "receipts/2026/24a-Travel").glob("*"))
    assert {a["field"] for a in audit} == {"category", "amount"}


def test_apply_edit_rejects_bad_values(tmp_path):
    rid = store(tmp_path)
    with pytest.raises(ValueError):
        storage.apply_edit(tmp_path, rid, {"amount": -5})
    with pytest.raises(ValueError):
        storage.apply_edit(tmp_path, rid, {"category": "Bribes"})


def test_rebuild_db_from_sidecars(tmp_path):
    store(tmp_path)
    store(tmp_path, vendor="Home Depot", category="Supplies", amount=86.13)
    db.db_path(tmp_path).unlink()
    assert storage.rebuild_db(tmp_path) == 2
    totals = report.category_totals(tmp_path, "2026")
    assert {t["category"]: t["total"] for t in totals} == {"Travel": 412.5, "Supplies": 86.13}


def test_quarantine_writes_note(tmp_path):
    storage.quarantine(tmp_path, b"junk", "../evil name.jpg", "unreadable", EMAIL_META)
    items = storage.list_quarantine(tmp_path)
    assert len(items) == 1 and items[0]["reason"] == "unreadable"
    # hostile filename was sanitized
    assert not any(".." in str(p) for p in (tmp_path / "quarantine").iterdir())


def test_csv_export(tmp_path):
    store(tmp_path)
    text = report.csv_text(tmp_path, "2026")
    assert "Delta Airlines" in text and "412.5" in text


def test_validate_rejects_bad_extractions():
    good = {"vendor": "X", "date": "2026-06-14", "amount": 10.0,
            "category": "Meals", "notes": ""}
    assert _validate(good).amount == 10.0
    for bad in (
        {**good, "vendor": ""},
        {**good, "amount": None},
        {**good, "amount": "12.50"},
        {**good, "amount": -3},
        {**good, "category": "Fun"},
        {**good, "date": "06/14/2026"},
        {**good, "date": "1993-01-01"},
    ):
        with pytest.raises(ExtractionError):
            _validate(bad)


def test_validate_allows_null_date():
    good = {"vendor": "X", "date": None, "amount": 10.0, "category": "Meals", "notes": ""}
    assert _validate(good).date is None


def test_slugify():
    assert storage.slugify("Delta Airlines, Inc.") == "delta-airlines-inc"
    assert storage.slugify("  ") == "unknown"
    assert storage.slugify("a" * 100).startswith("a") and len(storage.slugify("a" * 100)) == 40


def test_transient_api_classification():
    import httpx
    import anthropic
    from tax_assistant.extractor import _is_transient

    def status_error(code, message):
        req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        resp = httpx.Response(code, request=req, json={
            "type": "error", "error": {"type": "invalid_request_error", "message": message}})
        return anthropic.APIStatusError(message, response=resp,
                                        body={"error": {"message": message}})

    assert _is_transient(status_error(429, "rate limited"))
    assert _is_transient(status_error(529, "overloaded"))
    assert _is_transient(status_error(500, "boom"))
    assert _is_transient(status_error(
        400, "Your credit balance is too low to access the Anthropic API."))
    assert not _is_transient(status_error(400, "image exceeds maximum dimensions"))
    assert not _is_transient(status_error(404, "model not found"))
