"""Dashboard — system state cards."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from src.wip_web.deps import get_db


router = APIRouter()


def _import_health(latest_import):
    """WO-22: honest freshness for the latest completed (non-dry-run) Mastercopy
    import. GREEN < 24h, AMBER > 24h, RED > 7 days or never."""
    if not latest_import or not latest_import.get("completed_at"):
        return {"level": "red", "label": "no completed import", "stale": True}
    age = datetime.now() - latest_import["completed_at"]
    hours = age.total_seconds() / 3600.0
    if hours < 24:
        level = "green"
    elif hours < 24 * 7:
        level = "amber"
    else:
        level = "red"
    if hours < 1:
        label = "%d min ago" % int(age.total_seconds() / 60)
    elif hours < 48:
        label = "%d hours ago" % int(hours)
    else:
        label = "%d days ago" % int(hours / 24)
    return {"level": level, "label": label, "hours": round(hours, 1), "stale": level != "green"}


def _scalar(c, sql, params=()):
    c.execute(sql, params)
    r = c.fetchone()
    if not r:
        return None
    return next(iter(r.values()))


def _row(c, sql, params=()):
    c.execute(sql, params)
    return c.fetchone()


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db=Depends(get_db)):
    with db.cursor() as c:
        sf_companies = _scalar(c,
            "SELECT COUNT(*) AS n FROM wip_company WHERE is_active_in_salesforce = 1")
        wip_cases = _scalar(c,
            "SELECT COUNT(*) AS n FROM wip_case")
        open_rejects = _scalar(c,
            "SELECT COUNT(*) AS n FROM wip_import_reject "
            "WHERE resolution_status IN ('NEW', 'NEEDS_REVIEW')")
        # WO-23B: split the reject queue into work the customer can act on
        # (a firm name we couldn't match) vs source-data gaps (no firm name).
        _actionable = ("resolution_status IN ('NEW','NEEDS_REVIEW') "
                       "AND firm_name_normalised IS NOT NULL AND firm_name_normalised <> '' "
                       "AND reject_reason IN ('NO_COMPANY_MATCH','MULTIPLE_COMPANY_MATCHES','POTENTIAL_DUPLICATE_COMPANY')")
        review_item_firms = _scalar(c,
            "SELECT COUNT(DISTINCT firm_name_normalised) AS n FROM wip_import_reject WHERE " + _actionable)
        source_gap_rows = _scalar(c,
            "SELECT COUNT(*) AS n FROM wip_import_reject "
            "WHERE resolution_status IN ('NEW','NEEDS_REVIEW') AND NOT (" + _actionable + ")")
        dup_groups = _scalar(c,
            "SELECT COUNT(*) AS n FROM wip_company_duplicate_candidate "
            "WHERE status IN ('NEW', 'NEEDS_REVIEW')")
        groups_pending = _scalar(c,
            "SELECT COUNT(*) AS n FROM wip_company_group "
            "WHERE review_status IN ('PROPOSED', 'NEEDS_REVIEW')")
        groups_approved = _scalar(c,
            "SELECT COUNT(*) AS n FROM wip_company_group "
            "WHERE review_status = 'APPROVED'")
        latest_sync = _row(c,
            "SELECT status, started_at, completed_at, sync_type "
            "FROM wip_sync_run ORDER BY id DESC LIMIT 1")
        latest_import = _row(c,
            "SELECT status, completed_at, rows_seen, rows_inserted, "
            "       rows_updated, rows_rejected "
            "FROM wip_import_batch WHERE dry_run = 0 "
            "ORDER BY id DESC LIMIT 1")

    return request.app.state.templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "active": "dashboard",
            "sf_companies": sf_companies or 0,
            "wip_cases": wip_cases or 0,
            "open_rejects": open_rejects or 0,
            "review_item_firms": review_item_firms or 0,
            "source_gap_rows": source_gap_rows or 0,
            "dup_groups": dup_groups or 0,
            "groups_pending": groups_pending or 0,
            "groups_approved": groups_approved or 0,
            "latest_sync": latest_sync,
            "latest_import": latest_import,
            "import_health": _import_health(latest_import),
        },
    )
