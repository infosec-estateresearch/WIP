"""Placeholders for nav items not yet built (Companies / WIP Cases /
Imports / Reports / Admin / Alias Review). Keeps the toolbar mental
model intact without pretending the pages do something."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse


router = APIRouter()


def _placeholder(request: Request, key: str, title: str, note: str):
    return request.app.state.templates.TemplateResponse(
        "placeholder.html",
        {"request": request, "active": key, "page_title": title, "note": note},
    )


# /companies, /live-cases, /imports, /reports are now real pages
# (routes/companies.py, live_cases.py, imports.py, reports.py) — WO-20.


@router.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    return _placeholder(request, "admin", "Admin",
        "DB users (wip_app for writes, wip_read for reads), migrations "
        "(scripts/run_migration.py), and sync schedule (wip-sf-sync.timer + "
        "wip-sf-sync-nightly.timer) — managed via shell, not GUI for now.")


@router.get("/data-review/alias-review", response_class=HTMLResponse)
def alias_review(request: Request):
    return _placeholder(request, "data_review", "Alias Review",
        "Alias review for canonical_company is owned by ERic (ELT side) "
        "via scripts/generate_alias_review_pack.py. Not surfaced here yet — "
        "this is a WIP-side GUI.")
