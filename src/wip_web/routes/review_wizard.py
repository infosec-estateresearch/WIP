"""WO-21 — Simple Duplicate Review Wizard.

Customer-safe, idiot-proof workflow: one possible duplicate / firm group at a
time, plain English, three buttons (Yes same firm / No different / Not sure),
with commercial context (confirmed WIP + open cases vs awaiting-review rejects +
possible unmatched value, kept separate). Decisions map onto the existing,
non-destructive group actions. Nothing is merged or deleted — only review/status
records change, and every decision is reversible.

  Yes, same firm   -> approve group   (visible to ERic group views)
  No, different    -> reject group     (sticky — not re-proposed)
  Not sure         -> needs review     (set aside, unchanged)
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.wip_web.deps import get_db
from src.wip_app.groups.apply import _approve_group, _reject_group, _flag_needs_review


router = APIRouter(prefix="/duplicate-review", tags=["duplicate-review"])

# A review item is a firm-group suggestion not yet decided.
PENDING = ("PROPOSED", "NEEDS_REVIEW")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _group(db, gid):
    with db.cursor() as c:
        c.execute("SELECT * FROM wip_company_group WHERE id = %s", (gid,))
        return c.fetchone()


def _members(db, gid):
    """Members with per-account WIP, open cases and rejected-row context."""
    with db.cursor() as c:
        c.execute(
            "SELECT m.salesforce_account_id, wc.salesforce_account_name AS name, "
            "wc.billing_city AS city, wc.account_type, wc.account_name_normalised AS norm, "
            "COALESCE(s.current_wip_value,0) AS wip, COALESCE(s.current_wip_count,0) AS cases "
            "FROM wip_company_group_member m "
            "JOIN wip_company wc ON wc.id = m.wip_company_id "
            "LEFT JOIN v_eric_wip_company_summary s ON s.wip_company_id = m.wip_company_id "
            "WHERE m.group_id = %s ORDER BY wip DESC, name", (gid,))
        members = list(c.fetchall())
        for m in members:
            c.execute("SELECT COALESCE(SUM(reject_count),0) rc, COALESCE(SUM(possible_yield_value),0) rv "
                      "FROM v_eric_wip_reject_firm_summary WHERE firm_name_normalised = %s", (m["norm"],))
            r = c.fetchone() or {}
            m["reject_count"] = int(r.get("rc") or 0)
            m["reject_value"] = float(r.get("rv") or 0)
    return members


def _money(db, group, members):
    """Confirmed (WIP/cases) vs awaiting-review (rejects/possible value)."""
    wip = sum(float(m["wip"] or 0) for m in members)
    cases = sum(int(m["cases"] or 0) for m in members)
    norms = {group["group_name_normalised"]} | {m["norm"] for m in members}
    norms = [n for n in norms if n]
    rc = rv = 0
    if norms:
        ph = ",".join(["%s"] * len(norms))
        with db.cursor() as c:
            c.execute("SELECT COALESCE(SUM(reject_count),0) rc, COALESCE(SUM(possible_yield_value),0) rv "
                      "FROM v_eric_wip_reject_firm_summary WHERE firm_name_normalised IN (" + ph + ")", norms)
            r = c.fetchone() or {}
            rc, rv = int(r.get("rc") or 0), float(r.get("rv") or 0)
    return {"wip": wip, "cases": cases, "reject_count": rc, "reject_value": rv}


def _pick_primary(members, group):
    if not members:
        return None
    existing = group.get("primary_salesforce_account_id")
    ids = {m["salesforce_account_id"] for m in members}
    if existing in ids:
        return existing
    return members[0]["salesforce_account_id"]  # highest WIP (members ordered by wip)


def _counts(db):
    with db.cursor() as c:
        c.execute("SELECT review_status, COUNT(*) n FROM wip_company_group GROUP BY review_status")
        by = {r["review_status"]: int(r["n"]) for r in c.fetchall()}
        c.execute("SELECT COUNT(*) n FROM wip_company_group g WHERE g.review_status IN ('PROPOSED','NEEDS_REVIEW') "
                  "AND EXISTS (SELECT 1 FROM wip_company_group_member m JOIN v_eric_wip_company_summary s "
                  "ON s.wip_company_id = m.wip_company_id WHERE m.group_id = g.id AND s.current_wip_value > 0)")
        high = int((c.fetchone() or {}).get("n") or 0)
    return {
        "waiting": by.get("PROPOSED", 0) + by.get("NEEDS_REVIEW", 0),
        "proposed": by.get("PROPOSED", 0),
        "not_sure": by.get("NEEDS_REVIEW", 0),
        "confirmed": by.get("APPROVED", 0),
        "rejected": by.get("REJECTED", 0),
        "high_priority": high,
    }


def _next_pending(db, after_id=0):
    ph = ",".join(["%s"] * len(PENDING))
    with db.cursor() as c:
        c.execute(
            "SELECT g.id FROM wip_company_group g WHERE g.review_status IN (" + ph + ") AND g.id > %s "
            "ORDER BY (SELECT COALESCE(SUM(s.current_wip_value),0) FROM wip_company_group_member m "
            "LEFT JOIN v_eric_wip_company_summary s ON s.wip_company_id = m.wip_company_id WHERE m.group_id = g.id) DESC, "
            "g.id ASC LIMIT 1", list(PENDING) + [after_id])
        row = c.fetchone()
        if row:
            return row["id"]
        # wrap to the first pending one
        c.execute("SELECT id FROM wip_company_group WHERE review_status IN (" + ph + ") ORDER BY id ASC LIMIT 1", list(PENDING))
        row = c.fetchone()
    return row["id"] if row else None


def _record_reviewer(db, gid, reviewer):
    reviewer = (reviewer or "").strip()
    if not reviewer:
        return
    with db.cursor() as c:
        c.execute("UPDATE wip_company_group SET reviewed_by = %s WHERE id = %s", (reviewer, gid))
        c.execute("UPDATE wip_company_group_member SET reviewed_by = %s WHERE group_id = %s", (reviewer, gid))
    db.commit()


def _t(request, name, ctx):
    ctx.update({"request": request, "active": "duplicate_review"})
    return request.app.state.templates.TemplateResponse(name, ctx)


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
def dashboard(request: Request, db=Depends(get_db)):
    counts = _counts(db)
    with db.cursor() as c:
        c.execute("SELECT COUNT(*) n FROM wip_company_duplicate_candidate WHERE status IN ('NEW','NEEDS_REVIEW')")
        counts["duplicate_candidates"] = int((c.fetchone() or {}).get("n") or 0)
    next_id = _next_pending(db)
    return _t(request, "wizard/dashboard.html", {"counts": counts, "next_id": next_id})


@router.get("/next", response_class=HTMLResponse)
def review_next(request: Request, after: int = 0, db=Depends(get_db)):
    nid = _next_pending(db, after)
    if not nid:
        return RedirectResponse(url="/duplicate-review?flash=All+caught+up.+Nothing+left+to+review.", status_code=303)
    return RedirectResponse(url=f"/duplicate-review/item/{nid}", status_code=303)


@router.get("/item/{gid}", response_class=HTMLResponse)
def review_item(gid: int, request: Request, db=Depends(get_db)):
    group = _group(db, gid)
    if not group:
        raise HTTPException(status_code=404, detail="Review item not found")
    members = _members(db, gid)
    money = _money(db, group, members)
    counts = _counts(db)
    return _t(request, "wizard/item.html",
              {"group": group, "members": members, "money": money, "counts": counts})


@router.post("/item/{gid}/decide")
def decide(gid: int, decision: str = Form(...), reviewer: str = Form(""), db=Depends(get_db)):
    group = _group(db, gid)
    if not group:
        raise HTTPException(status_code=404, detail="Review item not found")
    try:
        if decision == "yes":
            members = _members(db, gid)
            primary = _pick_primary(members, group)
            _approve_group(db, group, primary, None)
            msg = "Confirmed. These records will now be reported together as one firm in WIP and ERic."
        elif decision == "no":
            _reject_group(db, group, None)
            msg = "Confirmed. These records will stay separate and this suggestion will not be shown again."
        elif decision == "notsure":
            _flag_needs_review(db, group, None)
            msg = "No problem. This has been set aside for later review. Nothing has been changed."
        else:
            raise HTTPException(status_code=400, detail="Unknown decision")
        db.commit()
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        return RedirectResponse(url=f"/duplicate-review/item/{gid}?error=Could+not+save:+{e}", status_code=303)
    _record_reviewer(db, gid, reviewer)
    from urllib.parse import quote
    return RedirectResponse(url=f"/duplicate-review/next?after={gid}&flash={quote(msg)}", status_code=303)


@router.get("/confirmed", response_class=HTMLResponse)
def confirmed(request: Request, db=Depends(get_db)):
    with db.cursor() as c:
        c.execute("SELECT g.id AS group_id, g.group_name, g.reviewed_by, g.reviewed_at, "
                  "s.member_count, s.group_current_wip_value "
                  "FROM wip_company_group g "
                  "JOIN v_eric_wip_company_group_candidate_summary s ON s.group_id = g.id "
                  "WHERE g.review_status = 'APPROVED' "
                  "ORDER BY s.group_current_wip_value DESC, g.group_name")
        rows = list(c.fetchall())
    return _t(request, "wizard/confirmed.html", {"rows": rows})


@router.get("/rejected", response_class=HTMLResponse)
def rejected(request: Request, db=Depends(get_db)):
    with db.cursor() as c:
        c.execute("SELECT id AS group_id, group_name, reviewed_by, reviewed_at "
                  "FROM wip_company_group WHERE review_status = 'REJECTED' ORDER BY reviewed_at DESC, group_name")
        rows = list(c.fetchall())
    return _t(request, "wizard/rejected.html", {"rows": rows})


@router.post("/item/{gid}/change")
def change_decision(gid: int, action: str = Form(...), reviewer: str = Form(""), db=Depends(get_db)):
    """Reverse / change a previous decision. Non-destructive: only flips status.
      action=not_sure   -> back to NEEDS_REVIEW (from approved or rejected)
      action=different   -> mark REJECTED (sticky)
    """
    group = _group(db, gid)
    if not group:
        raise HTTPException(status_code=404, detail="Item not found")
    try:
        if action == "different":
            _reject_group(db, group, None)
            dest = "/duplicate-review/rejected"
        else:  # not_sure / reopen
            _flag_needs_review(db, group, None)
            dest = "/duplicate-review"
        db.commit()
    except Exception as e:
        db.rollback()
        return RedirectResponse(url=f"{dest}?error=Could+not+change:+{e}", status_code=303)
    _record_reviewer(db, gid, reviewer)
    return RedirectResponse(url=f"{dest}?flash=Decision+changed.+Nothing+was+merged+or+deleted.", status_code=303)
