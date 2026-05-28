# WIP — Estate Research Work-In-Progress SQL Application

WIP is the operational SQL application for Estate Research work-in-progress
data. It replaces the manual `MASTERCOPY-V2.xlsx` master copy as the
authoritative source for case-level commercial state (open cases, status,
fees invoiced, WIP balance, estimated yield, etc.).

## Identity rule — Salesforce is the company master

**Salesforce Account is the single source of truth for company identity.**
WIP does NOT maintain its own company master.

- Companies enter WIP only by being mirrored from Salesforce (hourly sync).
- If a company does not exist in Salesforce, it cannot have WIP data.
  The operational rule is: *create the Salesforce Account first, wait for
  the next hourly sync, then add WIP data against it.*
- A company that exists in Salesforce but has no WIP is a valid, meaningful
  state. ERic answers *"that firm exists but has no current WIP"* rather
  than *"that firm doesn't exist"*.
- A Salesforce Account that becomes inactive or is deleted is **soft-marked
  inactive in WIP** — never hard-deleted, because historical WIP records
  may still reference it.

This means:

```
Salesforce Account  =  company identity master
WIP SQL             =  work-in-progress operational layer (mirrors SF)
ERic                =  read-only intelligence layer (reads WIP views)
```

## Ownership boundary

WIP **owns**:

- The `nif_wip` MariaDB database — all WIP tables, migrations, audit trail
- Hourly Salesforce Account → WIP company mirror sync
- CSV import from the legacy Excel master copy (transition period only;
  the CSV becomes ingestion input, never company identity authority)
- WIP record lifecycle: create, update status, close, financial fields
- Future WIP UI / API
- Any scheduler/timer for WIP-internal jobs

WIP **does not own**:

- ERic itself (chat proxy, retrieval API, semantic catalogue) — lives in
  `/home/ELT` as a separate codebase and repo
- LLM orchestration, planner, synthesiser
- ERic's function catalogue or business-term catalogue
- `canonical_company` — that stays in `nif_elt` as ERic's cross-source
  identity bridge. WIP stores `canonical_company_id` as a foreign key
  pointer for ERic's join convenience, but Salesforce AccountId is WIP's
  primary identity.

## Integration boundary with ERic

ERic is a **read-only consumer** of approved WIP data. WIP exposes SQL views
(or, later, a small API) that ERic queries. ERic must NEVER write to WIP
tables.

```
Salesforce Account (master)
  ↓ hourly sync
WIP company mirror + WIP cases / financials / status
  ↓ exposed as nif_wip.v_eric_* read-only views
ERic queries those views (matching on canonical_company_id where needed)
```

See `docs/ERIC_INTEGRATION_BOUNDARY.md` for the contract and
`docs/blueprints/WO-9B-WIP-data-model.md` for the table-level blueprint.

## Status

| Work package | Status |
|---|---|
| WO-9A — repo bootstrap | done (cb4447b) |
| WO-9B — Salesforce-aligned data model blueprint | done (996b204) |
| WO-10A — `nif_wip` schema + duplicate-detection tables | **this commit** |
| WO-10B — Salesforce Account hourly sync implementation | next |
| WO-10C — Sync timer + health check | next |
| WO-11 — CSV WIP import into wip_case tables | not started |
| WO-12 — ERic WIP query migration (point at WIP views) | not started |
| WO-13 — WIP UI/API (incl. duplicate review screen) | deferred |

## Repo layout

```
configs/        WIP configuration (database, sync, env wiring)
src/wip_app/    application code
migrations/     numbered SQL migrations for the nif_wip database
scripts/        operator scripts (sync, import, audit, repair)
tests/          pytest tests
infra/systemd/  WIP-owned systemd services/timers
ops/evidence/   per-WO operational evidence (run reports, before/after)
docs/           architecture + integration boundary
docs/blueprints/  per-WO design documents (this is WO-9B)
```

## Running

WIP uses the **existing MariaDB instance** that ERic already runs against
(currently `localhost:3336`), but a **separate database** named `nif_wip`.
Connection details follow the same env-var convention as ERic — see
`/home/WIP/configs/wip.json.example`.

Setup (Python 3.9+):

```bash
cd /home/WIP
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### One-time database bootstrap (operator, MariaDB root)

Creates `nif_wip` and the `wip_app` / `wip_read` users with the right
grants. Already executed during WO-10A; documented here for re-runs:

```sql
CREATE DATABASE IF NOT EXISTS nif_wip
  DEFAULT CHARACTER SET utf8mb4 DEFAULT COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'wip_app'@'%'  IDENTIFIED BY '<generated>';
CREATE USER IF NOT EXISTS 'wip_read'@'%' IDENTIFIED BY '<generated>';
GRANT ALL PRIVILEGES ON nif_wip.* TO 'wip_app'@'%';
GRANT SELECT ON nif_elt.canonical_company         TO 'wip_app'@'%';
GRANT SELECT ON nif_elt.entity_alias              TO 'wip_app'@'%';
GRANT SELECT ON nif_elt.sf_account_company_bridge TO 'wip_app'@'%';
FLUSH PRIVILEGES;
```

Credentials written to `/home/WIP/.env` (gitignored, mode 600). The
`wip_read` user only gets SELECT on the `v_eric_*` views — those grants
land per-view during WO-12.

### Migration workflow

```bash
# show every migration file + applied / pending / drift status
python -m scripts.run_migration --status

# preview pending migrations without applying
python -m scripts.run_migration --dry-run

# apply pending migrations (idempotent, tracked via schema_migration)
python -m scripts.run_migration --apply

# apply just one file
python -m scripts.run_migration --apply --only 002_wip_sync_run.sql
```

The runner records each applied file's SHA-256 checksum in the
`schema_migration` table and refuses to re-apply if a previously-applied
file has been edited (drift detection). Always create a NEW numbered
migration file rather than editing an applied one.

## Branch / worktree rules

- Default working branch: `main`
- Feature work: `wo/WO-<NN>-short-slug` branches
- No force-push to `main`
- Dev worktrees (if needed later): `/home/WIP-dev`
- WIP and ERic repos are independent — do NOT use a single worktree spanning both
