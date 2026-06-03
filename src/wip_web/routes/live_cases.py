"""Live Cases — imported, Salesforce-aligned WIP cases only.

Reads v_eric_wip_current_cases (cases that landed cleanly and carry current
WIP). Rejected Mastercopy rows are NOT shown here — they live under Data Review
→ Unmatched Mastercopy Rows. This page is the "trusted/aligned" surface.
"""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from src.wip_web.deps import get_db


router = APIRouter(prefix="/live-cases", tags=["live-cases"])
PAGE_SIZE = 50


@router.get("", response_class=HTMLResponse)
def list_cases(
    request: Request,
    firm: str = Query(""),
    status: str = Query(""),
    sort: str = Query("wip"),
    has_rejects: int = Query(0),
    page: int = Query(1, ge=1, le=10_000),
    db=Depends(get_db),
):
    where, params = ["1=1"], []
    if firm:
        where.append("cc.company_name LIKE %s"); params.append(f"%{firm.strip()}%")
    if status:
        where.append("cc.status = %s"); params.append(status)
    if has_rejects:
        where.append("EXISTS (SELECT 1 FROM v_eric_wip_reject_firm_summary r "
                     "WHERE r.firm_name_normalised = co.account_name_normalised)")
    where_sql = "WHERE " + " AND ".join(where)
    order_sql = "ORDER BY wc.updated_at DESC, cc.current_wip_value DESC" if sort == "recent" \
        else "ORDER BY cc.current_wip_value DESC, wc.updated_at DESC"

    base = ("FROM v_eric_wip_current_cases cc "
            "JOIN wip_case wc ON wc.id = cc.wip_case_id "
            "LEFT JOIN wip_company co ON co.salesforce_account_id = cc.salesforce_account_id " + where_sql)
    with db.cursor() as c:
        c.execute("SELECT COUNT(*) n " + base, params)
        total = int((c.fetchone() or {}).get("n") or 0)
        c.execute(
            "SELECT cc.wip_case_id, cc.case_reference, cc.case_name, cc.case_type, "
            "cc.company_name, cc.status, cc.current_wip_value AS wip, "
            "cc.current_estimated_yield AS ey, cc.current_fees_invoiced AS fees, "
            "cc.financial_as_of, wc.source, wc.updated_at " + base +
            " " + order_sql + " LIMIT %s OFFSET %s",
            params + [PAGE_SIZE, (page - 1) * PAGE_SIZE],
        )
        rows = list(c.fetchall())
        c.execute("SELECT DISTINCT status FROM v_eric_wip_current_cases WHERE status IS NOT NULL ORDER BY status")
        statuses = [r["status"] for r in c.fetchall()]

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    qs = urlencode({"firm": firm, "status": status, "sort": sort, "has_rejects": has_rejects or ""})
    return request.app.state.templates.TemplateResponse(
        "live_cases_list.html",
        {"request": request, "active": "live_cases", "rows": rows, "total": total,
         "page": page, "total_pages": total_pages, "statuses": statuses,
         "f_firm": firm, "f_status": status, "f_sort": sort, "f_has_rejects": has_rejects,
         "qs_for_pages": qs},
    )
