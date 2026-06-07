"""Data Review → Duplicate Accounts.

Backed by wip_company_duplicate_candidate. Actions:
  - Mark False Positive       → status = IGNORED_FALSE_POSITIVE
  - Mark Resolved in SF       → status = RESOLVED_IN_SALESFORCE
  - Add Note                  → resolution_note update only

NO merge button — Salesforce owns the merge. The GUI's job here is to
record what the operator decided about the cluster, not to act on the
underlying SF Accounts.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.wip_web.deps import get_db


router = APIRouter(prefix="/data-review/duplicates", tags=["duplicates"])


VALID_STATUSES = {"NEW", "NEEDS_REVIEW", "RESOLVED_IN_SALESFORCE", "IGNORED_FALSE_POSITIVE"}
DEFAULT_PAGE_SIZE = 25


def _back(candidate_id=None, flash=None, error=None):
    qs = {}
    if flash: qs["flash"] = flash
    if error: qs["error"] = error
    suffix = ("?" + urlencode(qs)) if qs else ""
    if candidate_id:
        return RedirectResponse(url=f"/data-review/duplicates/{candidate_id}{suffix}", status_code=303)
    return RedirectResponse(url=f"/data-review/duplicates{suffix}", status_code=303)


@router.get("", response_class=HTMLResponse)
def list_duplicates(
    request: Request,
    status: str = Query("OPEN"),
    page: int = Query(1),
    db=Depends(get_db),
):
    page = max(1, page)  # WO-23: clamp — no 422, no negative offset
    where = []
    params: list = []
    if status in VALID_STATUSES:
        where.append("status = %s")
        params.append(status)
    elif status == "OPEN":
        where.append("status IN ('NEW','NEEDS_REVIEW')")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    page_size = DEFAULT_PAGE_SIZE

    with db.cursor() as c:
        c.execute(f"SELECT COUNT(*) AS n FROM wip_company_duplicate_candidate {where_sql}", params)
        total = int((c.fetchone() or {}).get("n") or 0)
        c.execute(
            f"""
            SELECT id, duplicate_group_key, candidate_count, status,
                   first_detected_at, last_seen_at,
                   reviewed_by, reviewed_at, resolution_note
              FROM wip_company_duplicate_candidate
              {where_sql}
             ORDER BY (status IN ('NEW','NEEDS_REVIEW')) DESC,
                      candidate_count DESC, duplicate_group_key ASC
             LIMIT %s OFFSET %s
            """,
            params + [page_size, (page - 1) * page_size],
        )
        rows = list(c.fetchall())

    total_pages = max(1, (total + page_size - 1) // page_size)
    return request.app.state.templates.TemplateResponse(
        "data_review/duplicates_list.html",
        {
            "request": request, "active": "data_review",
            "rows": rows, "total": total, "page": page,
            "total_pages": total_pages, "status_filter": status,
            "qs_for_pages": urlencode({"status": status}),
            "page_size": page_size,
        },
    )


@router.get("/{candidate_id}", response_class=HTMLResponse)
def duplicate_detail(request: Request, candidate_id: int, db=Depends(get_db)):
    with db.cursor() as c:
        c.execute(
            "SELECT * FROM wip_company_duplicate_candidate WHERE id = %s",
            (candidate_id,),
        )
        cand = c.fetchone()
        if not cand:
            raise HTTPException(status_code=404, detail="duplicate candidate not found")

    sf_ids = json.loads(cand.get("salesforce_account_ids_json") or "[]")
    members = []
    if sf_ids:
        placeholders = ",".join(["%s"] * len(sf_ids))
        with db.cursor() as c:
            c.execute(
                f"SELECT id, salesforce_account_id, salesforce_account_name, "
                f"       account_type, industry, billing_city, billing_postcode, "
                f"       canonical_company_id, is_active_in_salesforce "
                f"  FROM wip_company WHERE salesforce_account_id IN ({placeholders})",
                sf_ids,
            )
            members = list(c.fetchall())

    return request.app.state.templates.TemplateResponse(
        "data_review/duplicates_detail.html",
        {
            "request": request, "active": "data_review",
            "cand": cand, "members": members,
            "sf_account_ids": sf_ids,
        },
    )


def _update_status(db, candidate_id: int, new_status: str, note: str | None, action: str):
    if new_status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"invalid status '{new_status}'")
    now = datetime.now(timezone.utc)
    with db.cursor() as c:
        c.execute(
            "UPDATE wip_company_duplicate_candidate SET "
            "  status = %s, "
            "  reviewed_by = %s, reviewed_at = %s, "
            "  resolution_note = COALESCE(%s, resolution_note) "
            "WHERE id = %s",
            (new_status, "operator-gui", now, note, candidate_id),
        )
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"{action} failed: {e}")


@router.post("/{candidate_id}/mark-false-positive")
def mark_false_positive(candidate_id: int, operator_note: str = Form(""), db=Depends(get_db)):
    _update_status(db, candidate_id, "IGNORED_FALSE_POSITIVE",
                   operator_note.strip() or None, "mark-false-positive")
    return _back(candidate_id, flash="Marked IGNORED_FALSE_POSITIVE.")


@router.post("/{candidate_id}/mark-resolved-sf")
def mark_resolved_sf(candidate_id: int, operator_note: str = Form(""), db=Depends(get_db)):
    _update_status(db, candidate_id, "RESOLVED_IN_SALESFORCE",
                   operator_note.strip() or None, "mark-resolved-sf")
    return _back(candidate_id, flash="Marked RESOLVED_IN_SALESFORCE.")


@router.post("/{candidate_id}/note")
def add_note(candidate_id: int, operator_note: str = Form(...), db=Depends(get_db)):
    now = datetime.now(timezone.utc)
    with db.cursor() as c:
        c.execute(
            "UPDATE wip_company_duplicate_candidate SET "
            "  resolution_note = %s, "
            "  reviewed_by = %s, reviewed_at = %s "
            "WHERE id = %s",
            (operator_note.strip(), "operator-gui", now, candidate_id),
        )
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"note add failed: {e}")
    return _back(candidate_id, flash="Note saved.")
