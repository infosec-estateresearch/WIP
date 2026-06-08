"""WO-27 — Add / Edit WIP entry against an existing firm (cutover-gated).

Flow:
  - GET  /company/{id}/wip/new   add form, firm pre-selected + locked
  - GET  /wip/new                search/select an existing firm first
  - POST /company/{id}/wip/new   create (only when manual writes enabled)
  - GET  /wip/case/{id}/edit     edit form (imported rows render but can't save)
  - POST /wip/case/{id}/edit     update (only manual rows, only when enabled)

No route creates a company. While WIP_MANUAL_WRITES_ENABLED is off, the GET
forms render with the Submit button disabled and the POST handlers refuse to
write (HTTP 403, no mutation).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.wip_app.manual_entry import (
    ValidationError, create_manual_wip_case, update_manual_wip_case, validate_entry,
)
from src.wip_web.deps import get_db
from src.wip_web.feature_flags import (
    CUTOVER_MESSAGE, SUBMIT_DISABLED_HELP, manual_writes_enabled,
)

router = APIRouter(tags=["wip-entry"])
ACTOR = "operator"


# --------------------------------------------------------------------------- #
# loaders
# --------------------------------------------------------------------------- #

def _company(db, company_id: int) -> dict:
    with db.cursor() as c:
        c.execute("SELECT id, salesforce_account_id, salesforce_account_name, "
                  "canonical_company_id, account_type, billing_city "
                  "FROM wip_company WHERE id = %s", (company_id,))
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Firm not found")
    return row


def _case_with_company(db, case_id: int) -> dict:
    with db.cursor() as c:
        c.execute(
            "SELECT wc.id, wc.wip_company_id, wc.salesforce_account_id, wc.case_reference, "
            "  wc.case_name, wc.status, wc.manual_note, wc.source, wc.created_by, "
            "  co.salesforce_account_name, "
            "  f.wip_value, f.estimated_yield, f.fees_invoiced, f.as_of_date "
            "FROM wip_case wc "
            "JOIN wip_company co ON co.id = wc.wip_company_id "
            "LEFT JOIN wip_case_financial f ON f.id = ("
            "  SELECT id FROM wip_case_financial WHERE wip_case_id = wc.id "
            "  ORDER BY as_of_date DESC, id DESC LIMIT 1) "
            "WHERE wc.id = %s", (case_id,))
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="WIP case not found")
    return row


def _ctx(request, **extra) -> dict:
    base = {
        "request": request,
        "writes_enabled": manual_writes_enabled(),
        "cutover_message": CUTOVER_MESSAGE,
        "submit_help": SUBMIT_DISABLED_HELP,
        "errors": {},
        "form": {},
    }
    base.update(extra)
    return base


def _render(request, template, status_code=200, **extra):
    return request.app.state.templates.TemplateResponse(
        template, _ctx(request, **extra), status_code=status_code)


# --------------------------------------------------------------------------- #
# Add
# --------------------------------------------------------------------------- #

@router.get("/wip/new", response_class=HTMLResponse)
def select_firm(request: Request, q: str = Query(""), db=Depends(get_db)):
    """Step 1 from Live Cases: search/select an existing firm (no creation)."""
    rows = []
    q = q.strip()
    if q:
        with db.cursor() as c:
            c.execute(
                "SELECT c.id, c.salesforce_account_name AS name, c.billing_city, "
                "  COALESCE(s.current_wip_value,0) AS wip, COALESCE(s.current_wip_count,0) AS cases "
                "FROM wip_company c "
                "LEFT JOIN v_eric_wip_company_summary s ON s.wip_company_id = c.id "
                "WHERE c.salesforce_account_name LIKE %s OR c.account_name_normalised LIKE %s "
                "ORDER BY s.current_wip_value DESC, c.salesforce_account_name LIMIT 50",
                (f"%{q}%", f"%{q.lower()}%"))
            rows = list(c.fetchall())
    return _render(request, "wip_entry/select_firm.html", active="live_cases", q=q, rows=rows)


@router.get("/company/{company_id}/wip/new", response_class=HTMLResponse)
def add_form(company_id: int, request: Request, db=Depends(get_db)):
    company = _company(db, company_id)
    return _render(request, "wip_entry/new.html", active="companies", company=company)


@router.post("/company/{company_id}/wip/new", response_class=HTMLResponse)
async def add_submit(company_id: int, request: Request, db=Depends(get_db)):
    company = _company(db, company_id)
    form = dict(await request.form())
    if not manual_writes_enabled():
        # Cutover gate: render the form back, Submit still disabled, no write.
        return _render(request, "wip_entry/new.html", status_code=403,
                       active="companies", company=company, form=form, blocked=True)
    try:
        cleaned = validate_entry(form)
        case_id = create_manual_wip_case(db, company, cleaned, ACTOR)
        db.commit()
    except ValidationError as ve:
        db.rollback()
        return _render(request, "wip_entry/new.html", status_code=422,
                       active="companies", company=company, form=form, errors=ve.errors)
    except Exception:
        db.rollback()
        return _render(request, "wip_entry/new.html", status_code=422, active="companies",
                       company=company, form=form,
                       errors={"_": "Could not save — nothing was changed."})
    from urllib.parse import quote
    msg = (f"WIP entry added for {company['salesforce_account_name']}. "
           "It is now visible in Live Cases and the firm summary.")
    return RedirectResponse(url=f"/companies/{company_id}?flash={quote(msg)}#case-{case_id}",
                            status_code=303)


# --------------------------------------------------------------------------- #
# Edit
# --------------------------------------------------------------------------- #

@router.get("/wip/case/{case_id}/edit", response_class=HTMLResponse)
def edit_form(case_id: int, request: Request, db=Depends(get_db)):
    case = _case_with_company(db, case_id)
    return _render(request, "wip_entry/edit.html", active="live_cases", case=case,
                   is_manual=(case["source"] == "manual"))


@router.post("/wip/case/{case_id}/edit", response_class=HTMLResponse)
async def edit_submit(case_id: int, request: Request, db=Depends(get_db)):
    case = _case_with_company(db, case_id)
    is_manual = case["source"] == "manual"
    form = dict(await request.form())
    # Imported Mastercopy rows are never editable here, regardless of the flag.
    if not is_manual or not manual_writes_enabled():
        return _render(request, "wip_entry/edit.html", status_code=403, active="live_cases",
                       case=case, is_manual=is_manual, form=form, blocked=True)
    try:
        cleaned = validate_entry(form)
        update_manual_wip_case(db, case, cleaned, ACTOR)
        db.commit()
    except ValidationError as ve:
        db.rollback()
        return _render(request, "wip_entry/edit.html", status_code=422, active="live_cases",
                       case=case, is_manual=is_manual, form=form, errors=ve.errors)
    except Exception:
        db.rollback()
        return _render(request, "wip_entry/edit.html", status_code=422, active="live_cases",
                       case=case, is_manual=is_manual, form=form,
                       errors={"_": "Could not save — nothing was changed."})
    from urllib.parse import quote
    msg = f"WIP entry updated for {case['salesforce_account_name']}."
    return RedirectResponse(url=f"/companies/{case['wip_company_id']}?flash={quote(msg)}",
                            status_code=303)
