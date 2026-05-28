# WIP — Architecture

## Position in the Estate Research data landscape

```
                ┌──────────────────────────────────┐
                │   Salesforce (Account = master   │
                │   for COMPANY IDENTITY)          │
                └────────────────┬─────────────────┘
                                 │ AccountId, Name, Type,
                                 │ Industry, Owner, Billing*
                                 ▼
            ┌───────────────────────────────────────┐
            │  Hourly Salesforce Account sync       │
            │  (runs in /home/WIP)                  │
            └────────────────┬──────────────────────┘
                             │
                             ▼
┌───────────────────────────────────────────────────────────────────┐
│                       WIP (this application)                       │
│                                                                    │
│   wip_company     ← mirror of Salesforce Account (1 row per SF id) │
│   wip_case        ← WIP-specific operational records               │
│   wip_case_financial                                               │
│   wip_status_history                                               │
│   wip_import_batch                                                 │
│   wip_audit_log                                                    │
│                                                                    │
│   Identity rule: wip_case.wip_company_id REQUIRES a row in         │
│   wip_company, which can only exist via Salesforce sync.           │
└─────────────────────────┬──────────────────────────────────────────┘
                          │ read-only SQL views (v_eric_*)
                          ▼
┌───────────────────────────────────────────────────────────────────┐
│                         ERic (/home/ELT)                           │
│   Reads: v_eric_wip_company_summary, v_eric_wip_current_*          │
│   Matches by: canonical_company_id (WIP stores this for ERic's     │
│   cross-source joins; SF AccountId is WIP's primary key)           │
│   Writes to WIP: NEVER.                                            │
└───────────────────────────────────────────────────────────────────┘
```

## Design principles (post WO-9B clarification)

1. **Salesforce Account is the company identity master.** WIP does not
   create its own company identity — every WIP company row is a mirror of
   a Salesforce Account, keyed on `salesforce_account_id`.
2. **No Salesforce Account → no WIP company.** Operational rule: if a
   company doesn't exist in Salesforce, create it there first; the hourly
   sync brings it into WIP; then WIP data can attach. The WIP app enforces
   this with a foreign-key constraint on `wip_case.wip_company_id`.
3. **Null WIP is valid.** A company that exists (because it's in Salesforce
   and synced) but has no current WIP rows is a legitimate state. ERic
   answers *"firm exists, no current WIP"* — not *"firm not found"*.
4. **Soft-delete only.** Salesforce Accounts marked inactive or deleted
   in SF flip `wip_company.is_active_in_salesforce` to false, but the row
   stays — historical WIP records may still reference it.
5. **canonical_company remains ERic's cross-source bridge.** WIP stores
   `canonical_company_id` on `wip_company` as a foreign-key pointer so
   ERic can join WIP data to its `canonical_company` master. But WIP's
   own primary identity is `salesforce_account_id`.
6. **All mutations audited.** Every WIP mutation writes a
   `wip_audit_log` row recording who/when/what changed. Sync runs also
   log to `wip_import_batch` (and per-row audit if a value materially
   changed).
7. **Read-only views for ERic.** ERic reads versioned `nif_wip.v_eric_*`
   views, never raw `wip_*` tables. View signatures are contracts.

## Database

- **Instance**: shared `localhost:3336` MariaDB (same instance as `nif_elt`)
- **Database name**: `nif_wip` (separate from `nif_elt`)
- **Users**:
  - `wip_app` — full DDL + DML on `nif_wip.*`, plus SELECT on
    `nif_elt.canonical_company` and `nif_elt.entity_alias` so the SF sync
    can resolve `canonical_company_id` at insert time
  - `wip_read` — SELECT only on `nif_wip.v_eric_*` views (granted to ERic
    when WO-12 ships)

## Planned table outline (firmed up by `docs/blueprints/WO-9B-WIP-data-model.md`)

```
wip_company                ← Salesforce Account mirror
wip_case                   ← WIP records, FK to wip_company
wip_case_financial         ← per-case WIP/fees/yield over time
wip_status_history         ← case state transitions
wip_import_batch           ← CSV + SF sync provenance
wip_audit_log              ← who/when/what for every mutation
wip_sync_run               ← per-run SF sync telemetry
```

Plus read-only views consumed by ERic:

```
v_eric_wip_company_summary     ← one row per company with current WIP rollup
v_eric_wip_current_cases       ← currently-open cases with firm + status
v_eric_wip_firm_with_wip       ← firms with non-zero current WIP (for anti-join)
v_eric_wip_audit_recent        ← recent mutations
```

## Integration with ERic

See `docs/ERIC_INTEGRATION_BOUNDARY.md`.

## What WIP does NOT do

- Run LLM calls or talk to Open WebUI
- Maintain ERic's semantic catalogue or business-term registry
- Own `canonical_company` / `canonical_person` / `entity_alias` (those
  remain in `nif_elt`, owned by the ERic ingestion / resolution stack)
- Run the FCO promotion job (that's an ERic-side aggregation of Case
  Manager snapshots, separate from WIP)
- Create company records independently — Salesforce-first only
