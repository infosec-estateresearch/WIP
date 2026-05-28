"""WIP Salesforce sync health report (WO-10C).

Reports the operational state of the WIP↔Salesforce sync:

- last successful incremental + full sync timestamps and age
- last failed run (if any)
- counters from the most recent successful run
- duplicate group count (informational — never makes status RED)
- canonical resolution percentage from latest sync
- overall status: GREEN / AMBER / RED

Thresholds (architect spec):
  GREEN   incremental ≤ 2h  AND  full ≤ 36h  AND  no newer-failed-run
  AMBER   incremental > 2h  OR   full > 36h
  RED     incremental > 6h  OR   full > 7d   OR  latest failed run is
          newer than the most recent successful run of any sync type

Usage:
  python -m scripts.sync_health
  python -m scripts.sync_health --json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from src.wip_app.db import wip_connection


# Thresholds (hours / days)
INCREMENTAL_AMBER_AGE_HOURS = 2
INCREMENTAL_RED_AGE_HOURS = 6
FULL_AMBER_AGE_HOURS = 36
FULL_RED_AGE_DAYS = 7


@dataclass
class SyncHealth:
    status: str  # GREEN / AMBER / RED
    reasons: list[str]
    latest_incremental_at: str | None
    latest_incremental_age_hours: float | None
    latest_full_at: str | None
    latest_full_age_hours: float | None
    latest_failed_at: str | None
    latest_failed_sync_type: str | None
    latest_failed_error: str | None
    latest_run_counters: dict[str, int | None]
    duplicate_groups_active: int
    duplicate_companies_flagged: int
    canonical_resolution_pct: float | None
    total_wip_companies: int
    active_wip_companies: int


def collect_health(conn) -> SyncHealth:
    """Read the WIP tables and classify the sync state."""
    now = datetime.now(timezone.utc)

    with conn.cursor() as c:
        # Most recent SUCCESSFUL run per sync_type
        c.execute("""
            SELECT sync_type, MAX(completed_at) AS last_at
            FROM wip_sync_run
            WHERE status = 'completed'
            GROUP BY sync_type
        """)
        last_success = {r["sync_type"]: r["last_at"] for r in c.fetchall()}

        # Most recent FAILED run (any type)
        c.execute("""
            SELECT sync_type, started_at, error_message
            FROM wip_sync_run
            WHERE status = 'failed'
            ORDER BY started_at DESC LIMIT 1
        """)
        failed = c.fetchone()

        # Counters from the most recent SUCCESSFUL run (any type)
        c.execute("""
            SELECT sync_type, sf_accounts_fetched, sf_accounts_inserted,
                   sf_accounts_updated, sf_accounts_unchanged,
                   sf_accounts_soft_deleted,
                   canonical_resolved, canonical_unresolved
            FROM wip_sync_run
            WHERE status = 'completed'
            ORDER BY completed_at DESC LIMIT 1
        """)
        latest = c.fetchone() or {}

        # Duplicate-detection state
        c.execute("""
            SELECT COUNT(*) AS n
            FROM wip_company_duplicate_candidate
            WHERE status IN ('NEW', 'NEEDS_REVIEW')
        """)
        dup_groups = (c.fetchone() or {}).get("n", 0)

        c.execute("""
            SELECT COUNT(*) AS n FROM wip_company
            WHERE is_potential_duplicate = 1
        """)
        dup_companies = (c.fetchone() or {}).get("n", 0)

        c.execute("""
            SELECT COUNT(*) AS total,
                   SUM(is_active_in_salesforce = 1) AS active
            FROM wip_company
        """)
        cnts = c.fetchone() or {}
        total = int(cnts.get("total") or 0)
        active = int(cnts.get("active") or 0)

    def _utc(t):
        if t is None:
            return None
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)

    last_inc = _utc(last_success.get("salesforce_account_incremental"))
    last_full = _utc(last_success.get("salesforce_account_full"))
    last_any_success = max((t for t in (last_inc, last_full) if t), default=None)
    last_failed_at = _utc(failed["started_at"]) if failed else None

    inc_age_h = (now - last_inc).total_seconds() / 3600 if last_inc else None
    full_age_h = (now - last_full).total_seconds() / 3600 if last_full else None

    # Canonical resolution % from latest successful run
    cr = latest.get("canonical_resolved") if latest else None
    cu = latest.get("canonical_unresolved") if latest else None
    denom = (cr or 0) + (cu or 0)
    canon_pct = (cr / denom * 100) if denom else None

    # ── Classification ────────────────────────────────────────────────
    reasons: list[str] = []
    status = "GREEN"

    if last_failed_at and (last_any_success is None or last_failed_at > last_any_success):
        status = "RED"
        reasons.append(
            f"latest run failed at {last_failed_at.isoformat()} "
            f"(no newer successful run)"
        )

    if last_inc is None:
        status = _worsen(status, "RED")
        reasons.append("no successful incremental sync recorded")
    elif inc_age_h is not None and inc_age_h > INCREMENTAL_RED_AGE_HOURS:
        status = _worsen(status, "RED")
        reasons.append(
            f"incremental sync stale: {inc_age_h:.1f}h "
            f"(> {INCREMENTAL_RED_AGE_HOURS}h RED threshold)"
        )
    elif inc_age_h is not None and inc_age_h > INCREMENTAL_AMBER_AGE_HOURS:
        status = _worsen(status, "AMBER")
        reasons.append(
            f"incremental sync warm-stale: {inc_age_h:.1f}h "
            f"(> {INCREMENTAL_AMBER_AGE_HOURS}h AMBER threshold)"
        )

    if last_full is None:
        status = _worsen(status, "RED")
        reasons.append("no successful full reconciliation recorded")
    elif full_age_h is not None and full_age_h > FULL_RED_AGE_DAYS * 24:
        status = _worsen(status, "RED")
        reasons.append(
            f"full reconciliation stale: {full_age_h / 24:.1f}d "
            f"(> {FULL_RED_AGE_DAYS}d RED threshold)"
        )
    elif full_age_h is not None and full_age_h > FULL_AMBER_AGE_HOURS:
        status = _worsen(status, "AMBER")
        reasons.append(
            f"full reconciliation warm-stale: {full_age_h:.1f}h "
            f"(> {FULL_AMBER_AGE_HOURS}h AMBER threshold)"
        )

    if status == "GREEN":
        reasons.append(
            f"incremental {inc_age_h:.2f}h ago, full {full_age_h:.2f}h ago, "
            f"no newer failed run"
        )

    return SyncHealth(
        status=status,
        reasons=reasons,
        latest_incremental_at=last_inc.isoformat() if last_inc else None,
        latest_incremental_age_hours=round(inc_age_h, 2) if inc_age_h is not None else None,
        latest_full_at=last_full.isoformat() if last_full else None,
        latest_full_age_hours=round(full_age_h, 2) if full_age_h is not None else None,
        latest_failed_at=last_failed_at.isoformat() if last_failed_at else None,
        latest_failed_sync_type=failed["sync_type"] if failed else None,
        latest_failed_error=(failed["error_message"][:200] if failed and failed.get("error_message") else None),
        latest_run_counters={
            "sync_type":        latest.get("sync_type") if latest else None,
            "fetched":          latest.get("sf_accounts_fetched") if latest else None,
            "inserted":         latest.get("sf_accounts_inserted") if latest else None,
            "updated":          latest.get("sf_accounts_updated") if latest else None,
            "unchanged":        latest.get("sf_accounts_unchanged") if latest else None,
            "soft_deleted":     latest.get("sf_accounts_soft_deleted") if latest else None,
        },
        duplicate_groups_active=int(dup_groups),
        duplicate_companies_flagged=int(dup_companies),
        canonical_resolution_pct=round(canon_pct, 2) if canon_pct is not None else None,
        total_wip_companies=total,
        active_wip_companies=active,
    )


_ORDER = {"GREEN": 0, "AMBER": 1, "RED": 2}


def _worsen(current: str, candidate: str) -> str:
    """Return whichever status is strictly worse."""
    return candidate if _ORDER[candidate] > _ORDER[current] else current


def _render_text(h: SyncHealth) -> str:
    icon = {"GREEN": "✓", "AMBER": "~", "RED": "✗"}.get(h.status, "?")
    out: list[str] = []
    out.append(f"WIP Salesforce sync health: [{h.status}] {icon}")
    for r in h.reasons:
        out.append(f"  - {r}")
    out.append("")
    out.append("Sync state:")
    out.append(f"  Last incremental success : {h.latest_incremental_at or '(never)'} "
               f"(age {h.latest_incremental_age_hours}h)")
    out.append(f"  Last full success        : {h.latest_full_at or '(never)'} "
               f"(age {h.latest_full_age_hours}h)")
    if h.latest_failed_at:
        out.append(f"  Latest failed run        : {h.latest_failed_at} "
                   f"({h.latest_failed_sync_type}) — {h.latest_failed_error}")
    out.append("")
    out.append("Latest successful run counters:")
    for k, v in h.latest_run_counters.items():
        out.append(f"  {k:14}: {v}")
    out.append("")
    out.append("Data quality (informational — does NOT make status RED):")
    out.append(f"  duplicate groups active        : {h.duplicate_groups_active}")
    out.append(f"  companies flagged as duplicate : {h.duplicate_companies_flagged}")
    out.append(f"  canonical resolution %         : {h.canonical_resolution_pct}")
    out.append(f"  total wip_company rows         : {h.total_wip_companies} ({h.active_wip_companies} active)")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wip.sync_health")
    parser.add_argument("--json", action="store_true", help="Emit JSON to stdout.")
    parser.add_argument("--strict", action="store_true",
                        help="Exit non-zero if status is AMBER (1) or RED (2).")
    args = parser.parse_args(argv)

    conn = wip_connection(autocommit=True)
    try:
        health = collect_health(conn)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(asdict(health), indent=2))
    else:
        print(_render_text(health))

    if args.strict:
        return {"GREEN": 0, "AMBER": 1, "RED": 2}[health.status]
    return 0


if __name__ == "__main__":
    sys.exit(main())
