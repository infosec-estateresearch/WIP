# ERic ↔ WIP Integration Boundary

This document is the contract between WIP (`/home/WIP`) and ERic
(`/home/ELT`). Both sides are expected to honour it.

## Hard rules

1. **ERic NEVER writes to WIP tables or views.** Any write attempted against
   `nif_wip.*` from the ERic codebase is a bug.
2. **WIP NEVER reads from or writes to ERic's catalogue tables**
   (`eric_catalogue_*`, `eric_unknown_terms`). WIP MAY read
   `canonical_company` and `entity_alias` from `nif_elt` *read-only*
   at sync time to resolve `canonical_company_id` for WIP company rows —
   never write back.
3. **Salesforce Account is the only entry point for WIP company identity.**
   No CSV row, ERic process, manual UI, or API call may insert a
   `wip_company` row. The hourly SF sync is the sole creator.
4. **The Excel mastercopy CSV becomes WIP's case ingestion source** during
   transition (WO-11). ERic stops reading the CSV-derived FCO for
   WIP-flavoured answers once WO-12 ships.

## Data flow (post WO-10..WO-12)

```
Salesforce Account (master for COMPANY IDENTITY)
  ↓ hourly sync (owned by WIP)
WIP-side writes:
  wip_company  ← every company that exists in SF (active or soft-deleted)
  wip_case + wip_case_financial ← attached only via wip_company_id FK
  wip_audit_log ← every mutation
  wip_sync_run / wip_import_batch ← provenance + telemetry

ERic-side reads (read-only views, never tables):
  nif_wip.v_eric_wip_company_summary
  nif_wip.v_eric_wip_current_cases
  nif_wip.v_eric_wip_firm_with_wip
  nif_wip.v_eric_wip_audit_recent

Join key for ERic: canonical_company_id (WIP stores it for this purpose)
```

## How ERic gets read access

WIP creates a dedicated DB user with SELECT only on the v_eric_* views:

```sql
-- WIP-owned grant (created during WO-10 setup)
CREATE USER 'wip_read'@'%' IDENTIFIED BY '<secret>';
GRANT SELECT ON nif_wip.v_eric_wip_company_summary  TO 'wip_read'@'%';
GRANT SELECT ON nif_wip.v_eric_wip_current_cases    TO 'wip_read'@'%';
GRANT SELECT ON nif_wip.v_eric_wip_firm_with_wip    TO 'wip_read'@'%';
GRANT SELECT ON nif_wip.v_eric_wip_audit_recent     TO 'wip_read'@'%';
-- Explicitly NO grants on wip_company, wip_case, wip_*.
```

ERic env additions (when WO-12 ships):

```
NIF_WIP_DB_HOST=localhost
NIF_WIP_DB_PORT=3336
NIF_WIP_DB_NAME=nif_wip
NIF_WIP_DB_USER=wip_read
NIF_WIP_DB_PASS=<secret>
```

## Views WIP commits to providing

Signatures are the contract. WIP cannot break them without a major version
bump and a `BREAKING_VIEW_CHANGE.md` entry in both repos.

| View | Purpose | Replaces in ERic |
|---|---|---|
| `v_eric_wip_company_summary` | One row per SF-mirrored company with rollup of current WIP value, case count, last WIP date, `has_current_wip` flag | The firm-level slice currently derived from `firm_commercial_observation` |
| `v_eric_wip_current_cases` | One row per currently-open case with firm, status, current WIP/fees/yield | Replaces case-level reads against `case_monthly_snapshot` for WIP-flavoured answers |
| `v_eric_wip_firm_with_wip` | Firms currently holding non-zero WIP | Drives `webinar_firms_without_wip` anti-join (currently SQL against `firm_commercial_observation`) |
| `v_eric_wip_audit_recent` | Recent mutations (7-day window) for operator visibility | New capability |

Every view exposes `canonical_company_id`. ERic joins by that key when
answering cross-source questions.

Every view also exposes `salesforce_account_id` as the authoritative
WIP-side identity — usable for any future "go to the Salesforce record"
deep-link feature in the chat UI.

## What changes for ERic (timeline)

Initially (post WO-9B, the current state): **nothing**. ERic continues to
read from `firm_commercial_observation` derived from `case_monthly_snapshot`
(which is itself derived from the Excel master copy via the existing
promotion path).

After WO-10: WIP sync running hourly, `wip_company` populated, but no
`wip_case` data yet. ERic still on FCO.

After WO-11: WIP fully populated (CSV-imported), v_eric_* views serving
real data. ERic still on FCO until WO-12 cuts over.

After WO-12: ERic functions repoint at `nif_wip.v_eric_*` views. The
deterministic anti-join in `webinar_firms_without_wip` and the
WIP-metric rankings in `top_firms` use WIP as the authoritative source.

Eventual deprecation: once ERic is fully on the WIP views,
`firm_commercial_observation` may be retired as the commercial-data spine
(it may still serve case-level analytics derived from Case Manager
directly — but not WIP).

## "Firm exists, no current WIP" semantics

A key product win from the SF-master design: ERic can now distinguish

- *"firm doesn't exist anywhere we know about"* (no `wip_company` row,
  no `canonical_company` row)
- *"firm exists in WIP (= in Salesforce) but has no current WIP"*
  (`wip_company` row present, `has_current_wip = false`)
- *"firm exists in WIP and has current WIP"* (`has_current_wip = true`)

These map to clearly different user-facing answers:

| Question | Today's answer (FCO) | WIP-views answer |
|---|---|---|
| "What's our WIP with X?" if X has no FCO row | "I don't have data for X" | "X exists in Salesforce/WIP but has no current WIP" |
| Same if X has no canonical | Confused — anti-join under-excludes | Clear — `v_eric_wip_company_summary` returns a row with zeros |

## Cross-repo coordination

- Schema/view changes that affect ERic must be flagged in a
  `BREAKING_VIEW_CHANGE.md` entry in both repos.
- Mutual integration tests live in **WIP** under
  `tests/integration/test_eric_view_contract.py` (WO-12). WIP owns the
  contract; ERic owns the consumer logic.

## What does NOT cross the boundary

- ERic's semantic catalogue (`eric_catalogue.yaml`, `eric_catalogue_*`
  tables) — WIP does not see it.
- WIP's UI/API — ERic does not call it; ERic only reads SQL views.
- WIP's audit log writes — ERic does not write audit rows; if ERic ever
  needs to record "I read X at time Y", that's its own concern in `nif_elt`.
- WIP company creation — must come from Salesforce; ERic does not get to
  ask WIP to invent a company.

## Open boundary questions (to be resolved in WO-10 / WO-11)

- When SF sync resolves `canonical_company_id`, should it WRITE to
  `nif_elt.canonical_company` to register newly-seen SF accounts, or only
  READ existing mappings and leave unresolved as NULL? (Recommendation:
  read-only — let ERic's existing canonical resolution own writes there.)
- How does WIP handle a CSV row whose `case_reference` belongs to a
  company that doesn't yet exist in SF? Reject the row to a
  `wip_import_rejects` table for operator action? Park in a pending
  state? Block the whole batch?
- View-caching: should `v_eric_wip_company_summary` be a materialised view
  refreshed on a timer, or a real-time view? (Real-time is simpler; if
  ERic's chat latency suffers, materialise later.)
