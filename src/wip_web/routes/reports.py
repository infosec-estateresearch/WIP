"""Reports — simple read-only rollups over the WIP views.

Top WIP, top unmatched value, firms carrying both, possible duplicates, and
groups awaiting review. Rejected value is always labelled as awaiting review,
never as WIP.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from src.wip_web.deps import get_db


router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("", response_class=HTMLResponse)
def reports(request: Request, db=Depends(get_db)):
    with db.cursor() as c:
        c.execute("SELECT company_name, total_current_wip, active_case_count "
                  "FROM v_eric_wip_firm_with_wip ORDER BY total_current_wip DESC LIMIT 15")
        top_wip = list(c.fetchall())

        c.execute("SELECT firm_name_normalised, reject_count, possible_yield_value "
                  "FROM v_eric_wip_reject_firm_summary ORDER BY possible_yield_value DESC LIMIT 15")
        top_unmatched = list(c.fetchall())

        c.execute("SELECT f.company_name, f.total_current_wip, r.reject_count, r.possible_yield_value "
                  "FROM v_eric_wip_firm_with_wip f "
                  "JOIN wip_company c ON c.id = f.wip_company_id "
                  "JOIN v_eric_wip_reject_firm_summary r ON r.firm_name_normalised = c.account_name_normalised "
                  "ORDER BY r.possible_yield_value DESC LIMIT 15")
        wip_and_rejects = list(c.fetchall())

        c.execute("SELECT duplicate_group_key, candidate_count, status "
                  "FROM wip_company_duplicate_candidate WHERE status IN ('NEW','NEEDS_REVIEW') "
                  "ORDER BY candidate_count DESC, duplicate_group_key ASC LIMIT 15")
        duplicates = list(c.fetchall())

        c.execute("SELECT group_id, group_name, group_review_status, member_count, group_current_wip_value "
                  "FROM v_eric_wip_company_group_candidate_summary "
                  "WHERE group_review_status <> 'APPROVED' "
                  "ORDER BY group_current_wip_value DESC, member_count DESC LIMIT 15")
        groups_pending = list(c.fetchall())

    return request.app.state.templates.TemplateResponse(
        "reports.html",
        {"request": request, "active": "reports", "top_wip": top_wip,
         "top_unmatched": top_unmatched, "wip_and_rejects": wip_and_rejects,
         "duplicates": duplicates, "groups_pending": groups_pending},
    )
