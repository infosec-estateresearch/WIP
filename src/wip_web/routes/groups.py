"""Data Review → Company Groups.

List + detail + APPROVE / REJECT / NEEDS_MORE_REVIEW.

The DB writes go through the same functions WO-14A.1's CLI uses
(`src/wip_app/groups/apply.py`). This means:
  - one transaction per decision
  - the schema's partial-unique invariants enforce one APPROVED group
    per canonical / wip_company, one PRIMARY per APPROVED group
  - REJECT writes to wip_company_group_seed_reject so re-seed stays
    informed
The GUI is just a thin layer over those CLI functions — anything the CLI
refuses, the GUI also refuses.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.wip_app.groups.apply import (
    _approve_group, _flag_needs_review, _reject_group,
)
from src.wip_web.deps import get_db


router = APIRouter(prefix="/data-review/groups", tags=["groups"])


VALID_STATUSES = {"PROPOSED", "NEEDS_REVIEW", "APPROVED", "REJECTED"}
ALL_FILTER = "ALL"
DEFAULT_PAGE_SIZE = 25


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

def _load_groups(conn, status_filter: str, page: int, page_size: int,
                 candidates_impact: dict[str, float]) -> tuple[list[dict], int]:
    """Returns (rows, total_count). Rows enriched with impact_score from
    the WO-13 candidates CSV (matched by source_cluster_id)."""
    where = []
    params: list = []
    if status_filter in VALID_STATUSES:
        where.append("g.review_status = %s")
        params.append(status_filter)
    elif status_filter == "PENDING":
        # Default operator view: PROPOSED + NEEDS_REVIEW
        where.append("g.review_status IN ('PROPOSED', 'NEEDS_REVIEW')")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    with conn.cursor() as c:
        c.execute(f"SELECT COUNT(*) AS n FROM wip_company_group g {where_sql}", params)
        total = int((c.fetchone() or {}).get("n") or 0)

        # Roll up current WIP per group via the existing summary view
        c.execute(
            f"""
            SELECT g.id              AS group_id,
                   g.group_name,
                   g.group_name_normalised,
                   g.review_status,
                   g.group_type,
                   g.seed_warning,
                   g.source_cluster_id,
                   g.primary_salesforce_account_id,
                   g.primary_canonical_company_id,
                   g.reviewed_by,
                   g.reviewed_at,
                   COUNT(DISTINCT m.id)                       AS member_count,
                   COALESCE(SUM(cs.current_wip_value), 0)     AS group_current_wip,
                   COALESCE(SUM(cs.current_wip_count), 0)     AS group_active_case_count
              FROM wip_company_group g
              LEFT JOIN wip_company_group_member m ON m.group_id = g.id
              LEFT JOIN v_eric_wip_company_summary cs ON cs.wip_company_id = m.wip_company_id
              {where_sql}
             GROUP BY g.id, g.group_name, g.group_name_normalised,
                      g.review_status, g.group_type, g.seed_warning,
                      g.source_cluster_id, g.primary_salesforce_account_id,
                      g.primary_canonical_company_id, g.reviewed_by, g.reviewed_at
             ORDER BY (g.review_status = 'APPROVED') ASC,
                      g.review_status DESC,
                      g.group_name ASC
             LIMIT %s OFFSET %s
            """,
            params + [page_size, (page - 1) * page_size],
        )
        rows = list(c.fetchall())

    # Attach impact score + sort within the page by impact DESC
    for r in rows:
        r["impact_score"] = candidates_impact.get(r.get("source_cluster_id") or "", 0.0)
    rows.sort(key=lambda r: (-r["impact_score"], r["group_name"]))
    return rows, total


def _load_candidates_impact() -> dict[str, float]:
    """Load just the (cluster_id, impact_score) mapping from the WO-13
    candidates CSV. Failure is non-fatal — we just show 0.0 scores."""
    import csv
    from pathlib import Path
    out: dict[str, float] = {}
    path = Path("/home/WIP/ops/evidence/WO-13/sf_canonical_alignment_candidates.csv")
    if not path.exists():
        return out
    try:
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                try:
                    out[row["cluster_id"]] = float(row.get("impact_score") or 0)
                except (ValueError, KeyError):
                    pass
    except Exception:
        pass
    return out


def _load_group_detail(conn, group_id: int, candidates_csv_row: dict | None) -> dict:
    with conn.cursor() as c:
        c.execute(
            "SELECT * FROM wip_company_group WHERE id = %s",
            (group_id,),
        )
        group = c.fetchone()
        if not group:
            raise HTTPException(status_code=404, detail="group not found")
        c.execute(
            """
            SELECT m.id              AS member_id,
                   m.wip_company_id,
                   m.salesforce_account_id,
                   m.canonical_company_id,
                   m.member_role,
                   m.review_status,
                   wc.salesforce_account_name,
                   wc.account_type,
                   wc.industry,
                   wc.billing_city,
                   wc.billing_postcode,
                   wc.is_potential_duplicate,
                   COALESCE(cs.current_wip_value, 0)  AS current_wip_value,
                   COALESCE(cs.current_wip_count, 0)  AS current_wip_count
              FROM wip_company_group_member m
              JOIN wip_company wc ON wc.id = m.wip_company_id
              LEFT JOIN v_eric_wip_company_summary cs ON cs.wip_company_id = m.wip_company_id
             WHERE m.group_id = %s
             ORDER BY m.member_role DESC, wc.salesforce_account_name ASC
            """,
            (group_id,),
        )
        members = list(c.fetchall())
        group_current_wip = sum(float(m["current_wip_value"] or 0) for m in members)
        group_case_count = sum(int(m["current_wip_count"] or 0) for m in members)

    impact = float(candidates_csv_row.get("impact_score") or 0) if candidates_csv_row else 0.0
    return {
        "group": group,
        "members": members,
        "group_current_wip": group_current_wip,
        "group_active_case_count": group_case_count,
        "impact_score": impact,
        "webinar_attendees": int((candidates_csv_row or {}).get("webinar_attendees") or 0),
        "import_reject_count": int((candidates_csv_row or {}).get("import_reject_count") or 0),
        "distinct_cities": (candidates_csv_row or {}).get("distinct_cities", ""),
        "distinct_account_types": (candidates_csv_row or {}).get("distinct_account_types", ""),
    }


def _load_candidates_csv_row(cluster_id: str | None) -> dict | None:
    """Read the WO-13 row for one cluster_id."""
    if not cluster_id:
        return None
    import csv
    from pathlib import Path
    path = Path("/home/WIP/ops/evidence/WO-13/sf_canonical_alignment_candidates.csv")
    if not path.exists():
        return None
    try:
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                if row.get("cluster_id") == cluster_id:
                    return row
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
def list_groups(
    request: Request,
    status: str = Query("PENDING"),
    page: int = Query(1),
    db=Depends(get_db),
):
    page = max(1, page)  # WO-23: clamp — no 422, no negative offset
    impact = _load_candidates_impact()
    page_size = DEFAULT_PAGE_SIZE
    rows, total = _load_groups(db, status, page, page_size, impact)

    total_pages = max(1, (total + page_size - 1) // page_size)

    return request.app.state.templates.TemplateResponse(
        "data_review/groups_list.html",
        {
            "request": request,
            "active": "data_review",
            "rows": rows,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "status_filter": status,
            "qs_for_pages": urlencode({"status": status}),
        },
    )


@router.get("/{group_id}", response_class=HTMLResponse)
def group_detail(request: Request, group_id: int, db=Depends(get_db)):
    with db.cursor() as c:
        c.execute(
            "SELECT source_cluster_id FROM wip_company_group WHERE id = %s",
            (group_id,),
        )
        r = c.fetchone()
    cluster_id = (r or {}).get("source_cluster_id")
    candidates_row = _load_candidates_csv_row(cluster_id)

    detail = _load_group_detail(db, group_id, candidates_row)

    return request.app.state.templates.TemplateResponse(
        "data_review/groups_detail.html",
        {
            "request": request,
            "active": "data_review",
            **detail,
        },
    )


# ---------------------------------------------------------------------------
# Action handlers — one POST per decision. All writes go through the
# same functions WO-14A.1's CLI uses.
# ---------------------------------------------------------------------------

def _find_group(conn, group_id: int) -> dict:
    with conn.cursor() as c:
        c.execute("SELECT * FROM wip_company_group WHERE id = %s", (group_id,))
        r = c.fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="group not found")
    return r


def _commit_or_raise(conn, group_id: int, action: str):
    try:
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=f"{action} failed: {e}")


def _back_to_detail(group_id: int, *, flash=None, error=None):
    qs = {}
    if flash: qs["flash"] = flash
    if error: qs["error"] = error
    suffix = ("?" + urlencode(qs)) if qs else ""
    return RedirectResponse(url=f"/data-review/groups/{group_id}{suffix}", status_code=303)


@router.post("/{group_id}/approve")
def approve(
    group_id: int,
    set_primary_sf_id: str = Form(...),
    operator_note: str = Form(""),
    db=Depends(get_db),
):
    group = _find_group(db, group_id)
    if group["review_status"] == "APPROVED":
        return _back_to_detail(group_id, flash="Group is already APPROVED.")
    try:
        _approve_group(db, group, set_primary_sf_id.strip() or None,
                       operator_note.strip() or None)
    except Exception as e:
        db.rollback()
        # WO-26: friendly message for the canonical/wip already-approved-elsewhere
        # unique-index conflict instead of a raw SQL error.
        if getattr(e, "args", [None])[0] == 1062:
            return _back_to_detail(group_id, error="One of these accounts is already "
                                   "confirmed in another group. Remove it here, or change "
                                   "the other group first.")
        return _back_to_detail(group_id, error="Could not confirm — nothing was changed.")
    _commit_or_raise(db, group_id, "approve")
    return _back_to_detail(group_id, flash=f"APPROVED. {group['group_name']} is now visible to ERic via the group views.")


@router.post("/{group_id}/reject")
def reject(
    group_id: int,
    operator_note: str = Form(""),
    db=Depends(get_db),
):
    group = _find_group(db, group_id)
    if group["review_status"] == "REJECTED":
        return _back_to_detail(group_id, flash="These accounts are already kept separate.")
    try:
        _reject_group(db, group, operator_note.strip() or None)
    except Exception as e:
        db.rollback()
        return _back_to_detail(group_id, error=f"Could not save: {e}")
    _commit_or_raise(db, group_id, "reject")
    return _back_to_detail(group_id, flash=f"Kept separate. The name '{group['group_name_normalised']}' won't be proposed as a group again.")


@router.post("/{group_id}/needs-review")
def needs_review(
    group_id: int,
    operator_note: str = Form(""),
    db=Depends(get_db),
):
    group = _find_group(db, group_id)
    try:
        _flag_needs_review(db, group, operator_note.strip() or None)
    except Exception as e:
        db.rollback()
        return _back_to_detail(group_id, error=f"Mark-needs-review failed: {e}")
    _commit_or_raise(db, group_id, "needs-review")
    return _back_to_detail(group_id, flash="Marked NEEDS_REVIEW.")
