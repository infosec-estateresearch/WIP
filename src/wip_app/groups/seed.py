"""WO-14A.1 — group seed proposer.

Reads the WO-13 cluster CSV produced by
`ops/evidence/WO-13/generate_alignment_report.py`, filters to actionable
canonical-fragmentation clusters, and emits one PROPOSED group + members
per surviving cluster.

Rules (from WO-14B blueprint §4.1 / §7):
  - Only `CANONICAL_FRAGMENTATION` clusters
  - Only clusters with `impact_score >= MIN_IMPACT_SCORE` (default 20)
  - Skip clusters whose `group_name_normalised` is in
    `wip_company_group_seed_reject` (sticky-reject set)
  - Skip clusters where ANY member wip_company is already in an APPROVED
    group (idempotency — re-running the seed never duplicates)
  - When `seed_warning` applies (mixed account_types, >=6 cities), set
    the flag on the group row so the operator review CSV surfaces it
  - All seeded rows are PROPOSED; nothing becomes APPROVED here.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

logger = logging.getLogger("wip.groups.seed")


MIN_IMPACT_SCORE = 20.0
SEED_PROPOSER_TAG = "wo-13-seed"

# Cluster categories from the WO-13 report — seeding scope.
CATEGORY_FRAGMENTATION = "CANONICAL_FRAGMENTATION"

# A 6+-city cluster gets `seed_warning='wide_geographic_split'` so the
# operator sees it and reviews split-by-default (WO-14B §7 rule 4). The
# blueprint says "should be split-by-default — operator must explicitly
# opt back into a single group." We surface, we don't auto-split.
WIDE_GEO_CITY_THRESHOLD = 6


@dataclass
class SeededGroup:
    """One PROPOSED group + members about to be inserted, or already
    skipped. Returned by `propose_groups` so callers can render summary
    output (dry-run) or actually INSERT (apply)."""
    cluster_id: str
    group_name: str
    group_name_normalised: str
    member_wip_ids: list[int]
    member_sf_ids: list[str]
    member_canonical_ids: list[int]
    member_account_names: list[str]
    distinct_cities: list[str]
    suggested_primary_canonical_id: int | None
    seed_warning: str | None
    impact_score: float
    skipped_reason: str | None = None  # set when we won't insert


def _read_clusters(csv_path: Path) -> Iterable[dict]:
    """Yield every row from the WO-13 candidates CSV. No filtering yet."""
    with open(csv_path, newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            yield row


def _parse_list(field: str) -> list[str]:
    """WO-13 CSV uses ' | ' as the in-cell list separator (see fmt_list)."""
    if not field:
        return []
    return [x.strip() for x in field.split("|") if x.strip()]


def _parse_int_list(field: str) -> list[int]:
    out = []
    for s in _parse_list(field):
        try:
            out.append(int(s))
        except ValueError:
            pass
    return out


def _suggested_primary_canonical(canonical_ids: list[int]) -> int | None:
    """Per WO-14B §6.2: the canonical with the lowest id (and no
    parenthetical / city suffix) — proxy for "the parent canonical".
    The seed always suggests the lowest id; operator confirms or overrides
    when they fill `set_primary_sf_id` in the review CSV."""
    return min(canonical_ids) if canonical_ids else None


def _load_existing_approved_wip_ids(conn) -> set[int]:
    """wip_company_ids already in an APPROVED group. The seed never
    re-proposes one — those are owned."""
    with conn.cursor() as c:
        c.execute(
            "SELECT DISTINCT wip_company_id FROM wip_company_group_member "
            " WHERE review_status = 'APPROVED'"
        )
        return {int(r["wip_company_id"]) for r in c.fetchall()}


def _load_rejected_normalised_names(conn) -> set[str]:
    """The sticky-reject set. Either expires_at is NULL (forever) or in
    the future. Anything past expires_at is treated as un-rejected."""
    with conn.cursor() as c:
        c.execute(
            "SELECT group_name_normalised FROM wip_company_group_seed_reject "
            " WHERE expires_at IS NULL OR expires_at > UTC_TIMESTAMP(6)"
        )
        return {r["group_name_normalised"] for r in c.fetchall()}


def _load_existing_proposed_or_review_normalised(conn) -> set[str]:
    """Names with an open PROPOSED or NEEDS_REVIEW group — don't re-seed,
    the operator already has one to deal with."""
    with conn.cursor() as c:
        c.execute(
            "SELECT DISTINCT group_name_normalised FROM wip_company_group "
            " WHERE review_status IN ('PROPOSED', 'NEEDS_REVIEW')"
        )
        return {r["group_name_normalised"] for r in c.fetchall()}


def propose_groups(
    conn,
    csv_path: Path,
    *,
    min_impact_score: float = MIN_IMPACT_SCORE,
) -> list[SeededGroup]:
    """Build the proposed group list. Pure computation — does NOT write
    to the DB. Caller decides whether to dry-run-print or to INSERT.
    """
    already_approved = _load_existing_approved_wip_ids(conn)
    sticky_rejects = _load_rejected_normalised_names(conn)
    already_proposed = _load_existing_proposed_or_review_normalised(conn)

    seeded: list[SeededGroup] = []
    for row in _read_clusters(csv_path):
        if row.get("category") != CATEGORY_FRAGMENTATION:
            continue

        try:
            impact = float(row.get("impact_score") or 0)
        except ValueError:
            impact = 0.0
        if impact < min_impact_score:
            continue

        group_label = (row.get("group_label") or "").strip()
        if not group_label:
            continue

        canonical_ids = _parse_int_list(row.get("canonical_company_ids", ""))
        sf_ids = _parse_list(row.get("salesforce_account_ids", ""))
        names = _parse_list(row.get("account_names", ""))
        cities = _parse_list(row.get("distinct_cities", ""))

        # Pull wip_company_ids for the cluster by SF account id (the WO-13
        # CSV doesn't include the wip_company_id directly, but SF ids are
        # globally unique).
        wip_ids: list[int] = []
        with conn.cursor() as c:
            if sf_ids:
                placeholders = ",".join(["%s"] * len(sf_ids))
                c.execute(
                    f"SELECT id, salesforce_account_id, canonical_company_id "
                    f"FROM wip_company WHERE salesforce_account_id IN ({placeholders})",
                    sf_ids,
                )
                # Keep deterministic order by SF id input
                wc_by_sf = {r["salesforce_account_id"]: r for r in c.fetchall()}
                for sf in sf_ids:
                    rec = wc_by_sf.get(sf)
                    if rec:
                        wip_ids.append(int(rec["id"]))

        # Idempotency: if ANY member wip_company is already in an
        # APPROVED group, skip — that cluster is "owned".
        if any(wid in already_approved for wid in wip_ids):
            seeded.append(SeededGroup(
                cluster_id=row.get("cluster_id", ""),
                group_name=group_label.title(),
                group_name_normalised=group_label,
                member_wip_ids=wip_ids,
                member_sf_ids=sf_ids,
                member_canonical_ids=canonical_ids,
                member_account_names=names,
                distinct_cities=cities,
                suggested_primary_canonical_id=_suggested_primary_canonical(canonical_ids),
                seed_warning=None,
                impact_score=impact,
                skipped_reason="member_already_in_approved_group",
            ))
            continue

        # Sticky-reject: don't re-propose what the operator already rejected
        if group_label in sticky_rejects:
            seeded.append(SeededGroup(
                cluster_id=row.get("cluster_id", ""),
                group_name=group_label.title(),
                group_name_normalised=group_label,
                member_wip_ids=wip_ids,
                member_sf_ids=sf_ids,
                member_canonical_ids=canonical_ids,
                member_account_names=names,
                distinct_cities=cities,
                suggested_primary_canonical_id=_suggested_primary_canonical(canonical_ids),
                seed_warning=None,
                impact_score=impact,
                skipped_reason="sticky_rejected",
            ))
            continue

        # Don't re-seed if a PROPOSED/NEEDS_REVIEW already exists by name
        if group_label in already_proposed:
            seeded.append(SeededGroup(
                cluster_id=row.get("cluster_id", ""),
                group_name=group_label.title(),
                group_name_normalised=group_label,
                member_wip_ids=wip_ids,
                member_sf_ids=sf_ids,
                member_canonical_ids=canonical_ids,
                member_account_names=names,
                distinct_cities=cities,
                suggested_primary_canonical_id=_suggested_primary_canonical(canonical_ids),
                seed_warning=None,
                impact_score=impact,
                skipped_reason="already_proposed_or_needs_review",
            ))
            continue

        # Compute seed warnings
        seed_warning = None
        types = _parse_list(row.get("distinct_account_types", ""))
        if len(set(types)) >= 2:
            seed_warning = "mixed_account_types"
        elif len(cities) >= WIDE_GEO_CITY_THRESHOLD:
            seed_warning = "wide_geographic_split"

        seeded.append(SeededGroup(
            cluster_id=row.get("cluster_id", ""),
            group_name=group_label.title(),
            group_name_normalised=group_label,
            member_wip_ids=wip_ids,
            member_sf_ids=sf_ids,
            member_canonical_ids=canonical_ids,
            member_account_names=names,
            distinct_cities=cities,
            suggested_primary_canonical_id=_suggested_primary_canonical(canonical_ids),
            seed_warning=seed_warning,
            impact_score=impact,
        ))

    return seeded


def insert_proposed(conn, seeded: list[SeededGroup]) -> int:
    """INSERT every non-skipped SeededGroup as one wip_company_group +
    its wip_company_group_member rows in a single transaction. Returns
    the number of groups inserted.
    """
    now = datetime.now(timezone.utc)
    inserted = 0
    autocommit_before = getattr(conn, "get_autocommit", lambda: False)()
    conn.autocommit(False)
    try:
        with conn.cursor() as c:
            for s in seeded:
                if s.skipped_reason is not None:
                    continue
                if not s.member_wip_ids:
                    continue
                # Map suggested_primary_canonical_id back to a wip_company_id
                # so the operator can later promote one of THESE rows to
                # PRIMARY. For now no member is PRIMARY in the seed; the
                # operator picks via the review CSV.
                c.execute(
                    """
                    INSERT INTO wip_company_group
                        (group_name, group_name_normalised,
                         primary_salesforce_account_id,
                         primary_canonical_company_id,
                         group_type, review_status,
                         source_cluster_id, proposed_by, seed_warning,
                         created_at, updated_at)
                    VALUES (%s, %s, NULL, %s, %s, 'PROPOSED', %s, %s, %s, %s, %s)
                    """,
                    (
                        s.group_name, s.group_name_normalised,
                        s.suggested_primary_canonical_id,
                        # Mixed account_types → operator likely needs UNKNOWN
                        "UNKNOWN" if s.seed_warning == "mixed_account_types" else "FIRM_GROUP",
                        s.cluster_id, SEED_PROPOSER_TAG, s.seed_warning,
                        now, now,
                    ),
                )
                group_id = c.lastrowid

                # Pull each wip_company row to get canonical_company_id /
                # salesforce_account_id (denormalised onto the member row).
                placeholders = ",".join(["%s"] * len(s.member_wip_ids))
                c.execute(
                    f"SELECT id, salesforce_account_id, canonical_company_id "
                    f"FROM wip_company WHERE id IN ({placeholders})",
                    s.member_wip_ids,
                )
                wc_rows = c.fetchall()
                for wc in wc_rows:
                    c.execute(
                        """
                        INSERT INTO wip_company_group_member
                            (group_id, wip_company_id,
                             salesforce_account_id, canonical_company_id,
                             member_role, confidence_source, review_status,
                             proposed_by, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, 'UNKNOWN', 'WO-13-CLUSTER',
                                'PROPOSED', %s, %s, %s)
                        """,
                        (
                            group_id, int(wc["id"]),
                            wc["salesforce_account_id"],
                            wc["canonical_company_id"],
                            SEED_PROPOSER_TAG, now, now,
                        ),
                    )
                inserted += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit(autocommit_before)
    return inserted


def write_review_pack_csv(seeded: list[SeededGroup], out_path: Path) -> None:
    """Operator-facing CSV. Columns ending in '_blank' are for the
    operator to fill in: `operator_decision`, `set_primary_sf_id`,
    `operator_note`. Everything else is system-emitted."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "cluster_id", "group_name", "group_name_normalised",
            "impact_score", "seed_warning", "skipped_reason",
            "member_count", "member_wip_ids", "member_sf_ids",
            "member_account_names", "canonical_ids", "distinct_cities",
            "suggested_primary_canonical_id",
            # Operator fields (blank — to be filled in)
            "operator_decision", "set_primary_sf_id", "operator_note",
        ])
        for s in seeded:
            w.writerow([
                s.cluster_id, s.group_name, s.group_name_normalised,
                f"{s.impact_score:.1f}", s.seed_warning or "",
                s.skipped_reason or "",
                len(s.member_wip_ids),
                " | ".join(str(x) for x in s.member_wip_ids),
                " | ".join(s.member_sf_ids),
                " | ".join(s.member_account_names),
                " | ".join(str(x) for x in s.member_canonical_ids),
                " | ".join(s.distinct_cities),
                s.suggested_primary_canonical_id or "",
                "", "", "",
            ])
