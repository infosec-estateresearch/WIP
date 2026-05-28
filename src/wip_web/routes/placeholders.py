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


@router.get("/companies", response_class=HTMLResponse)
def companies(request: Request):
    return _placeholder(request, "companies", "Companies",
        "Salesforce-mirrored companies live in wip_company. Read-only views "
        "exist (v_eric_wip_company_summary, v_eric_wip_firm_with_wip). The "
        "company browser GUI is queued behind the Data Review surface.")


@router.get("/wip-cases", response_class=HTMLResponse)
def wip_cases(request: Request):
    return _placeholder(request, "wip_cases", "WIP Cases",
        "Per-case WIP lives in wip_case + wip_case_financial. Read-only via "
        "v_eric_wip_current_cases. Editing UI not built yet — operational "
        "edits go through the Mastercopy import flow.")


@router.get("/imports", response_class=HTMLResponse)
def imports(request: Request):
    return _placeholder(request, "imports", "Imports",
        "Mastercopy import history is in wip_import_batch (run-by-run "
        "telemetry). Use scripts/import_mastercopy_wip.py to apply a "
        "new import; reject queue is under Data Review → Import Rejects.")


@router.get("/reports", response_class=HTMLResponse)
def reports(request: Request):
    return _placeholder(request, "reports", "Reports",
        "Reports are queued. Current report sources: "
        "v_eric_wip_firm_with_wip (top firms by current WIP), "
        "v_eric_wip_import_coverage (import coverage signal for ERic).")


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
