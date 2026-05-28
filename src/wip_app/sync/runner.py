"""Salesforce → wip_company sync runner.

Orchestrates one sync run end-to-end:

  1. Insert wip_sync_run row (status='running'); record the watermark used.
  2. Fetch SF Accounts (full or incremental).
  3. Resolve canonical_company_id for each (in-memory lookup).
  4. For each fetched account:
       insert / update changed / unchanged-touch / reactivate
  5. Full mode only: soft-delete any wip_company not seen.
  6. Run duplicate detection.
  7. Mark sync_run completed (or failed) with telemetry.

Architect rules enforced here:
- Salesforce Account is identity master; UNIQUE(salesforce_account_id) FK
  is enforced at the DB level (WO-10A migration 001).
- Soft-delete only (sets is_active_in_salesforce=0, never DROP/DELETE).
- Audit logs only material changes (audit.diff_fields() decides).
- Dry-run path runs every step except DB writes and SF queries (well —
  SF still runs; we just don't mutate wip_*).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Literal

import pymysql

from src.wip_app.audit import diff_fields, write_audit
from src.wip_app.normaliser import duplicate_group_key, normalise_company_name
from src.wip_app.sync.canonical_resolver import CanonicalResolver
from src.wip_app.sync.duplicate_detector import (
    DuplicateDetectionResult,
    detect_and_upsert_duplicates,
)
from src.wip_app.sync.salesforce import fetch_accounts, make_salesforce_client

logger = logging.getLogger("wip.sync.runner")

SyncMode = Literal["full", "incremental"]


# Fields that ARE mirrored from Salesforce → wip_company; if any of these
# change, that's a material audit event. (`last_*_sync_at` etc. are
# housekeeping and are ignored by diff_fields below.)
_MIRRORED_FIELDS = (
    "salesforce_account_name", "account_type", "industry",
    "owner_salesforce_id", "owner_name",
    "billing_street", "billing_city", "billing_postcode", "billing_country",
    "tlp_flag",
    "canonical_company_id",
    "is_active_in_salesforce", "salesforce_deleted_at",
    "account_name_normalised", "duplicate_group_key",
)

_AUDIT_IGNORE_FIELDS = (
    "id", "created_at", "updated_at",
    "last_seen_in_salesforce_at", "last_salesforce_sync_at",
    "is_potential_duplicate",       # set by duplicate detector, not sync
    "duplicate_review_status",      # operator-managed
)


@dataclass
class SyncResult:
    sync_run_id: int | None = None
    mode: SyncMode = "full"
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    soft_deleted: int = 0
    reactivated: int = 0
    canonical_resolved: int = 0
    canonical_unresolved: int = 0
    canonical_ambiguous: int = 0
    duplicates: DuplicateDetectionResult = field(default_factory=DuplicateDetectionResult)
    error_message: str | None = None
    incremental_since: datetime | None = None


def run_sync(
    conn,
    *,
    mode: SyncMode,
    apply: bool,
    sf_client=None,
    canonical_resolver: CanonicalResolver | None = None,
) -> SyncResult:
    """Run one sync. Returns SyncResult. Raises on unrecoverable error
    AFTER writing the sync_run row with status='failed'.

    Args:
        conn: pymysql connection to nif_wip (autocommit=False — runner manages tx).
        mode: 'full' or 'incremental'.
        apply: False = dry-run (no wip_company / candidate writes; sync_run row IS
               still written so the dry-run is observable).
        sf_client: optional pre-built simple_salesforce.Salesforce instance
            (mainly for tests).
        canonical_resolver: optional pre-loaded resolver (for tests).
    """
    result = SyncResult(mode=mode)

    # 1. Watermark for incremental
    since: datetime | None = None
    if mode == "incremental":
        since = _last_successful_incremental_completed_at(conn)
        result.incremental_since = since

    # 2. Open sync_run row (always — even dry-run, with a 'dry_run_' actor prefix)
    sync_run_id = _open_sync_run(conn, mode=mode, since=since, apply=apply)
    result.sync_run_id = sync_run_id

    try:
        # 3. Pre-load canonical resolver (one cross-DB hit)
        if canonical_resolver is None:
            canonical_resolver = CanonicalResolver.load()

        # 4. Fetch from Salesforce
        if sf_client is None:
            sf_client = make_salesforce_client()
        accounts = fetch_accounts(sf_client, since=since, include_deleted=True)
        result.fetched = len(accounts)

        # 5. Per-account upsert
        actor = ("dry_run_" if not apply else "") + "sf_sync"
        seen_sf_ids: set[str] = set()
        for acct in accounts:
            sf_id = acct["Id"]
            if not sf_id:
                continue
            seen_sf_ids.add(sf_id)
            _process_account(
                conn, acct,
                resolver=canonical_resolver,
                sync_run_id=sync_run_id,
                actor=actor,
                apply=apply,
                result=result,
            )

        # 6. Full mode: soft-retire anything not seen in this fetch
        if mode == "full":
            _soft_retire_unseen(
                conn,
                seen_sf_ids=seen_sf_ids,
                sync_run_id=sync_run_id,
                actor=actor,
                apply=apply,
                result=result,
            )

        # 7. Duplicate detection (operates on what's now in wip_company)
        if apply:
            result.duplicates = detect_and_upsert_duplicates(
                conn, sync_run_id=sync_run_id,
            )

        # 8. Complete the sync_run row
        _close_sync_run(conn, sync_run_id=sync_run_id, status="completed", result=result)
        if apply:
            conn.commit()
        else:
            # Dry-run: roll back any accidental writes, but keep the sync_run
            # row visible by committing only THAT update separately. Simpler:
            # explicit rollback for safety, then a second small commit for the
            # sync_run close.
            conn.rollback()
            _close_sync_run(conn, sync_run_id=sync_run_id, status="completed", result=result)
            conn.commit()
        return result

    except Exception as e:
        result.error_message = str(e)
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            _close_sync_run(conn, sync_run_id=sync_run_id, status="failed", result=result)
            conn.commit()
        except Exception:
            logger.exception("could not record sync_run failure status")
        logger.exception("sync failed: %s", e)
        raise


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _open_sync_run(conn, *, mode: SyncMode, since: datetime | None, apply: bool) -> int:
    """Insert wip_sync_run row with status='running'. Returns the new id."""
    sync_type = (
        "salesforce_account_full" if mode == "full"
        else "salesforce_account_incremental"
    )
    now = datetime.now(timezone.utc)
    # sync_run is informational telemetry; commit immediately so it's visible
    # even if the sync later crashes.
    autocommit_before = getattr(conn, "get_autocommit", lambda: False)()
    conn.autocommit(True)
    try:
        with conn.cursor() as c:
            c.execute(
                "INSERT INTO wip_sync_run "
                "(sync_type, started_at, status, incremental_since) "
                "VALUES (%s, %s, 'running', %s)",
                (sync_type, now, since),
            )
            sync_run_id = c.lastrowid
        logger.info("opened wip_sync_run id=%d type=%s since=%s apply=%s",
                    sync_run_id, sync_type, since, apply)
        return sync_run_id
    finally:
        conn.autocommit(autocommit_before)


def _close_sync_run(conn, *, sync_run_id: int, status: str, result: SyncResult) -> None:
    """Mark the run row completed/failed with the gathered telemetry."""
    autocommit_before = getattr(conn, "get_autocommit", lambda: False)()
    conn.autocommit(True)
    try:
        with conn.cursor() as c:
            c.execute(
                """
                UPDATE wip_sync_run SET
                    completed_at = %s,
                    status = %s,
                    sf_accounts_fetched = %s,
                    sf_accounts_inserted = %s,
                    sf_accounts_updated = %s,
                    sf_accounts_unchanged = %s,
                    sf_accounts_soft_deleted = %s,
                    canonical_resolved = %s,
                    canonical_unresolved = %s,
                    error_message = %s
                 WHERE id = %s
                """,
                (
                    datetime.now(timezone.utc), status,
                    result.fetched, result.inserted, result.updated,
                    result.unchanged, result.soft_deleted,
                    result.canonical_resolved, result.canonical_unresolved,
                    result.error_message, sync_run_id,
                ),
            )
    finally:
        conn.autocommit(autocommit_before)


def _last_successful_incremental_completed_at(conn) -> datetime | None:
    """Return the watermark for the next incremental sync (or None on first run)."""
    with conn.cursor() as c:
        c.execute(
            "SELECT completed_at FROM wip_sync_run "
            "WHERE sync_type = 'salesforce_account_incremental' "
            "  AND status = 'completed' "
            "ORDER BY completed_at DESC LIMIT 1"
        )
        row = c.fetchone()
    return row["completed_at"] if row and row["completed_at"] else None


def _process_account(
    conn, acct: dict, *,
    resolver: CanonicalResolver,
    sync_run_id: int,
    actor: str,
    apply: bool,
    result: SyncResult,
) -> None:
    """Insert / update / unchanged a single Salesforce Account."""
    sf_id = acct["Id"]
    name = acct["Name"] or ""

    # Build the proposed mirrored field set
    norm = normalise_company_name(name)
    grp = duplicate_group_key(name)
    cc_id, cc_status = resolver.resolve(name, norm)
    if cc_status == "resolved":
        result.canonical_resolved += 1
    elif cc_status == "ambiguous":
        result.canonical_ambiguous += 1
    else:
        result.canonical_unresolved += 1

    is_active = 0 if acct.get("IsDeleted") else 1
    sf_deleted_at = (
        datetime.now(timezone.utc) if acct.get("IsDeleted") else None
    )

    proposed = {
        "salesforce_account_id":    sf_id,
        "salesforce_account_name":  name,
        "account_name_normalised":  norm,
        "duplicate_group_key":      grp,
        "account_type":             acct.get("Type"),
        "industry":                 acct.get("Industry"),
        "owner_salesforce_id":      acct.get("OwnerId"),
        "owner_name":               acct.get("Owner_Name"),
        "billing_street":           acct.get("BillingStreet"),
        "billing_city":             acct.get("BillingCity"),
        "billing_postcode":         acct.get("BillingPostalCode"),
        "billing_country":          acct.get("BillingCountry"),
        # SF returns TLP__c as True/False/None — treat None and False both as 0
        # so that a Salesforce Account without the custom field set doesn't
        # produce a phantom "change" against an existing 0/None on every sync.
        "tlp_flag":                 1 if acct.get("TLP__c") else 0,
        "canonical_company_id":     cc_id,
        "is_active_in_salesforce":  is_active,
        "salesforce_deleted_at":    sf_deleted_at,
    }

    # Look up existing row
    with conn.cursor() as c:
        c.execute(
            "SELECT * FROM wip_company WHERE salesforce_account_id = %s",
            (sf_id,),
        )
        existing = c.fetchone()

    now = datetime.now(timezone.utc)

    if existing is None:
        if not apply:
            result.inserted += 1
            return
        _insert_company(conn, proposed, now=now)
        # Re-read for audit (get the auto-assigned id)
        with conn.cursor() as c:
            c.execute("SELECT * FROM wip_company WHERE salesforce_account_id = %s", (sf_id,))
            new_row = c.fetchone()
        write_audit(
            conn,
            entity_type="wip_company",
            entity_id=new_row["id"],
            action="insert",
            after={k: new_row.get(k) for k in _MIRRORED_FIELDS},
            actor=actor,
            sync_run_id=sync_run_id,
        )
        result.inserted += 1
        return

    # Existing — compute changes
    existing_subset = {k: existing.get(k) for k in proposed.keys()}
    changes = diff_fields(existing_subset, proposed, ignore=_AUDIT_IGNORE_FIELDS)

    # Reactivation case: existing was inactive, now active again
    if existing.get("is_active_in_salesforce") == 0 and is_active == 1:
        action = "reactivate"
    elif existing.get("is_active_in_salesforce") == 1 and is_active == 0:
        action = "soft_delete"
    elif changes:
        action = "update"
    else:
        # Unchanged: just bump last_seen and last_sync timestamps
        if apply:
            with conn.cursor() as c:
                c.execute(
                    "UPDATE wip_company SET last_seen_in_salesforce_at = %s, "
                    "last_salesforce_sync_at = %s WHERE id = %s",
                    (now, now, existing["id"]),
                )
        result.unchanged += 1
        return

    if not apply:
        if action == "soft_delete":
            result.soft_deleted += 1
        elif action == "reactivate":
            result.reactivated += 1
        else:
            result.updated += 1
        return

    # Apply the update
    _update_company(conn, existing_id=existing["id"], proposed=proposed, now=now)
    write_audit(
        conn,
        entity_type="wip_company",
        entity_id=existing["id"],
        action=action,
        changed_fields=changes,
        after=proposed,
        actor=actor,
        sync_run_id=sync_run_id,
    )
    if action == "soft_delete":
        result.soft_deleted += 1
    elif action == "reactivate":
        result.reactivated += 1
    else:
        result.updated += 1


def _insert_company(conn, p: dict, *, now: datetime) -> None:
    with conn.cursor() as c:
        c.execute(
            """
            INSERT INTO wip_company (
                salesforce_account_id, salesforce_account_name,
                account_name_normalised, duplicate_group_key,
                account_type, industry,
                owner_salesforce_id, owner_name,
                billing_street, billing_city, billing_postcode, billing_country,
                tlp_flag, canonical_company_id,
                is_active_in_salesforce, salesforce_deleted_at,
                last_seen_in_salesforce_at, last_salesforce_sync_at,
                created_at, updated_at
            ) VALUES (
                %(salesforce_account_id)s, %(salesforce_account_name)s,
                %(account_name_normalised)s, %(duplicate_group_key)s,
                %(account_type)s, %(industry)s,
                %(owner_salesforce_id)s, %(owner_name)s,
                %(billing_street)s, %(billing_city)s, %(billing_postcode)s, %(billing_country)s,
                %(tlp_flag)s, %(canonical_company_id)s,
                %(is_active_in_salesforce)s, %(salesforce_deleted_at)s,
                %(now)s, %(now)s, %(now)s, %(now)s
            )
            """,
            {**p, "now": now},
        )


def _update_company(conn, *, existing_id: int, proposed: dict, now: datetime) -> None:
    with conn.cursor() as c:
        c.execute(
            """
            UPDATE wip_company SET
                salesforce_account_name = %(salesforce_account_name)s,
                account_name_normalised = %(account_name_normalised)s,
                duplicate_group_key     = %(duplicate_group_key)s,
                account_type            = %(account_type)s,
                industry                = %(industry)s,
                owner_salesforce_id     = %(owner_salesforce_id)s,
                owner_name              = %(owner_name)s,
                billing_street          = %(billing_street)s,
                billing_city            = %(billing_city)s,
                billing_postcode        = %(billing_postcode)s,
                billing_country         = %(billing_country)s,
                tlp_flag                = %(tlp_flag)s,
                canonical_company_id    = %(canonical_company_id)s,
                is_active_in_salesforce = %(is_active_in_salesforce)s,
                salesforce_deleted_at   = %(salesforce_deleted_at)s,
                last_seen_in_salesforce_at = %(now)s,
                last_salesforce_sync_at = %(now)s,
                updated_at              = %(now)s
            WHERE id = %(existing_id)s
            """,
            {**proposed, "now": now, "existing_id": existing_id},
        )


def _soft_retire_unseen(
    conn, *,
    seen_sf_ids: set[str],
    sync_run_id: int,
    actor: str,
    apply: bool,
    result: SyncResult,
) -> None:
    """Full-sync only: any active wip_company NOT in this fetch is soft-retired.

    Architect rule: NEVER hard-delete; just set is_active_in_salesforce=0 and
    salesforce_deleted_at=NOW(). Historical wip_case rows still need the FK
    to resolve.
    """
    with conn.cursor() as c:
        c.execute(
            "SELECT id, salesforce_account_id FROM wip_company "
            "WHERE is_active_in_salesforce = 1"
        )
        active = list(c.fetchall())

    unseen = [r for r in active if r["salesforce_account_id"] not in seen_sf_ids]
    now = datetime.now(timezone.utc)
    for r in unseen:
        result.soft_deleted += 1
        if not apply:
            continue
        with conn.cursor() as c:
            c.execute(
                "UPDATE wip_company SET is_active_in_salesforce = 0, "
                "salesforce_deleted_at = %s, last_salesforce_sync_at = %s, "
                "updated_at = %s WHERE id = %s",
                (now, now, now, r["id"]),
            )
        write_audit(
            conn,
            entity_type="wip_company",
            entity_id=r["id"],
            action="soft_delete",
            changed_fields={
                "is_active_in_salesforce": {"before": 1, "after": 0},
                "salesforce_deleted_at":   {"before": None, "after": now.isoformat()},
            },
            after={"salesforce_account_id": r["salesforce_account_id"], "is_active_in_salesforce": 0},
            actor=actor,
            sync_run_id=sync_run_id,
        )
