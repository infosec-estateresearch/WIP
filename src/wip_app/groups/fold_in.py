"""WO-14A.3 — fold-in pass for under-membered PROPOSED groups.

Background: the WO-14A.1 seed proposer read the original WO-13 candidates
CSV, which truncated long SF-ID lists at 8 entries (with a
" ... (+N more)" suffix). High-fragmentation clusters (Knights with 35
SF accounts, Wards with 13, etc.) ended up with under-membered live
PROPOSED groups. WO-14A.2's fix to `fmt_id_list` produces the full IDs
going forward, but the already-seeded groups still carry the smaller
membership. This module folds in the missing members.

Critical boundaries (per WO-14A.3 spec):
  - APPROVED groups: untouched
  - REJECTED groups: untouched
  - Sticky-rejected names: untouched (they have no live group anyway)
  - PROPOSED groups: may receive new PROPOSED members
  - NEEDS_REVIEW groups: may receive new PROPOSED members; status stays
    NEEDS_REVIEW (don't reopen the operator's pending decision)
  - Never delete an existing member
  - Never auto-approve anything
  - A wip_company already in ANOTHER APPROVED group is refused (the
    structural invariant from WO-14B §3 — one canonical/wip_company
    per APPROVED group); we surface it as a per-row skip with reason.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("wip.groups.fold_in")


SEED_PROPOSER_TAG = "wo-14a-3-foldin"


@dataclass
class GroupFoldInPlan:
    """Per-group fold-in plan: what's missing, what we'll add, and what
    we have to skip and why. Returned in dry-run for operator review;
    inserted in --apply mode."""
    group_id: int
    group_name: str
    group_name_normalised: str
    review_status: str
    source_cluster_id: str | None
    existing_member_wip_ids: list[int] = field(default_factory=list)
    expected_member_sf_ids: list[str] = field(default_factory=list)
    missing_wip_companies: list[dict] = field(default_factory=list)
    skipped_already_approved_elsewhere: list[dict] = field(default_factory=list)
    skipped_unknown_sf_ids: list[str] = field(default_factory=list)
    skipped_no_source_cluster: bool = False


@dataclass
class FoldInResult:
    groups_examined: int = 0
    groups_with_missing: int = 0
    groups_skipped_no_cluster: int = 0
    groups_skipped_not_pending: int = 0
    members_added: int = 0
    members_skipped_in_approved_elsewhere: int = 0
    members_skipped_unknown_sf: int = 0
    per_group: list[GroupFoldInPlan] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def _load_clusters_by_id(candidates_path: Path) -> dict[str, list[str]]:
    """{cluster_id: [salesforce_account_id, ...]} from the corrected
    WO-13 candidates CSV. Anything not in the file maps to no expected
    members (those groups get no fold-in).

    Defensive: Salesforce Account IDs are exactly 15 or 18 alphanumeric
    chars (no spaces, no punctuation). Filter anything else so stray
    truncation tokens like "..." or "(+27 more)" from a pre-WO-14A.2
    candidates CSV don't get parsed as IDs.
    """
    out: dict[str, list[str]] = {}
    with open(candidates_path, newline="") as f:
        for row in csv.DictReader(f):
            cid = row.get("cluster_id")
            if not cid:
                continue
            raw = row.get("salesforce_account_ids", "") or ""
            ids = [s.strip() for s in raw.split("|") if s.strip()]
            ids = [s for s in ids if len(s) in (15, 18) and s.isalnum()]
            out[cid] = ids
    return out


def _load_pending_groups(conn) -> list[dict]:
    """PROPOSED + NEEDS_REVIEW groups only. APPROVED and REJECTED are
    intentionally excluded — WO-14A.3 must not touch them."""
    with conn.cursor() as c:
        c.execute(
            "SELECT id, group_name, group_name_normalised, review_status, "
            "       source_cluster_id "
            "  FROM wip_company_group "
            " WHERE review_status IN ('PROPOSED', 'NEEDS_REVIEW')"
        )
        return list(c.fetchall())


def _current_member_wip_ids(conn, group_id: int) -> list[int]:
    with conn.cursor() as c:
        c.execute(
            "SELECT wip_company_id FROM wip_company_group_member "
            " WHERE group_id = %s",
            (group_id,),
        )
        return [int(r["wip_company_id"]) for r in c.fetchall()]


def _wip_companies_by_sf_id(conn, sf_ids: list[str]) -> dict[str, dict]:
    if not sf_ids:
        return {}
    placeholders = ",".join(["%s"] * len(sf_ids))
    with conn.cursor() as c:
        c.execute(
            f"SELECT id, salesforce_account_id, salesforce_account_name, "
            f"       canonical_company_id "
            f"  FROM wip_company "
            f" WHERE salesforce_account_id IN ({placeholders})",
            sf_ids,
        )
        return {r["salesforce_account_id"]: r for r in c.fetchall()}


def _wip_ids_in_approved_groups(conn, wip_ids: list[int]) -> set[int]:
    """The subset of wip_ids that are APPROVED members of SOME group.
    Used to guard against re-attaching a wip_company that's already
    owned by another APPROVED group."""
    if not wip_ids:
        return set()
    placeholders = ",".join(["%s"] * len(wip_ids))
    with conn.cursor() as c:
        c.execute(
            f"SELECT DISTINCT wip_company_id "
            f"  FROM wip_company_group_member "
            f" WHERE review_status = 'APPROVED' "
            f"   AND wip_company_id IN ({placeholders})",
            wip_ids,
        )
        return {int(r["wip_company_id"]) for r in c.fetchall()}


# ---------------------------------------------------------------------------
# Plan + apply
# ---------------------------------------------------------------------------

def build_plan(conn, candidates_path: Path) -> FoldInResult:
    """Pure computation — does NOT mutate the DB."""
    clusters = _load_clusters_by_id(candidates_path)
    pending = _load_pending_groups(conn)

    result = FoldInResult()

    for g in pending:
        result.groups_examined += 1

        cluster_id = g["source_cluster_id"]
        if not cluster_id:
            result.groups_skipped_no_cluster += 1
            result.per_group.append(GroupFoldInPlan(
                group_id=int(g["id"]),
                group_name=g["group_name"],
                group_name_normalised=g["group_name_normalised"],
                review_status=g["review_status"],
                source_cluster_id=None,
                skipped_no_source_cluster=True,
            ))
            continue

        expected_sf_ids = clusters.get(cluster_id, [])
        existing_wip_ids = _current_member_wip_ids(conn, int(g["id"]))
        wc_by_sf = _wip_companies_by_sf_id(conn, expected_sf_ids)

        plan = GroupFoldInPlan(
            group_id=int(g["id"]),
            group_name=g["group_name"],
            group_name_normalised=g["group_name_normalised"],
            review_status=g["review_status"],
            source_cluster_id=cluster_id,
            existing_member_wip_ids=existing_wip_ids,
            expected_member_sf_ids=expected_sf_ids,
        )

        # SF ids in the cluster that we couldn't resolve to a live wip_company
        for sf in expected_sf_ids:
            if sf not in wc_by_sf:
                plan.skipped_unknown_sf_ids.append(sf)
                result.members_skipped_unknown_sf += 1

        # Candidate wip_company rows the seed missed
        candidate_missing = [
            wc_by_sf[sf] for sf in expected_sf_ids
            if sf in wc_by_sf and int(wc_by_sf[sf]["id"]) not in set(existing_wip_ids)
        ]
        candidate_ids = [int(c["id"]) for c in candidate_missing]

        # Refuse to add a wip_company that's already in some APPROVED group
        # elsewhere — that's the WO-14B §3 invariant.
        approved_elsewhere = _wip_ids_in_approved_groups(conn, candidate_ids)
        for c in candidate_missing:
            if int(c["id"]) in approved_elsewhere:
                plan.skipped_already_approved_elsewhere.append(c)
                result.members_skipped_in_approved_elsewhere += 1
            else:
                plan.missing_wip_companies.append(c)

        if plan.missing_wip_companies:
            result.groups_with_missing += 1

        result.per_group.append(plan)

    return result


def apply_plan(conn, plan_result: FoldInResult) -> FoldInResult:
    """INSERT all `missing_wip_companies` rows under their groups.
    One transaction per group — a failure on Knights doesn't block
    the fold-in on Wards. Updates the result counters in place.
    """
    now = datetime.now(timezone.utc)
    autocommit_before = getattr(conn, "get_autocommit", lambda: False)()
    conn.autocommit(False)
    try:
        for plan in plan_result.per_group:
            if not plan.missing_wip_companies:
                continue
            try:
                with conn.cursor() as c:
                    for wc in plan.missing_wip_companies:
                        c.execute(
                            """
                            INSERT INTO wip_company_group_member
                                (group_id, wip_company_id,
                                 salesforce_account_id, canonical_company_id,
                                 member_role, confidence_source, review_status,
                                 proposed_by, created_at, updated_at)
                            VALUES (%s, %s, %s, %s, 'UNKNOWN',
                                    'WO-13-CLUSTER', 'PROPOSED',
                                    %s, %s, %s)
                            """,
                            (
                                plan.group_id, int(wc["id"]),
                                wc["salesforce_account_id"],
                                wc["canonical_company_id"],
                                SEED_PROPOSER_TAG, now, now,
                            ),
                        )
                        plan_result.members_added += 1
                conn.commit()
            except Exception as e:
                conn.rollback()
                plan_result.errors.append(
                    f"group {plan.group_id} ({plan.group_name}): {e}"
                )
    finally:
        conn.autocommit(autocommit_before)
    return plan_result
