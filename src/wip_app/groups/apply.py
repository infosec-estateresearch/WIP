"""WO-14A.1 — operator-decision applier.

Reads a marked review CSV (one row per group) and applies operator
decisions to the wip_company_group + wip_company_group_member tables.

MVP supported `operator_decision` values:
  - APPROVE_GROUP       — set group + members to APPROVED
  - REJECT_GROUP        — set group to REJECTED + add to sticky-reject
  - NEEDS_MORE_REVIEW   — flip status to NEEDS_REVIEW; no member change

Deferred per the architect's direction (require WO-14B amendments):
  - SPLIT_GROUP
  - MERGE_INTO=<group_id>

Rules enforced (per WO-14B §7):
  - APPROVE_GROUP requires set_primary_sf_id (or one will be chosen if
    the group has exactly one member); refuses ambiguous APPROVE.
  - The schema's partial UNIQUE indexes on
    wip_company_group_member.canonical_company_id_when_approved /
    wip_company_id_when_approved guard against attaching a
    canonical/wip_company that's already in another APPROVED group.
    The apply script catches the DB error and surfaces it to the
    operator rather than half-applying.
  - Each decision row is one transaction. A failure on row 5 doesn't
    leave half a group applied.
  - Idempotent: re-running the same CSV is a no-op (already-APPROVED
    groups skip; already-REJECTED stay).
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pymysql

logger = logging.getLogger("wip.groups.apply")


ALLOWED_DECISIONS = {"APPROVE_GROUP", "REJECT_GROUP", "NEEDS_MORE_REVIEW"}


@dataclass
class ApplyResult:
    rows_seen: int = 0
    groups_approved: int = 0
    groups_rejected: int = 0
    groups_needs_review: int = 0
    rows_skipped: int = 0
    rows_failed: int = 0
    errors: list[str] = field(default_factory=list)
    decisions_unknown: list[str] = field(default_factory=list)


@dataclass
class _Decision:
    cluster_id: str
    group_name: str
    group_name_normalised: str
    decision: str
    set_primary_sf_id: str | None
    operator_note: str | None
    row_number: int


def _read_decisions(csv_path: Path) -> list[_Decision]:
    """Parse the marked CSV. Skip rows with empty operator_decision."""
    out = []
    with open(csv_path, newline="") as f:
        r = csv.DictReader(f)
        for i, row in enumerate(r, start=2):  # row 1 is header
            decision = (row.get("operator_decision") or "").strip().upper()
            if not decision:
                continue
            out.append(_Decision(
                cluster_id=row.get("cluster_id", ""),
                group_name=row.get("group_name", ""),
                group_name_normalised=(row.get("group_name_normalised") or "").strip(),
                decision=decision,
                set_primary_sf_id=(row.get("set_primary_sf_id") or "").strip() or None,
                operator_note=(row.get("operator_note") or "").strip() or None,
                row_number=i,
            ))
    return out


def _find_group_row(conn, *, cluster_id: str, normalised: str) -> dict | None:
    """Look up the existing wip_company_group row for this CSV row.
    Match priority: source_cluster_id → group_name_normalised in
    PROPOSED/NEEDS_REVIEW state. Returns None if nothing to act on."""
    with conn.cursor() as c:
        if cluster_id:
            c.execute(
                "SELECT * FROM wip_company_group WHERE source_cluster_id = %s "
                "  AND review_status IN ('PROPOSED','NEEDS_REVIEW','APPROVED','REJECTED') "
                "ORDER BY id DESC LIMIT 1",
                (cluster_id,),
            )
            row = c.fetchone()
            if row:
                return row
        if normalised:
            c.execute(
                "SELECT * FROM wip_company_group WHERE group_name_normalised = %s "
                "  AND review_status IN ('PROPOSED','NEEDS_REVIEW') "
                "ORDER BY id DESC LIMIT 1",
                (normalised,),
            )
            return c.fetchone()
    return None


def _approve_group(
    conn, group_row: dict, set_primary_sf_id: str | None,
    operator_note: str | None,
) -> None:
    """Promote one PROPOSED/NEEDS_REVIEW group + all its members to
    APPROVED. Selects PRIMARY by operator-provided SF id; falls back to
    the single member if there's exactly one; otherwise refuses."""
    now = datetime.now(timezone.utc)
    group_id = int(group_row["id"])

    with conn.cursor() as c:
        c.execute(
            "SELECT id, salesforce_account_id FROM wip_company_group_member "
            "  WHERE group_id = %s",
            (group_id,),
        )
        members = list(c.fetchall())
        if not members:
            raise ValueError(
                f"group {group_id} ('{group_row['group_name']}') has no members"
            )

        # Decide PRIMARY
        if set_primary_sf_id:
            primary_candidates = [m for m in members
                                  if m["salesforce_account_id"] == set_primary_sf_id]
            if not primary_candidates:
                raise ValueError(
                    f"set_primary_sf_id '{set_primary_sf_id}' is not a member of "
                    f"group {group_id} ('{group_row['group_name']}')"
                )
            primary_member_id = int(primary_candidates[0]["id"])
            primary_sf = set_primary_sf_id
        elif len(members) == 1:
            primary_member_id = int(members[0]["id"])
            primary_sf = members[0]["salesforce_account_id"]
        else:
            raise ValueError(
                f"group {group_id} ('{group_row['group_name']}') has "
                f"{len(members)} members but no set_primary_sf_id was provided"
            )

        # Promote group
        c.execute(
            "UPDATE wip_company_group SET "
            "  review_status = 'APPROVED', "
            "  primary_salesforce_account_id = %s, "
            "  reviewed_by = %s, "
            "  reviewed_at = %s, "
            "  resolution_note = COALESCE(%s, resolution_note) "
            "WHERE id = %s",
            (primary_sf, "operator", now, operator_note, group_id),
        )

        # Promote members: PRIMARY for the chosen one, BRANCH for the rest
        # (operator can refine later — BRANCH is a safe default for the
        # canonical-fragmentation case the seed targets).
        c.execute(
            "UPDATE wip_company_group_member SET "
            "  review_status = 'APPROVED', "
            "  member_role = 'PRIMARY', "
            "  reviewed_by = %s, reviewed_at = %s "
            "WHERE id = %s",
            ("operator", now, primary_member_id),
        )
        c.execute(
            "UPDATE wip_company_group_member SET "
            "  review_status = 'APPROVED', "
            "  member_role = 'BRANCH', "
            "  reviewed_by = %s, reviewed_at = %s "
            "WHERE group_id = %s AND id <> %s",
            ("operator", now, group_id, primary_member_id),
        )


def _reject_group(conn, group_row: dict, operator_note: str | None) -> None:
    """REJECT the group AND record it in wip_company_group_seed_reject so
    a re-seed run doesn't propose the same name again."""
    now = datetime.now(timezone.utc)
    group_id = int(group_row["id"])
    normalised = group_row["group_name_normalised"]

    with conn.cursor() as c:
        c.execute(
            "UPDATE wip_company_group SET "
            "  review_status = 'REJECTED', "
            "  reviewed_by = %s, reviewed_at = %s, "
            "  resolution_note = COALESCE(%s, resolution_note) "
            "WHERE id = %s",
            ("operator", now, operator_note, group_id),
        )
        c.execute(
            "UPDATE wip_company_group_member SET "
            "  review_status = 'REJECTED', "
            "  reviewed_by = %s, reviewed_at = %s "
            "WHERE group_id = %s",
            ("operator", now, group_id),
        )
        # Sticky reject — idempotent via UNIQUE on group_name_normalised
        c.execute(
            "INSERT INTO wip_company_group_seed_reject "
            "  (group_name_normalised, rejected_by, rejected_at, reason, "
            "   source_group_id, source_cluster_id) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE "
            "  rejected_by = VALUES(rejected_by), "
            "  rejected_at = VALUES(rejected_at), "
            "  reason = VALUES(reason), "
            "  source_group_id = VALUES(source_group_id)",
            (
                normalised, "operator", now, operator_note,
                group_id, group_row.get("source_cluster_id"),
            ),
        )


def _flag_needs_review(conn, group_row: dict, operator_note: str | None) -> None:
    now = datetime.now(timezone.utc)
    with conn.cursor() as c:
        c.execute(
            "UPDATE wip_company_group SET "
            "  review_status = 'NEEDS_REVIEW', "
            "  reviewed_by = %s, reviewed_at = %s, "
            "  resolution_note = COALESCE(%s, resolution_note) "
            "WHERE id = %s",
            ("operator", now, operator_note, int(group_row["id"])),
        )


def apply_decisions(conn, csv_path: Path, *, dry_run: bool) -> ApplyResult:
    """Read the marked CSV and apply each operator_decision row.

    Transaction scope: ONE per decision row. A failure on row 5 does NOT
    roll back rows 1-4. Each row's success or failure is recorded in the
    result.
    """
    result = ApplyResult()
    decisions = _read_decisions(csv_path)
    result.rows_seen = len(decisions)

    autocommit_before = getattr(conn, "get_autocommit", lambda: False)()
    conn.autocommit(False)

    try:
        for d in decisions:
            if d.decision not in ALLOWED_DECISIONS:
                result.decisions_unknown.append(
                    f"row {d.row_number}: unknown decision '{d.decision}' "
                    f"(allowed: {sorted(ALLOWED_DECISIONS)})"
                )
                result.rows_skipped += 1
                continue

            group_row = _find_group_row(
                conn,
                cluster_id=d.cluster_id,
                normalised=d.group_name_normalised,
            )
            if group_row is None:
                result.errors.append(
                    f"row {d.row_number}: no group found for cluster_id="
                    f"'{d.cluster_id}' / normalised='{d.group_name_normalised}'"
                )
                result.rows_failed += 1
                continue

            # Idempotency: skip if the target state is already in place
            current_status = group_row["review_status"]
            if d.decision == "APPROVE_GROUP" and current_status == "APPROVED":
                result.rows_skipped += 1
                continue
            if d.decision == "REJECT_GROUP" and current_status == "REJECTED":
                result.rows_skipped += 1
                continue

            try:
                if d.decision == "APPROVE_GROUP":
                    _approve_group(conn, group_row, d.set_primary_sf_id, d.operator_note)
                    if dry_run:
                        conn.rollback()
                    else:
                        conn.commit()
                    result.groups_approved += 1
                elif d.decision == "REJECT_GROUP":
                    _reject_group(conn, group_row, d.operator_note)
                    if dry_run:
                        conn.rollback()
                    else:
                        conn.commit()
                    result.groups_rejected += 1
                elif d.decision == "NEEDS_MORE_REVIEW":
                    _flag_needs_review(conn, group_row, d.operator_note)
                    if dry_run:
                        conn.rollback()
                    else:
                        conn.commit()
                    result.groups_needs_review += 1
            except Exception as e:
                conn.rollback()
                result.errors.append(
                    f"row {d.row_number} ({d.group_name}): {e}"
                )
                result.rows_failed += 1
    finally:
        conn.autocommit(autocommit_before)

    return result
