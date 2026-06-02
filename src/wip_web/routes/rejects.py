"""Data Review → Import Rejects.

Lists wip_import_reject with filters (reject_reason, firm_name search,
batch_id, status). Per-row: Mark Ignored, Mark Needs Review, View row.

No "create company" button. Companies are created in Salesforce; this
GUI only records what the operator decided about the rejected row.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.wip_web.deps import get_db


router = APIRouter(prefix="/data-review/rejects", tags=["rejects"])


REJECT_REASONS = [
    "NO_COMPANY_MATCH",
    "MULTIPLE_COMPANY_MATCHES",
    "POTENTIAL_DUPLICATE_COMPANY",
    "MISSING_FIRM_NAME",
    "MISSING_CASE_REFERENCE",
    "INVALID_FINANCIAL_VALUE",
    "INVALID_DATE",
    "DUPLICATE_SOURCE_ROW",
    "UNSUPPORTED_ROW_SHAPE",
]
RESOLUTION_STATUSES = ["NEW", "NEEDS_REVIEW", "RESOLVED", "IGNORED", "REJECTED"]
DEFAULT_PAGE_SIZE = 50

# numeric estimated_yield expression for "high value first" sorting (WO-15A)
_YIELD_NUM = (
    "CASE WHEN JSON_UNQUOTE(JSON_EXTRACT(row_json,'$.estimated_yield')) "
    "REGEXP '^-?[0-9]+(\\\\.[0-9]+)?$' THEN "
    "CAST(JSON_UNQUOTE(JSON_EXTRACT(row_json,'$.estimated_yield')) AS DECIMAL(15,2)) END"
)
# does the rejected row's firm normalise to a known (any-status) WIP group?
_IN_GROUP = (
    "EXISTS (SELECT 1 FROM wip_company_group g "
    "WHERE g.group_name_normalised = wip_import_reject.firm_name_normalised)"
)


def _parse_reject_content(raw) -> dict:
    """Pull the human-meaningful content out of the raw Mastercopy row so the
    operator sees what is waiting, not just why it bounced."""
    try:
        d = json.loads(raw) if raw else {}
    except Exception:
        d = {}
    if not isinstance(d, dict):
        d = {}
    try:
        y = float(d.get("estimated_yield"))
    except (TypeError, ValueError):
        y = None
    return {
        "case_name": d.get("case_name"),
        "case_number": d.get("case_number"),
        "case_type": d.get("case_type"),
        "estate_value": d.get("estate_value"),
        "estimated_yield": d.get("estimated_yield"),
        "estimated_yield_num": y,
        "case_status": d.get("status"),
        "instruction_date": d.get("instruction_date"),
    }


def _back(reject_id=None, *, flash=None, error=None, qs_passthrough: str = ""):
    qs = {}
    if flash: qs["flash"] = flash
    if error: qs["error"] = error
    suffix = ("?" + urlencode(qs)) if qs else ""
    if reject_id:
        return RedirectResponse(url=f"/data-review/rejects/{reject_id}{suffix}", status_code=303)
    base = f"/data-review/rejects{('?' + qs_passthrough) if qs_passthrough else ''}"
    return RedirectResponse(url=base, status_code=303)


@router.get("", response_class=HTMLResponse)
def list_rejects(
    request: Request,
    reject_reason: str = Query(""),
    firm: str = Query(""),
    batch_id: Optional[int] = Query(None),
    status: str = Query("OPEN"),
    sort: str = Query("recent"),
    possible_group: int = Query(0),
    page: int = Query(1, ge=1, le=10_000),
    db=Depends(get_db),
):
    where: list[str] = []
    params: list = []
    if reject_reason and reject_reason in REJECT_REASONS:
        where.append("reject_reason = %s"); params.append(reject_reason)
    if firm:
        where.append("(firm_name_raw LIKE %s OR firm_name_normalised LIKE %s)")
        like = f"%{firm.strip()}%"
        params.extend([like, like])
    if batch_id:
        where.append("import_batch_id = %s"); params.append(batch_id)
    if status in RESOLUTION_STATUSES:
        where.append("resolution_status = %s"); params.append(status)
    elif status == "OPEN":
        where.append("resolution_status IN ('NEW','NEEDS_REVIEW')")
    if possible_group:
        where.append(_IN_GROUP)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    # "High value first" surfaces the rows worth chasing; default is newest.
    if sort == "value":
        order_sql = f"ORDER BY ({_YIELD_NUM}) IS NULL ASC, ({_YIELD_NUM}) DESC, id DESC"
    else:
        order_sql = "ORDER BY id DESC"

    page_size = DEFAULT_PAGE_SIZE
    with db.cursor() as c:
        c.execute(f"SELECT COUNT(*) AS n FROM wip_import_reject {where_sql}", params)
        total = int((c.fetchone() or {}).get("n") or 0)
        c.execute(
            f"""
            SELECT id, import_batch_id, source_sheet, source_row_number,
                   firm_name_raw, firm_name_normalised, case_reference_raw,
                   reject_reason, resolution_status, created_at,
                   reviewed_by, reviewed_at, row_json,
                   {_IN_GROUP} AS in_group
              FROM wip_import_reject
              {where_sql}
             {order_sql}
             LIMIT %s OFFSET %s
            """,
            params + [page_size, (page - 1) * page_size],
        )
        rows = list(c.fetchall())
        for r in rows:
            r["content"] = _parse_reject_content(r.pop("row_json", None))
            r["in_group"] = bool(r.get("in_group"))

        # Distinct batch ids for the filter dropdown
        c.execute("SELECT DISTINCT import_batch_id FROM wip_import_reject "
                  " ORDER BY import_batch_id DESC")
        batch_ids = [int(r["import_batch_id"]) for r in c.fetchall() if r.get("import_batch_id")]

    total_pages = max(1, (total + page_size - 1) // page_size)

    # Preserve filters in pagination links
    qs_for_pages = urlencode({
        "reject_reason": reject_reason, "firm": firm,
        "batch_id": batch_id or "", "status": status,
        "sort": sort, "possible_group": possible_group or "",
    })

    return request.app.state.templates.TemplateResponse(
        "data_review/rejects_list.html",
        {
            "request": request, "active": "data_review",
            "rows": rows, "total": total, "page": page,
            "total_pages": total_pages, "page_size": page_size,
            "reject_reasons": REJECT_REASONS,
            "resolution_statuses": ["OPEN"] + RESOLUTION_STATUSES + ["ALL"],
            "batch_ids": batch_ids,
            "f_reason": reject_reason, "f_firm": firm,
            "f_batch": batch_id, "f_status": status,
            "f_sort": sort, "f_possible_group": possible_group,
            "qs_for_pages": qs_for_pages,
        },
    )


@router.get("/{reject_id}", response_class=HTMLResponse)
def reject_detail(request: Request, reject_id: int, db=Depends(get_db)):
    with db.cursor() as c:
        c.execute("SELECT * FROM wip_import_reject WHERE id = %s", (reject_id,))
        rej = c.fetchone()
    if not rej:
        raise HTTPException(status_code=404, detail="reject not found")

    candidate_ids = json.loads(rej.get("candidate_company_ids_json") or "null") or []
    candidate_names = json.loads(rej.get("candidate_company_names_json") or "null") or []
    try:
        row_json_pretty = json.dumps(json.loads(rej["row_json"]), indent=2, default=str)
    except Exception:
        row_json_pretty = rej.get("row_json") or ""

    return request.app.state.templates.TemplateResponse(
        "data_review/rejects_detail.html",
        {
            "request": request, "active": "data_review",
            "rej": rej,
            "candidate_ids": candidate_ids,
            "candidate_names": candidate_names,
            "row_json_pretty": row_json_pretty,
        },
    )


def _update_reject_status(db, reject_id: int, new_status: str, note: str | None, action: str):
    if new_status not in RESOLUTION_STATUSES:
        raise HTTPException(status_code=400, detail=f"invalid status '{new_status}'")
    now = datetime.now(timezone.utc)
    with db.cursor() as c:
        c.execute(
            "UPDATE wip_import_reject SET "
            "  resolution_status = %s, "
            "  reviewed_by = %s, reviewed_at = %s, "
            "  resolution_note = COALESCE(%s, resolution_note) "
            "WHERE id = %s",
            (new_status, "operator-gui", now, note, reject_id),
        )
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"{action} failed: {e}")


@router.post("/{reject_id}/mark-ignored")
def mark_ignored(reject_id: int, operator_note: str = Form(""), db=Depends(get_db)):
    _update_reject_status(db, reject_id, "IGNORED",
                          operator_note.strip() or None, "mark-ignored")
    return _back(reject_id, flash="Marked IGNORED.")


@router.post("/{reject_id}/mark-needs-review")
def mark_needs_review(reject_id: int, operator_note: str = Form(""), db=Depends(get_db)):
    _update_reject_status(db, reject_id, "NEEDS_REVIEW",
                          operator_note.strip() or None, "mark-needs-review")
    return _back(reject_id, flash="Marked NEEDS_REVIEW.")
