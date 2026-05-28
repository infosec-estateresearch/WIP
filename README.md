# WIP — Estate Research Work-In-Progress SQL Application

WIP is the operational SQL application for Estate Research work-in-progress
data. It replaces the manual `MASTERCOPY-V2.xlsx` master copy as the
authoritative source for case-level commercial state (open cases, status,
fees invoiced, WIP balance, estimated yield, etc.).

## Ownership boundary

WIP **owns**:

- The `nif_wip` MariaDB database — all WIP tables, migrations, audit trail
- CSV import from the legacy Excel master copy (transition period only)
- WIP record lifecycle: create, update status, close, financial fields
- Salesforce alignment: WIP records carry the canonical `Salesforce AccountId`
  and resolved `canonical_company_id` where available
- Future WIP UI / API
- Any scheduler/timer for WIP-internal jobs

WIP **does not own**:

- ERic itself (chat proxy, retrieval API, semantic catalogue) — lives in
  `/home/ELT` as a separate codebase and repo
- LLM orchestration, planner, synthesiser
- Function catalogue or business-term catalogue

## Integration boundary with ERic

ERic is a **read-only consumer** of approved WIP data. WIP exposes SQL views
(or, later, a small API) that ERic queries. ERic must NEVER write to WIP
tables.

Preferred MVP integration shape:

```
WIP owns writes + imports
  ↓
WIP exposes SQL views in nif_wip
  ↓
ERic granted SELECT on those views via a dedicated read-only DB user
  ↓
ERic functions (e.g. webinar_firms_without_wip, top_firms by total_wip)
  switch to reading from the new views during WO-11
```

See `docs/ERIC_INTEGRATION_BOUNDARY.md` for the full contract.

## Status

| Work package | Status |
|---|---|
| WO-9A — repo bootstrap | this commit |
| WO-9B — data model + Salesforce alignment design | not started |
| WO-10 — CSV import MVP with lineage | not started |
| WO-11 — ERic WIP query migration | not started |
| WO-12 — WIP UI/API | deferred |

## Repo layout

```
configs/        WIP configuration (database, import paths, env wiring)
src/wip_app/    application code
migrations/     numbered SQL migrations for the nif_wip database
scripts/        operator scripts (import, audit, repair)
tests/          pytest tests
infra/systemd/  WIP-owned systemd services/timers (do not put in /home/ELT/infra)
ops/evidence/   per-WO operational evidence (run reports, before/after states)
docs/           architecture + integration boundary documents
```

## Running

Set up Python environment (Python 3.9+):

```bash
cd /home/WIP
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

WIP uses the **existing MariaDB instance** that ERic already runs against
(currently `localhost:3336`), but a **separate database** named `nif_wip`.
Connection details are managed via the same env-var convention as ERic — see
`/home/WIP/configs/wip.json` for the resolution path.

## Branch / worktree rules

- Default working branch: `main`
- Feature work: `wo/WO-<NN>-short-slug` branches
- No force-push to `main`
- Dev worktrees (if needed later): `/home/WIP-dev`
- WIP and ERic repos are independent — do NOT use a single worktree spanning both
