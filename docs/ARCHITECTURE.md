# WIP — Architecture

## Position in the Estate Research data landscape

```
                ┌──────────────────────────────┐
                │  Salesforce (system of record│
                │  for client firm identity)   │
                └────────────┬─────────────────┘
                             │ AccountId
                             ▼
┌────────────────────┐  ┌────────────────────┐  ┌────────────────────┐
│  Excel Mastercopy  │  │  Case Manager (CM) │  │  Learning Hub (LH) │
│  (being retired)   │  │  authoritative for │  │  webinars + people │
│                    │  │  incidents/sectors │  │                    │
└─────────┬──────────┘  └─────────┬──────────┘  └─────────┬──────────┘
          │                       │                       │
          │ CSV import (transition only)                  │
          ▼                       │                       │
┌──────────────────────────────────────────────────────────────────┐
│                         WIP (this app)                            │
│  nif_wip.*                                                        │
│  Owns: case-level WIP lifecycle, financial state, audit          │
│  Aligns to: Salesforce.AccountId, canonical_company_id           │
└─────────────────────────┬─────────────────────────────────────────┘
                          │ SQL views (read-only)
                          ▼
┌──────────────────────────────────────────────────────────────────┐
│                         ERic (/home/ELT)                          │
│  Chat proxy, retrieval API, planner, synthesiser, catalogue       │
│  Consumes: WIP via read-only SELECT on nif_wip views              │
│  Never writes to WIP tables.                                      │
└──────────────────────────────────────────────────────────────────┘
```

## Design principles

1. **WIP is authority for its own data.** Tables under `nif_wip.*` are the
   single source of truth for WIP records. Other systems consume, never
   override.
2. **Salesforce alignment, not Salesforce ownership.** Every WIP record
   carries `salesforce_account_id` where available, but a missing or stale
   Salesforce link does not block WIP record creation.
3. **Canonical resolution required.** Every WIP firm reference resolves to
   `canonical_company_id` via the same deterministic paths ERic uses
   (canonical_company / entity_alias / sf_account_company_bridge). No
   free-text-only firm references.
4. **CSV as transitional ingestion source.** During migration, the Excel
   master copy feeds into WIP via a one-way import with lineage tracking
   (each WIP row knows which CSV row/sheet/version it came from). Eventually
   the CSV path is retired in favour of direct WIP UI/API entry.
5. **All mutations audited.** Every change to a WIP record writes an
   `wip_audit_log` row recording who/when/what changed.
6. **Read-only views for downstream.** ERic and any future consumer reads
   through versioned SQL views, not raw tables. View signatures are
   contracts; breaking changes require a new view version.

## Database

- **Instance**: shared `localhost:3336` MariaDB (same instance as `nif_elt`)
- **Database name**: `nif_wip` (separate from `nif_elt`)
- **Users**:
  - `wip_app` — full DDL + DML on `nif_wip.*`, no access to `nif_elt`
  - `wip_read` — SELECT only on `nif_wip.v_*` views (granted to ERic later)

## Planned table outline (WO-9B will firm this up)

```
wip_firms                  — canonical firm reference, Salesforce/canonical link
wip_cases                  — case lifecycle, status, instructing firm
wip_case_status            — historical status transitions
wip_financials             — per-period financial state (WIP, fees, yield)
wip_source_imports         — CSV import provenance per row
wip_audit_log              — who/when/what for every mutation
```

Plus read-only views:

```
v_wip_active_cases
v_wip_firms_with_current_wip
v_wip_firm_period_totals
v_wip_audit_recent
```

## Integration with ERic

See `docs/ERIC_INTEGRATION_BOUNDARY.md`.

## What WIP does NOT do

- Run LLM calls or talk to OpenWebUI
- Maintain ERic's semantic catalogue or business-term registry
- Own the canonical_company / canonical_person / entity_alias tables (those
  remain in `nif_elt`, owned by the ERic ingestion / resolution stack)
- Run the FCO promotion job (also remains in ERic — that aggregates Case
  Manager snapshots, separate from WIP's own state)
