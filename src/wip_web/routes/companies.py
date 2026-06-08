"""Companies / Firms — the primary browse surface.

Salesforce-aligned companies (wip_company) with WIP status, firm-group status,
and the rejected-Mastercopy value waiting against each firm. Read-only browse:
no company creation, no Salesforce merge, no duplicate resolution here — those
stay in Salesforce / Data Review. Rejected rows are shown as *waiting*, never
folded into the WIP figure (WO-20 safety rule).
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from src.wip_web.deps import get_db


router = APIRouter(prefix="/companies", tags=["companies"])

PAGE_SIZE = 50


def _group_status_map(db, canonical_ids):
    """canonical_company_id -> (group_name, review_status) for any-status groups."""
    ids = [c for c in canonical_ids if c]
    if not ids:
        return {}
    ph = ",".join(["%s"] * len(ids))
    with db.cursor() as c:
        c.execute(
            "SELECT canonical_company_id, group_name, group_review_status "
            "FROM v_eric_wip_company_group_candidate_member "
            "WHERE canonical_company_id IN (" + ph + ")",
            ids,
        )
        return {r["canonical_company_id"]: (r["group_name"], r["group_review_status"]) for r in c.fetchall()}


def _reject_map(db, norms):
    """normalised firm name -> (reject_count, possible_value)."""
    keys = [n for n in norms if n]
    if not keys:
        return {}
    ph = ",".join(["%s"] * len(keys))
    with db.cursor() as c:
        c.execute(
            "SELECT firm_name_normalised, reject_count, possible_yield_value "
            "FROM v_eric_wip_reject_firm_summary WHERE firm_name_normalised IN (" + ph + ")",
            keys,
        )
        return {r["firm_name_normalised"]: (int(r["reject_count"] or 0), float(r["possible_yield_value"] or 0)) for r in c.fetchall()}


def _sibling_counts(db, canonical_ids):
    ids = [c for c in canonical_ids if c]
    if not ids:
        return {}
    ph = ",".join(["%s"] * len(ids))
    with db.cursor() as c:
        c.execute(
            "SELECT canonical_company_id, COUNT(*) n FROM wip_company "
            "WHERE canonical_company_id IN (" + ph + ") GROUP BY canonical_company_id",
            ids,
        )
        return {r["canonical_company_id"]: int(r["n"]) for r in c.fetchall()}


@router.get("", response_class=HTMLResponse)
def list_companies(
    request: Request,
    q: str = Query(""),
    has_wip: int = Query(0),
    possible_dup: int = Query(0),
    page: int = Query(1),
    db=Depends(get_db),
):
    page = max(1, page)  # WO-23: clamp — no 422, no negative offset
    where, params = ["c.is_active_in_salesforce = 1"], []
    if q:
        where.append("(c.salesforce_account_name LIKE %s OR c.account_name_normalised LIKE %s)")
        like = f"%{q.strip()}%"; params += [like, like]
    if has_wip:
        where.append("COALESCE(s.current_wip_value,0) > 0")
    if possible_dup:
        where.append("c.is_potential_duplicate = 1")
    where_sql = "WHERE " + " AND ".join(where)

    with db.cursor() as c:
        c.execute(
            "SELECT COUNT(*) n FROM wip_company c "
            "LEFT JOIN v_eric_wip_company_summary s ON s.wip_company_id = c.id " + where_sql,
            params,
        )
        total = int((c.fetchone() or {}).get("n") or 0)
        c.execute(
            "SELECT c.id, c.salesforce_account_name AS name, c.account_name_normalised AS norm, "
            "c.account_type, c.billing_city, c.canonical_company_id, c.is_potential_duplicate, "
            "c.last_salesforce_sync_at, COALESCE(s.current_wip_value,0) AS wip, "
            "COALESCE(s.current_wip_count,0) AS cases "
            "FROM wip_company c "
            "LEFT JOIN v_eric_wip_company_summary s ON s.wip_company_id = c.id " + where_sql +
            " ORDER BY wip DESC, name ASC LIMIT %s OFFSET %s",
            params + [PAGE_SIZE, (page - 1) * PAGE_SIZE],
        )
        rows = list(c.fetchall())

    groups = _group_status_map(db, [r["canonical_company_id"] for r in rows])
    rejects = _reject_map(db, [r["norm"] for r in rows])
    siblings = _sibling_counts(db, [r["canonical_company_id"] for r in rows])
    for r in rows:
        gn, gs = groups.get(r["canonical_company_id"], (None, None))
        r["group_name"], r["group_status"] = gn, gs
        rc, rv = rejects.get(r["norm"], (0, 0.0))
        r["reject_count"], r["reject_value"] = rc, rv
        r["sf_account_count"] = siblings.get(r["canonical_company_id"], 1)

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    qs = urlencode({"q": q, "has_wip": has_wip or "", "possible_dup": possible_dup or ""})
    return request.app.state.templates.TemplateResponse(
        "companies_list.html",
        {"request": request, "active": "companies", "rows": rows, "total": total,
         "page": page, "total_pages": total_pages, "f_q": q, "f_has_wip": has_wip,
         "f_possible_dup": possible_dup, "qs_for_pages": qs},
    )


@router.get("/{company_id}", response_class=HTMLResponse)
def company_detail(company_id: int, request: Request, db=Depends(get_db)):
    with db.cursor() as c:
        c.execute("SELECT * FROM wip_company WHERE id = %s", (company_id,))
        company = c.fetchone()
        if not company:
            raise HTTPException(status_code=404, detail="Company not found")
        canon = company["canonical_company_id"]
        # WIP summary for this account
        c.execute("SELECT current_wip_value, current_wip_count, latest_wip_date "
                  "FROM v_eric_wip_company_summary WHERE wip_company_id = %s", (company_id,))
        summary = c.fetchone() or {}
        # sibling SF accounts under same canonical
        siblings = []
        group = None
        if canon:
            c.execute("SELECT id, salesforce_account_name, account_type, billing_city, is_potential_duplicate "
                      "FROM wip_company WHERE canonical_company_id = %s AND id <> %s ORDER BY salesforce_account_name",
                      (canon, company_id))
            siblings = list(c.fetchall())
            c.execute("SELECT group_id, group_name, group_name_normalised, group_review_status, "
                      "member_count, group_current_wip_value "
                      "FROM v_eric_wip_company_group_candidate_summary "
                      "WHERE primary_canonical_company_id = %s OR group_id IN "
                      "(SELECT group_id FROM v_eric_wip_company_group_candidate_member WHERE canonical_company_id = %s) "
                      "LIMIT 1", (canon, canon))
            group = c.fetchone()
        # cases for this account (+ wip_case_id and source for Edit links / Manual badge)
        c.execute("SELECT cc.wip_case_id, cc.case_reference, cc.case_name, cc.case_type, "
                  "cc.status, cc.current_wip_value, cc.current_estimated_yield, "
                  "cc.current_fees_invoiced, cc.financial_as_of, wc.source "
                  "FROM v_eric_wip_current_cases cc JOIN wip_case wc ON wc.id = cc.wip_case_id "
                  "WHERE cc.salesforce_account_id = %s "
                  "ORDER BY cc.current_wip_value DESC LIMIT 200", (company["salesforce_account_id"],))
        cases = list(c.fetchall())
        # rejected rows matched by this account's normalised name AND, when the
        # firm is grouped, the group's normalised name (the Hugh James case:
        # rejects normalise to "hugh james" but the account is "hugh james london").
        norms = {company["account_name_normalised"]}
        if group and group.get("group_name_normalised"):
            norms.add(group["group_name_normalised"])
        norms = [n for n in norms if n]
        reject = None
        if norms:
            ph = ",".join(["%s"] * len(norms))
            c.execute("SELECT COALESCE(SUM(reject_count),0) reject_count, "
                      "COALESCE(SUM(possible_yield_value),0) possible_yield_value, "
                      "GROUP_CONCAT(DISTINCT reject_reasons) reject_reasons "
                      "FROM v_eric_wip_reject_firm_summary WHERE firm_name_normalised IN (" + ph + ")",
                      norms)
            reject = c.fetchone()
            if reject and not int(reject["reject_count"] or 0):
                reject = None

    return request.app.state.templates.TemplateResponse(
        "companies_detail.html",
        {"request": request, "active": "companies", "company": company,
         "summary": summary, "siblings": siblings, "group": group, "cases": cases,
         "reject": reject},
    )
