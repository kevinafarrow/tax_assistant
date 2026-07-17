"""Web UI: password-gated browsing, running totals, receipt viewing/editing,
CSV export, quarantine listing. Binds to the LAN only (see docker-compose)."""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, TimestampSigner

from . import db, report, storage
from .categories import CATEGORY_NAMES

log = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

SESSION_COOKIE = "tax_assistant_session"
PUBLIC_PATHS = ("/login", "/healthz")


def _signer(request: Request) -> TimestampSigner:
    return TimestampSigner(request.app.state.cfg.session_secret)


def is_authenticated(request: Request) -> bool:
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return False
    max_age = request.app.state.cfg.session_max_age_days * 86400
    try:
        _signer(request).unsign(cookie, max_age=max_age)
        return True
    except BadSignature:
        return False


async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path not in PUBLIC_PATHS and not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


@router.get("/healthz")
def healthz() -> PlainTextResponse:
    return PlainTextResponse("ok")


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
def login(request: Request, password: str = Form(...)):
    cfg = request.app.state.cfg
    if not cfg.web_password or not secrets.compare_digest(password, cfg.web_password):
        log.warning("Failed web UI login from %s", request.client.host if request.client else "?")
        return templates.TemplateResponse(
            request, "login.html", {"error": "Wrong password."}, status_code=401
        )
    resp = RedirectResponse("/", status_code=303)
    token = _signer(request).sign(b"ok").decode()
    resp.set_cookie(
        SESSION_COOKIE, token,
        max_age=cfg.session_max_age_days * 86400,
        httponly=True, samesite="strict",
    )
    return resp


@router.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@router.get("/")
def index(request: Request):
    current = str(datetime.now(timezone.utc).year)
    return RedirectResponse(f"/year/{current}", status_code=303)


@router.get("/year/{year}", response_class=HTMLResponse)
def year_view(request: Request, year: str):
    data_dir = request.app.state.cfg.data_dir
    return templates.TemplateResponse(request, "year.html", {
        "year": year,
        "years": report.years(data_dir),
        "totals": report.category_totals(data_dir, year),
        "grand_total": report.year_total(data_dir, year),
        "receipts": report.receipts_for_year(data_dir, year),
        "quarantine_count": len(storage.list_quarantine(data_dir)),
    })


@router.get("/receipt/{receipt_id}", response_class=HTMLResponse)
def receipt_view(request: Request, receipt_id: int, saved: int = 0):
    data_dir = request.app.state.cfg.data_dir
    with db.connect(data_dir) as conn:
        row = db.get_receipt(conn, receipt_id)
        if row is None:
            return PlainTextResponse("not found", status_code=404)
        audit = conn.execute(
            "SELECT * FROM audit_log WHERE receipt_id = ? ORDER BY id DESC", (receipt_id,)
        ).fetchall()
    return templates.TemplateResponse(request, "receipt.html", {
        "r": dict(row),
        "audit": [dict(a) for a in audit],
        "categories": CATEGORY_NAMES,
        "years": report.years(data_dir),
        "saved": saved,
        "error": None,
        "is_pdf": row["file_path"].endswith(".pdf"),
    })


@router.post("/receipt/{receipt_id}")
def receipt_edit(request: Request, receipt_id: int,
                 vendor: str = Form(...), date: str = Form(...),
                 amount: float = Form(...), category: str = Form(...),
                 notes: str = Form("")):
    data_dir = request.app.state.cfg.data_dir
    try:
        storage.apply_edit(data_dir, receipt_id, {
            "vendor": vendor, "date": date, "amount": amount,
            "category": category, "notes": notes,
        })
    except (ValueError, KeyError) as exc:
        with db.connect(data_dir) as conn:
            row = db.get_receipt(conn, receipt_id)
        if row is None:
            return PlainTextResponse("not found", status_code=404)
        return templates.TemplateResponse(request, "receipt.html", {
            "r": dict(row), "audit": [], "categories": CATEGORY_NAMES,
            "years": report.years(data_dir),
            "saved": 0, "error": str(exc),
            "is_pdf": row["file_path"].endswith(".pdf"),
        }, status_code=400)
    return RedirectResponse(f"/receipt/{receipt_id}?saved=1", status_code=303)


@router.get("/receipt/{receipt_id}/image")
def receipt_image(request: Request, receipt_id: int):
    data_dir = request.app.state.cfg.data_dir
    with db.connect(data_dir) as conn:
        row = db.get_receipt(conn, receipt_id)
    if row is None:
        return PlainTextResponse("not found", status_code=404)
    # Resolve against the data dir and refuse anything that escapes it.
    path = (data_dir / row["file_path"]).resolve()
    if not path.is_relative_to(data_dir.resolve()) or not path.exists():
        return PlainTextResponse("file missing", status_code=404)
    return FileResponse(path)


@router.get("/report/{year}.csv")
def report_csv(request: Request, year: str):
    data_dir = request.app.state.cfg.data_dir
    return Response(
        report.csv_text(data_dir, year),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="report-{year}.csv"'},
    )


@router.get("/quarantine", response_class=HTMLResponse)
def quarantine_view(request: Request):
    data_dir = request.app.state.cfg.data_dir
    return templates.TemplateResponse(request, "quarantine.html", {
        "items": storage.list_quarantine(data_dir),
        "years": report.years(data_dir),
    })
