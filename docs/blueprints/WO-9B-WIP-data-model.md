# WO-9B — WIP SQL Data Model + Salesforce Alignment Blueprint

**Status:** blueprint only — no migrations, no DB creation, no code in this
work package. WO-10 implements the SF sync; WO-11 imports the CSV WIP rows;
WO-12 repoints ERic at the resulting views.

**Design spine** (verbatim per the architect):
> *WIP must not create its own independent company master. WIP company
> records are Salesforce Account mirrors. A company must exist in Salesforce
> before WIP data can be added. Salesforce Account sync runs hourly and
> creates/updates WIP company records. Companies with no WIP still exist in
> WIP with null/zero WIP values. ERic reads WIP via SQL views and does not
> write to WIP.*

---

## 1. Architectural decisions locked in this blueprint

| Decision | Reason |
|---|---|
| Salesforce Account is the **only** path into `wip_company` | Single source of truth for company identity; eliminates duplicate/typo company drift |
| Hourly SF → WIP sync | Tight enough for sales workflow, loose enough not to hammer SF API |
| Soft-delete only on WIP companies | Historical WIP rows still need their `wip_company_id` to be valid |
| `salesforce_account_id` is `wip_company`'s natural key | Primary identity; UNIQUE; never NULL |
| `canonical_company_id` is a non-authoritative pointer | Convenience for ERic's cross-source joins; nullable until canonical resolution catches up |
| Null/zero WIP for an active company is a valid state | Not the same as "company not found" — ERic answers correctly |
| ERic reads through `v_eric_*` views only | View signatures = contract; breaking change requires version bump |
| All mutations audited via `wip_audit_log` | Operator visibility, compliance, debug |
| Per-run sync telemetry in `wip_sync_run` | Operational health, retry, drift detection |

---

## 2. Tables — full DDL contract

These are the schemas WO-10/WO-11 will migrate to. Reviewed as a contract,
not as final SQL — column lengths and indexes can be tuned in WO-10.

### 2.1 `wip_company` — mirror of Salesforce Account

```sql
CREATE TABLE wip_company (
    id                          BIGINT AUTO_INCREMENT PRIMARY KEY,

    -- Salesforce identity (PRIMARY KEY for sync; never NULL)
    salesforce_account_id       VARCHAR(18)  NOT NULL,
    salesforce_account_name     VARCHAR(255) NOT NULL,

    -- Salesforce-mirrored fields (synced hourly, may change)
    account_type                VARCHAR(64)  NULL,        -- e.g. "Tier 1", "Tier 2"
    industry                    VARCHAR(128) NULL,        -- e.g. "Solicitor", "Council"
    owner_salesforce_id         VARCHAR(18)  NULL,        -- SF user id
    owner_name                  VARCHAR(255) NULL,        -- denormalised for fast display
    billing_street              VARCHAR(255) NULL,
    billing_city                VARCHAR(128) NULL,
    billing_postcode            VARCHAR(32)  NULL,
    billing_country             VARCHAR(64)  NULL,
    tlp_flag                    TINYINT(1)   NULL,        -- Account.TLP__c — training-platform participation
    -- Add more SF custom fields here as needed; keep mirror-only.

    -- ERic cross-source bridge — populated by sync if a canonical_company
    -- mapping exists in nif_elt; else NULL until resolution catches up.
    canonical_company_id        BIGINT       NULL,

    -- Lifecycle state (soft-delete only — NEVER hard-delete)
    is_active_in_salesforce     TINYINT(1)   NOT NULL DEFAULT 1,
    salesforce_deleted_at       DATETIME(6)  NULL,
    last_seen_in_salesforce_at  DATETIME(6)  NOT NULL,
    last_salesforce_sync_at     DATETIME(6)  NOT NULL,

    created_at                  DATETIME(6)  NOT NULL,
    updated_at                  DATETIME(6)  NOT NULL,

    UNIQUE KEY uq_wip_company_sf_id (salesforce_account_id),
    KEY idx_wip_company_canonical (canonical_company_id),
    KEY idx_wip_company_active (is_active_in_salesforce, last_seen_in_salesforce_at),
    KEY idx_wip_company_name (salesforce_account_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Salesforce Account mirror — single source of WIP company identity';
```

**Constraints encoded in app logic (not DB-enforced):**

- `salesforce_account_id` is only set by the SF sync — never by a CSV
  import or operator UI directly.
- `canonical_company_id` is resolved at sync time using ERic's existing
  paths (canonical_name match, entity_alias match, sf_account_company_bridge).
  May remain NULL — that's a `needs_review` signal but not a blocker.

### 2.2 `wip_case` — WIP-specific operational records

```sql
CREATE TABLE wip_case (
    id                  BIGINT AUTO_INCREMENT PRIMARY KEY,

    -- FK to the company mirror — REQUIRED, no orphan WIP allowed.
    wip_company_id      BIGINT NOT NULL,

    -- Denormalised foreign keys for fast ERic joins (kept in sync via trigger
    -- or app logic; canonical bridge to Salesforce and ERic).
    salesforce_account_id VARCHAR(18) NOT NULL,
    canonical_company_id  BIGINT NULL,

    -- WIP record identity
    case_reference      VARCHAR(64)  NULL,    -- e.g. case number from CSV
    case_name           VARCHAR(255) NOT NULL, -- e.g. deceased person name
    case_type           VARCHAR(64)  NULL,
    source              ENUM('csv_import', 'wip_ui', 'wip_api', 'manual')
                        NOT NULL DEFAULT 'csv_import',

    -- Lifecycle
    status              VARCHAR(64) NOT NULL,  -- open, in_progress, closed, etc.
    opened_at           DATE         NULL,
    closed_at           DATE         NULL,

    created_at          DATETIME(6) NOT NULL,
    updated_at          DATETIME(6) NOT NULL,

    CONSTRAINT fk_wip_case_company
        FOREIGN KEY (wip_company_id) REFERENCES wip_company(id),

    KEY idx_wip_case_sf_account (salesforce_account_id),
    KEY idx_wip_case_canonical (canonical_company_id),
    KEY idx_wip_case_status (status, closed_at),
    KEY idx_wip_case_reference (case_reference)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='WIP records — must FK to a wip_company (which is a SF mirror)';
```

### 2.3 `wip_case_financial` — per-period WIP/fees/yield

```sql
CREATE TABLE wip_case_financial (
    id                  BIGINT AUTO_INCREMENT PRIMARY KEY,
    wip_case_id         BIGINT NOT NULL,

    as_of_date          DATE   NOT NULL,
    period_year         SMALLINT NOT NULL,
    period_month        TINYINT  NOT NULL,

    -- Monetary values (GBP throughout; currency column reserved for future)
    wip_value           DECIMAL(15,2) NULL,
    fees_invoiced       DECIMAL(15,2) NULL,
    actual_yield        DECIMAL(15,2) NULL,
    estimated_yield     DECIMAL(15,2) NULL,
    currency            CHAR(3)       NOT NULL DEFAULT 'GBP',

    -- Provenance
    source              ENUM('csv_import', 'wip_ui', 'wip_api', 'manual')
                        NOT NULL DEFAULT 'csv_import',
    import_batch_id     BIGINT NULL,

    created_at          DATETIME(6) NOT NULL,
    updated_at          DATETIME(6) NOT NULL,

    CONSTRAINT fk_wip_case_financial_case
        FOREIGN KEY (wip_case_id) REFERENCES wip_case(id),
    CONSTRAINT fk_wip_case_financial_batch
        FOREIGN KEY (import_batch_id) REFERENCES wip_import_batch(id),

    UNIQUE KEY uq_wip_case_financial_case_period (wip_case_id, period_year, period_month),
    KEY idx_wip_case_financial_as_of (as_of_date),
    KEY idx_wip_case_financial_period (period_year, period_month)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='Per-period WIP/fees/yield for each wip_case';
```

### 2.4 `wip_status_history` — case state transitions

```sql
CREATE TABLE wip_status_history (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    wip_case_id     BIGINT       NOT NULL,
    from_status     VARCHAR(64)  NULL,        -- NULL on initial set
    to_status       VARCHAR(64)  NOT NULL,
    actor           VARCHAR(128) NULL,        -- operator / system identifier
    reason          VARCHAR(255) NULL,
    changed_at      DATETIME(6)  NOT NULL,

    CONSTRAINT fk_wip_status_history_case
        FOREIGN KEY (wip_case_id) REFERENCES wip_case(id),

    KEY idx_wip_status_history_case_time (wip_case_id, changed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

### 2.5 `wip_import_batch` — CSV / SF sync provenance

```sql
CREATE TABLE wip_import_batch (
    id                  BIGINT AUTO_INCREMENT PRIMARY KEY,
    source_type         ENUM('csv_mastercopy', 'salesforce_account_sync',
                             'manual', 'wip_ui', 'wip_api') NOT NULL,
    source_file         VARCHAR(512) NULL,
    source_hash         VARCHAR(64)  NULL,         -- SHA-256 fingerprint of input
    started_at          DATETIME(6)  NOT NULL,
    completed_at        DATETIME(6)  NULL,
    rows_seen           INT          NOT NULL DEFAULT 0,
    rows_inserted       INT          NOT NULL DEFAULT 0,
    rows_updated        INT          NOT NULL DEFAULT 0,
    rows_rejected       INT          NOT NULL DEFAULT 0,
    status              ENUM('running', 'completed', 'failed', 'partial') NOT NULL,
    error_message       TEXT         NULL,

    KEY idx_wip_import_batch_type_time (source_type, started_at),
    KEY idx_wip_import_batch_hash (source_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

### 2.6 `wip_audit_log` — who/when/what for every mutation

```sql
CREATE TABLE wip_audit_log (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    entity_type     ENUM('wip_company', 'wip_case', 'wip_case_financial',
                         'wip_status_history') NOT NULL,
    entity_id       BIGINT       NOT NULL,
    action          ENUM('insert', 'update', 'soft_delete', 'reactivate') NOT NULL,
    before_json     JSON         NULL,
    after_json      JSON         NOT NULL,
    actor           VARCHAR(128) NOT NULL,  -- 'sf_sync', 'csv_import', operator id
    import_batch_id BIGINT       NULL,
    changed_at      DATETIME(6)  NOT NULL,

    KEY idx_wip_audit_entity (entity_type, entity_id, changed_at),
    KEY idx_wip_audit_actor_time (actor, changed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='Append-only audit log — never UPDATE or DELETE rows here';
```

### 2.7 `wip_sync_run` — per-run Salesforce sync telemetry

```sql
CREATE TABLE wip_sync_run (
    id                      BIGINT AUTO_INCREMENT PRIMARY KEY,
    sync_type               ENUM('salesforce_account') NOT NULL,
    started_at              DATETIME(6) NOT NULL,
    completed_at            DATETIME(6) NULL,
    status                  ENUM('running', 'completed', 'failed', 'partial') NOT NULL,
    sf_accounts_fetched     INT NOT NULL DEFAULT 0,
    sf_accounts_inserted    INT NOT NULL DEFAULT 0,
    sf_accounts_updated     INT NOT NULL DEFAULT 0,
    sf_accounts_unchanged   INT NOT NULL DEFAULT 0,
    sf_accounts_soft_deleted INT NOT NULL DEFAULT 0,
    canonical_resolved      INT NOT NULL DEFAULT 0,
    canonical_unresolved    INT NOT NULL DEFAULT 0,
    error_message           TEXT NULL,

    KEY idx_wip_sync_run_type_time (sync_type, started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

---

## 3. Read-only views for ERic

ERic gets `SELECT` only on these. Signatures are the contract; breaking them
requires a major version bump and notice to the ERic side.

### `v_eric_wip_company_summary` — one row per company with current WIP rollup

```sql
CREATE OR REPLACE VIEW v_eric_wip_company_summary AS
SELECT
    c.id                              AS wip_company_id,
    c.salesforce_account_id,
    c.canonical_company_id,
    c.salesforce_account_name         AS company_name,
    c.account_type,
    c.industry,
    c.is_active_in_salesforce,
    COALESCE(curr.current_wip_value, 0)   AS current_wip_value,
    COALESCE(curr.current_wip_count, 0)   AS current_wip_count,
    curr.latest_wip_date,
    (COALESCE(curr.current_wip_value, 0) > 0)
                                      AS has_current_wip
FROM wip_company c
LEFT JOIN (
    SELECT
        cs.wip_company_id,
        SUM(f.wip_value)        AS current_wip_value,
        COUNT(DISTINCT cs.id)   AS current_wip_count,
        MAX(f.as_of_date)       AS latest_wip_date
    FROM wip_case cs
    JOIN wip_case_financial f ON f.wip_case_id = cs.id
    WHERE cs.status NOT IN ('closed', 'rejected', 'archived')
    GROUP BY cs.wip_company_id
) curr ON curr.wip_company_id = c.id;
```

This is the workhorse view for ERic's WIP questions. Note `has_current_wip`
is computed — ERic uses it for the anti-join that replaces today's
`webinar_firms_without_wip` SQL.

### `v_eric_wip_current_cases` — currently-open cases with firm + status

```sql
CREATE OR REPLACE VIEW v_eric_wip_current_cases AS
SELECT
    cs.id                       AS wip_case_id,
    cs.salesforce_account_id,
    cs.canonical_company_id,
    c.salesforce_account_name   AS company_name,
    cs.case_reference,
    cs.case_name,
    cs.case_type,
    cs.status,
    cs.opened_at,
    f.wip_value                 AS current_wip_value,
    f.fees_invoiced             AS current_fees_invoiced,
    f.actual_yield              AS current_actual_yield,
    f.estimated_yield           AS current_estimated_yield,
    f.as_of_date                AS financial_as_of
FROM wip_case cs
JOIN wip_company c ON c.id = cs.wip_company_id
LEFT JOIN wip_case_financial f
    ON f.wip_case_id = cs.id
    AND f.as_of_date = (
        SELECT MAX(as_of_date)
        FROM wip_case_financial f2
        WHERE f2.wip_case_id = cs.id
    )
WHERE cs.status NOT IN ('closed', 'rejected', 'archived');
```

### `v_eric_wip_firm_with_wip` — for ERic's anti-join

```sql
CREATE OR REPLACE VIEW v_eric_wip_firm_with_wip AS
SELECT
    c.id                        AS wip_company_id,
    c.salesforce_account_id,
    c.canonical_company_id,
    c.salesforce_account_name   AS company_name,
    SUM(f.wip_value)            AS total_current_wip,
    COUNT(DISTINCT cs.id)       AS active_case_count,
    MAX(f.as_of_date)           AS latest_wip_date
FROM wip_company c
JOIN wip_case cs ON cs.wip_company_id = c.id
JOIN wip_case_financial f ON f.wip_case_id = cs.id
WHERE cs.status NOT IN ('closed', 'rejected', 'archived')
  AND f.wip_value > 0
GROUP BY c.id, c.salesforce_account_id, c.canonical_company_id, c.salesforce_account_name
HAVING SUM(f.wip_value) > 0;
```

ERic's WO-12 changes `webinar_firms_without_wip` to:

```sql
-- pseudo-SQL on ERic side
SELECT firm.* FROM webinar_firms_with_attendees firm
WHERE firm.canonical_company_id NOT IN (
    SELECT canonical_company_id FROM nif_wip.v_eric_wip_firm_with_wip
    WHERE canonical_company_id IS NOT NULL
);
```

### `v_eric_wip_audit_recent` — operator visibility into recent mutations

```sql
CREATE OR REPLACE VIEW v_eric_wip_audit_recent AS
SELECT
    a.id, a.entity_type, a.entity_id, a.action, a.actor,
    a.changed_at, a.import_batch_id
FROM wip_audit_log a
WHERE a.changed_at >= NOW() - INTERVAL 7 DAY
ORDER BY a.changed_at DESC;
```

(Future ERic capability: surface "WIP changes in the last X days for firm Y"
without exposing raw audit JSON.)

---

## 4. Salesforce sync — operational contract

### Cadence
- Hourly via systemd timer in `/home/WIP/infra/systemd/`
- Idempotent: re-running same hour produces no changes (compares fields,
  only updates when materially different)

### Steps per run
1. Insert `wip_sync_run` row with `status='running'`
2. Query Salesforce: `SELECT Id, Name, Type, Industry, OwnerId,
   Owner.Name, BillingStreet, BillingCity, BillingPostalCode,
   BillingCountry, TLP__c, IsDeleted, LastModifiedDate FROM Account`
   (consider incremental: WHERE LastModifiedDate > last_successful_sync)
3. For each SF Account:
   - If absent from `wip_company`: INSERT new row, resolve
     `canonical_company_id` via `nif_elt.canonical_company` + `entity_alias`
     + `sf_account_company_bridge`
   - If present and unchanged: bump `last_seen_in_salesforce_at`,
     `last_salesforce_sync_at`
   - If present and changed: UPDATE the mirrored fields, write
     `wip_audit_log` row with before/after JSON
   - If marked `IsDeleted=true` in SF: set
     `is_active_in_salesforce=false`, `salesforce_deleted_at=NOW()`
4. For any `wip_company` row not seen in this run AND
   `is_active_in_salesforce=true`: flag as candidate for soft-delete
   (do not auto-delete on a single missed run — require N consecutive
   misses to avoid false soft-deletes from incremental query gaps)
5. UPDATE `wip_sync_run` to `'completed'` with counters

### Failure handling
- Sync wraps the whole batch in a transaction at the WIP database level
- If SF API call fails partway: rollback WIP changes, mark
  `wip_sync_run` as `'failed'` with `error_message`
- If individual row resolution fails: skip the row, log, continue —
  partial sync runs are explicitly allowed (`status='partial'`)

### Single-run lock
- Same `fcntl.flock` pattern as ERic's `run_aggregation.py` — concurrent
  invocations exit non-zero

---

## 5. WIP entry validation rules

These are enforced by the WIP app (CSV import, future UI, future API),
NOT just by foreign keys:

| Rule | Enforcement |
|---|---|
| WIP case must reference an existing `wip_company.id` | DB FK |
| `wip_company` cannot be created except by SF sync | App logic; no INSERT path exposed from CSV import or UI |
| Operator attempting to add WIP for an unrecognised firm gets a clear message | App-level error: *"This company must be created in Salesforce first. Once Salesforce sync runs, WIP can be added."* |
| `wip_case.salesforce_account_id` must match `wip_company.salesforce_account_id` | App logic (DB trigger could enforce too) |
| `wip_case_financial.wip_value` ≥ 0 (no negative WIP) | App validation |
| Soft-deleted SF accounts can still hold historical WIP but cannot accept new WIP rows | App validation on insert |

---

## 6. Migration path from current state

| Step | Action | Result |
|---|---|---|
| WO-10 (next) | Create `nif_wip` database, run migrations, implement SF sync | `wip_company` populated from Salesforce; empty `wip_case*` |
| WO-11 | CSV import job reads MASTERCOPY-V2.xlsx, maps each row to a `wip_company.salesforce_account_id` (via canonical_company bridge), inserts `wip_case` + `wip_case_financial` | WIP fully populated; CSV becomes one-way input only |
| WO-12 | ERic functions (`webinar_firms_without_wip`, `top_firms(metric=total_wip)`) repointed at `nif_wip.v_eric_*` views | ERic stops reading `firm_commercial_observation` for WIP-derived answers |
| WO-13 (later) | WIP UI/API ships — operators add WIP via the app, not via CSV | CSV ingest path retired |

Each step has its own dry-run-then-apply discipline. CSV ingest is NOT
turned off until WIP UI is proven.

---

## 7. What this blueprint deliberately does NOT include

Per the architect's "blueprint only, no migrations yet" scope:

- ❌ Actual SQL migration files (those land in WO-10)
- ❌ Database creation (`CREATE DATABASE nif_wip`) — WO-10
- ❌ Salesforce sync implementation — WO-10
- ❌ CSV import job — WO-11
- ❌ ERic-side query changes — WO-12
- ❌ Any code in `src/wip_app/` beyond the existing `__init__.py` skeleton

If any of those start creeping in before being separately authorised, stop.

---

## 8. Open questions to resolve in WO-10

These don't block the blueprint but need answers before the SF sync ships:

1. **Incremental vs full-resync.** Hourly should probably be incremental
   (`LastModifiedDate > last_sync`), but a weekly full resync would
   catch any missed soft-deletes or schema drift. Confirm cadence.
2. **TLP__c and other Estate Research custom fields** — confirm the
   exact custom field list to mirror. The blueprint includes `tlp_flag`
   as the example; WO-10 should enumerate.
3. **Salesforce API auth** — reuse ERic's existing
   `NIF_SRC_SALESFORCE_*` credentials with read-only scope, or provision
   a separate WIP-app integration user?
4. **canonical_company resolution at sync time** — same
   `_build_canonical_lookup` pattern as `firm_observation.py`? Cross-DB
   join from `nif_wip` to `nif_elt`? Or pre-fetch into memory each sync?
5. **`wip_audit_log.before_json/after_json`** — store full row, or only
   the changed fields? Storage vs forensic detail trade-off.

These are WO-10 design-time decisions, captured here so they're not
forgotten.
