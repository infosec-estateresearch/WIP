"""Imports — Mastercopy/Salesforce import history and coverage.

Reads wip_import_batch (run telemetry) and, per batch, the reject breakdown.
Read-only: this page does not trigger imports (that stays a shell script).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from src.wip_web.deps import get_db


router = APIRouter(prefix="/imports", tags=["imports"])

# numeric estimated_yield from the raw rejected row
_YIELD = ("CASE WHEN JSON_UNQUOTE(JSON_EXTRACT(row_json,'$.estimated_yield')) "
          "REGEXP '^-?[0-9]+(\\\\.[0-9]+)?$' THEN "
          "CAST(JSON_UNQUOTE(JSON_EXTRACT(row_json,'$.estimated_yield')) AS DECIMAL(15,2)) ELSE 0 END")


def _imported(r):
    return int(r.get("rows_inserted") or 0) + int(r.get("rows_updated") or 0) + int(r.get("rows_unchanged") or 0)


@router.get("", response_class=HTMLResponse)
def list_imports(request: Request, db=Depends(get_db)):
    with db.cursor() as c:
        c.execute("SELECT id, source_type, source_file, source_sheet, started_at, completed_at, "
                  "status, rows_seen, rows_inserted, rows_updated, rows_unchanged, rows_rejected "
                  "FROM wip_import_batch WHERE dry_run = 0 ORDER BY started_at DESC, id DESC")
        rows = list(c.fetchall())
    for r in rows:
        imp = _imported(r)
        seen = int(r.get("rows_seen") or 0)
        r["imported"] = imp
        r["coverage_pct"] = round(100.0 * imp / seen, 1) if seen else None
    return request.app.state.templates.TemplateResponse(
        "imports_list.html", {"request": request, "active": "imports", "rows": rows},
    )


@router.get("/{batch_id}", response_class=HTMLResponse)
def import_detail(batch_id: int, request: Request, db=Depends(get_db)):
    with db.cursor() as c:
        c.execute("SELECT * FROM wip_import_batch WHERE id = %s", (batch_id,))
        batch = c.fetchone()
        if not batch:
            raise HTTPException(status_code=404, detail="Import batch not found")
        c.execute("SELECT reject_reason, COUNT(*) n, SUM(" + _YIELD + ") val "
                  "FROM wip_import_reject WHERE import_batch_id = %s "
                  "GROUP BY reject_reason ORDER BY n DESC", (batch_id,))
        reasons = list(c.fetchall())
        c.execute("SELECT firm_name_normalised, COUNT(*) n, SUM(" + _YIELD + ") val "
                  "FROM wip_import_reject WHERE import_batch_id = %s "
                  "AND firm_name_normalised IS NOT NULL AND firm_name_normalised <> '' "
                  "GROUP BY firm_name_normalised ORDER BY val DESC LIMIT 20", (batch_id,))
        top_firms = list(c.fetchall())
    batch["imported"] = _imported(batch)
    seen = int(batch.get("rows_seen") or 0)
    batch["coverage_pct"] = round(100.0 * batch["imported"] / seen, 1) if seen else None
    return request.app.state.templates.TemplateResponse(
        "imports_detail.html",
        {"request": request, "active": "imports", "batch": batch,
         "reasons": reasons, "top_firms": top_firms},
    )
