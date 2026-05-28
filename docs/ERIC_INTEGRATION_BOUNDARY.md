# ERic ↔ WIP Integration Boundary

This document is the contract between WIP (`/home/WIP`) and ERic
(`/home/ELT`). Both sides are expected to honour it.

## Hard rules

1. **ERic NEVER writes to WIP tables or views.** Any write attempted against
   `nif_wip.*` from the ERic codebase is a bug.
2. **WIP NEVER reads from or writes to ERic's catalogue tables**
   (`canonical_company`, `entity_alias`, `eric_catalogue_*`,
   `eric_unknown_terms`). These remain owned by ERic.
3. **The Excel mastercopy CSV becomes WIP's ingestion source** during the
   transition. ERic stops reading the CSV directly once WO-11 ships.

## Data flow

```
WIP-side writes:
  WIP UI / WIP CSV import → wip_app → nif_wip.* tables → wip_audit_log

ERic-side reads:
  ERic chat proxy → planner → executor → nif_wip.v_* views (read-only)
```

## How ERic gets read access

WIP exposes versioned views. ERic connects with a dedicated DB user that
has SELECT only on those views.

```sql
-- created during WIP setup
CREATE USER 'wip_read'@'%' IDENTIFIED BY '<secret>';
GRANT SELECT ON nif_wip.v_* TO 'wip_read'@'%';
```

ERic env additions (when WO-11 ships):

```
NIF_WIP_DB_HOST=localhost
NIF_WIP_DB_PORT=3336
NIF_WIP_DB_NAME=nif_wip
NIF_WIP_DB_USER=wip_read
NIF_WIP_DB_PASS=<secret>
```

## Views WIP commits to providing

These are the contract — WIP cannot break their signature without a major
version bump. Initial set (subject to WO-9B refinement):

| View | Purpose | Replaces in ERic |
|---|---|---|
| `v_wip_active_cases` | One row per currently-open case with firm, status, estimated yield, current WIP | Part of `case_monthly_snapshot` reliance |
| `v_wip_firms_with_current_wip` | One row per firm currently holding WIP > 0 | Drives `webinar_firms_without_wip` anti-join |
| `v_wip_firm_period_totals` | Per (firm, period) totals: fees invoiced, actual yield, WIP | Drives `top_firms` ranking metrics |
| `v_wip_audit_recent` | Recent mutations for operator visibility | New capability |

Every view exposes `canonical_company_id`. ERic joins by that key.

## What changes for ERic

Initially (post WO-9A): **nothing**. ERic continues to read from
`firm_commercial_observation` derived from `case_monthly_snapshot` (which is
itself derived from the Excel master copy via the existing promotion path).

After WO-11: ERic functions repoint at `nif_wip.v_*` views. The
deterministic anti-join in `webinar_firms_without_wip` and the WIP-metric
rankings in `top_firms` use WIP as the authoritative source.

Deprecation: once ERic is fully on the WIP views, `case_monthly_snapshot`
and `firm_commercial_observation` may be retired as commercial-data spines
(they may still serve case-level analytics from CM directly).

## Cross-repo coordination

- Schema/view changes that affect ERic must be flagged in a
  `BREAKING_VIEW_CHANGE.md` entry in both repos.
- Mutual integration tests live in **WIP** under
  `tests/integration/test_eric_view_contract.py`. WIP owns the contract;
  ERic owns the consumer logic.

## What does NOT cross the boundary

- ERic's semantic catalogue (`eric_catalogue.yaml`, `eric_catalogue_*`
  tables) — WIP does not see it, does not depend on it.
- WIP's UI/API — ERic does not call it; ERic only reads SQL views.
- WIP's audit log writes — ERic does not write audit rows; if ERic ever
  needs to record "I read X at time Y", that's its own concern in `nif_elt`.

## Open boundary questions (to be resolved in WO-9B / WO-10)

- Does WIP store its own copy of firm names, or only the
  `canonical_company_id` resolved at import time?
- How does WIP handle a row whose firm cannot be canonically resolved?
  Reject at import? Park in `wip_pending_firms`? Default to free-text with
  a flag?
- What's the cadence of ERic's view-cache invalidation (if any) versus
  WIP's write rate?
