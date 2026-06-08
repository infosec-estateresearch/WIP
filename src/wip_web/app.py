"""WIP GUI — FastAPI server-side rendered operator interface.

Internal only. No auth (per WO-14A.4 spec — internal LAN/VPN
deployment assumed). Adds an auth layer later if exposed beyond local
trusted network.

Tech stack:
  - FastAPI (already in use by chat_proxy/api in /home/ELT)
  - Jinja2 server-side templates
  - HTMX for in-page partial updates without a build chain
  - Bootstrap 5.3 via CDN

Run:
  python -m uvicorn src.wip_web.app:app --host 127.0.0.1 --port 8300
or via systemd: `systemctl start wip-web`.

Routes are organised under src/wip_web/routes/. The only DB write paths
are the explicit action endpoints (Data Review forms). Every action
honours the WO-14B blueprint rule: GUI does not create Salesforce
Accounts, does not merge companies, does not silently change canonical
identity — those decisions belong in Salesforce.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.wip_web.routes import (
    dashboard, groups, duplicates, rejects, placeholders,
    companies, live_cases, imports, reports, review_wizard,
)


_HERE = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(_HERE / "templates"))


# Human-friendly labels for internal ENUM values. Keep raw values in
# URL params, DB writes, and tests — only the displayed label changes.
_STATUS_LABELS = {
    # wip_company_group.review_status + wip_company_group_member.review_status
    "PROPOSED":                 "Pending review",
    "NEEDS_REVIEW":             "Needs review",
    "APPROVED":                 "Confirmed",
    "REJECTED":                 "Kept separate",
    # wip_company_duplicate_candidate.status
    "NEW":                      "New",
    "RESOLVED_IN_SALESFORCE":   "Resolved in Salesforce",
    "IGNORED_FALSE_POSITIVE":   "Not a duplicate",
    # wip_import_reject.resolution_status
    "RESOLVED":                 "Resolved",
    "IGNORED":                  "Ignored",
    # wip_company_group.group_type
    "FIRM_GROUP":               "Firm group",
    "BRAND_GROUP":              "Brand group",
    "BRANCH_GROUP":             "Branch group",
    "NETWORK_GROUP":            "Network group",
    "UNKNOWN":                  "Unknown",
    # wip_company_group_member.member_role
    "PRIMARY":                  "Primary (HQ)",
    "BRANCH":                   "Branch",
    "OFFICE":                   "Office",
    "ALIAS":                    "Alias",
    "RELATED_ACCOUNT":          "Related account",
    # wip_company_group.seed_warning
    "mixed_account_types":      "Mixed account types — check for false positive",
    "wide_geographic_split":    "Many locations — confirm it's one firm",
}


def human_status(value):
    """Jinja2 filter: ENUM → friendly label. Unknown values pass through."""
    if value is None:
        return ""
    return _STATUS_LABELS.get(str(value), str(value))


TEMPLATES.env.filters["human_status"] = human_status

app = FastAPI(
    title="Estate Research WIP",
    description="Internal operator GUI for the WIP application.",
    version="0.1.0",
    docs_url="/docs",
)

app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

# Make TEMPLATES discoverable by route modules without circular imports.
app.state.templates = TEMPLATES

# Routers
app.include_router(dashboard.router)
app.include_router(groups.router)
app.include_router(duplicates.router)
app.include_router(rejects.router)
app.include_router(companies.router)
app.include_router(live_cases.router)
app.include_router(imports.router)
app.include_router(reports.router)
app.include_router(review_wizard.router)
app.include_router(placeholders.router)


@app.get("/healthz", response_class=HTMLResponse, include_in_schema=False)
def healthz():
    return "ok"
