"""WO-14A.1 — propose_groups filtering rules.

The seed proposer reads a WO-13 candidates CSV and emits PROPOSED group
entries, skipping clusters that the architect's rules say should be
ignored. These tests pin down each skip path so a future change can't
silently re-propose what an operator already declined.
"""
from __future__ import annotations

import csv
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.wip_app.groups.seed import (
    CATEGORY_FRAGMENTATION,
    MIN_IMPACT_SCORE,
    propose_groups,
)


def _write_candidates_csv(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "candidates.csv"
    cols = [
        "cluster_id", "category", "group_label",
        "wip_company_count", "salesforce_account_ids", "account_names",
        "canonical_company_count", "canonical_company_ids", "canonical_names",
        "distinct_cities", "distinct_postcodes_count",
        "distinct_account_types",
        "total_current_wip_gbp", "active_case_count",
        "webinar_attendees", "import_reject_count",
        "impact_score", "recommendation", "evidence_notes",
    ]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            full = {c: "" for c in cols}
            full.update(r)
            w.writerow(full)
    return path


def _row(
    *, cluster_id="CC-FRAG-test", group_label="testfirm",
    category=CATEGORY_FRAGMENTATION, impact=50.0,
    sf_ids=("001A", "001B"), canonical_ids=(100, 101),
    names=("Test Firm A", "Test Firm B"), cities=("London", "Bristol"),
    account_types=("Tier 1",),
):
    return {
        "cluster_id": cluster_id,
        "category": category,
        "group_label": group_label,
        "salesforce_account_ids": " | ".join(sf_ids),
        "account_names": " | ".join(names),
        "canonical_company_ids": " | ".join(str(x) for x in canonical_ids),
        "canonical_company_count": str(len(canonical_ids)),
        "distinct_cities": " | ".join(cities),
        "distinct_account_types": " | ".join(account_types),
        "impact_score": str(impact),
    }


def _fake_conn(
    approved_wip_ids: set[int] = None,
    sticky_rejects: set[str] = None,
    already_proposed: set[str] = None,
    wip_by_sf: dict = None,
):
    """Stand-in for a pymysql conn. The seed code's queries follow a
    fixed pattern — we route by SQL substring."""
    approved_wip_ids = approved_wip_ids or set()
    sticky_rejects = sticky_rejects or set()
    already_proposed = already_proposed or set()
    wip_by_sf = wip_by_sf or {}

    conn = MagicMock()

    def cursor():
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur._fetched = []
        def execute(sql, params=()):
            cur._fetched = []
            s = sql.strip().upper()
            if "FROM WIP_COMPANY_GROUP_MEMBER" in s and "APPROVED" in s and "WIP_COMPANY_ID" in s:
                cur._fetched = [{"wip_company_id": wid} for wid in approved_wip_ids]
            elif "FROM WIP_COMPANY_GROUP_SEED_REJECT" in s:
                cur._fetched = [{"group_name_normalised": n} for n in sticky_rejects]
            elif "FROM WIP_COMPANY_GROUP" in s and "PROPOSED" in s:
                cur._fetched = [{"group_name_normalised": n} for n in already_proposed]
            elif "FROM WIP_COMPANY WHERE SALESFORCE_ACCOUNT_ID IN" in s:
                # Per-cluster lookup — return rows for the SF ids in params
                cur._fetched = [
                    wip_by_sf[sf] for sf in params if sf in wip_by_sf
                ]
        cur.execute.side_effect = execute
        cur.fetchall.side_effect = lambda: cur._fetched
        return cur
    conn.cursor.side_effect = cursor
    return conn


# ---------------------------------------------------------------------------
# Filter rules
# ---------------------------------------------------------------------------

def test_only_canonical_fragmentation_category_is_seeded(tmp_path):
    """Architect rule: seed only CANONICAL_FRAGMENTATION clusters. The
    UNMAPPED_SF_ACCOUNT and NEEDS_SALESFORCE_MERGE buckets need different
    handling and are out of scope for WO-14A.1."""
    path = _write_candidates_csv(tmp_path, [
        _row(group_label="kept_frag", category="CANONICAL_FRAGMENTATION"),
        _row(group_label="skipped_merge", category="NEEDS_SALESFORCE_MERGE"),
        _row(group_label="skipped_unmapped", category="UNMAPPED_SF_ACCOUNT"),
    ])
    result = propose_groups(
        _fake_conn(wip_by_sf={
            "001A": {"id": 1, "salesforce_account_id": "001A", "canonical_company_id": 100},
            "001B": {"id": 2, "salesforce_account_id": "001B", "canonical_company_id": 101},
        }),
        path,
    )
    labels = [s.group_name_normalised for s in result]
    assert labels == ["kept_frag"]


def test_below_impact_threshold_is_skipped(tmp_path):
    """impact_score 10 < threshold 20 → cluster does not appear in the
    seed output at all (not even as a skipped row — it never gets
    considered)."""
    path = _write_candidates_csv(tmp_path, [
        _row(group_label="too_small", impact=10.0),
        _row(group_label="big_enough", impact=50.0),
    ])
    result = propose_groups(
        _fake_conn(wip_by_sf={
            "001A": {"id": 1, "salesforce_account_id": "001A", "canonical_company_id": 100},
            "001B": {"id": 2, "salesforce_account_id": "001B", "canonical_company_id": 101},
        }),
        path,
    )
    assert [s.group_name_normalised for s in result] == ["big_enough"]


def test_sticky_rejected_name_is_skipped_with_reason(tmp_path):
    """A name in wip_company_group_seed_reject must yield a SeededGroup
    with skipped_reason='sticky_rejected' so dry-run output shows it,
    AND the entry must NOT be re-INSERTed by apply mode."""
    path = _write_candidates_csv(tmp_path, [
        _row(group_label="alreadysaid_no"),
    ])
    result = propose_groups(
        _fake_conn(sticky_rejects={"alreadysaid_no"}, wip_by_sf={
            "001A": {"id": 1, "salesforce_account_id": "001A", "canonical_company_id": 100},
            "001B": {"id": 2, "salesforce_account_id": "001B", "canonical_company_id": 101},
        }),
        path,
    )
    assert len(result) == 1
    assert result[0].skipped_reason == "sticky_rejected"


def test_already_approved_member_skips_cluster(tmp_path):
    """Idempotency: if any member wip_company is in an APPROVED group,
    skip the whole cluster (the operator owns it)."""
    path = _write_candidates_csv(tmp_path, [
        _row(group_label="owned_firm",
             sf_ids=("001A", "001B"),
             canonical_ids=(100, 101)),
    ])
    result = propose_groups(
        _fake_conn(approved_wip_ids={1}, wip_by_sf={
            "001A": {"id": 1, "salesforce_account_id": "001A", "canonical_company_id": 100},
            "001B": {"id": 2, "salesforce_account_id": "001B", "canonical_company_id": 101},
        }),
        path,
    )
    assert len(result) == 1
    assert result[0].skipped_reason == "member_already_in_approved_group"


def test_already_proposed_normalised_name_is_skipped(tmp_path):
    """Re-running the seed shouldn't pile up duplicate PROPOSED entries
    for the same firm — that would flood the review CSV."""
    path = _write_candidates_csv(tmp_path, [
        _row(group_label="alreadyqueued"),
    ])
    result = propose_groups(
        _fake_conn(already_proposed={"alreadyqueued"}, wip_by_sf={
            "001A": {"id": 1, "salesforce_account_id": "001A", "canonical_company_id": 100},
            "001B": {"id": 2, "salesforce_account_id": "001B", "canonical_company_id": 101},
        }),
        path,
    )
    assert len(result) == 1
    assert result[0].skipped_reason == "already_proposed_or_needs_review"


# ---------------------------------------------------------------------------
# seed_warning surfacing (WO-14B §7 rule 3 + rule 4)
# ---------------------------------------------------------------------------

def test_mixed_account_types_sets_warning(tmp_path):
    """Mixed account_types is a false-positive smell — operator review
    must see the warning, AND the apply step routes those to
    group_type=UNKNOWN (tested via the insert path)."""
    path = _write_candidates_csv(tmp_path, [
        _row(group_label="mixedtypes",
             account_types=("Tier 1 Solicitor", "Council")),
    ])
    result = propose_groups(
        _fake_conn(wip_by_sf={
            "001A": {"id": 1, "salesforce_account_id": "001A", "canonical_company_id": 100},
            "001B": {"id": 2, "salesforce_account_id": "001B", "canonical_company_id": 101},
        }),
        path,
    )
    assert len(result) == 1
    assert result[0].seed_warning == "mixed_account_types"


def test_wide_geographic_split_sets_warning(tmp_path):
    """6+ cities tips into split-by-default territory — operator must
    explicitly opt back into a single group."""
    path = _write_candidates_csv(tmp_path, [
        _row(group_label="widegeo",
             cities=("London", "Bristol", "Cardiff", "Manchester",
                     "Edinburgh", "Glasgow")),
    ])
    result = propose_groups(
        _fake_conn(wip_by_sf={
            "001A": {"id": 1, "salesforce_account_id": "001A", "canonical_company_id": 100},
            "001B": {"id": 2, "salesforce_account_id": "001B", "canonical_company_id": 101},
        }),
        path,
    )
    assert len(result) == 1
    assert result[0].seed_warning == "wide_geographic_split"


def test_default_no_warning(tmp_path):
    """Plain canonical-fragmentation cluster with one account_type and
    a normal number of cities → no seed warning."""
    path = _write_candidates_csv(tmp_path, [
        _row(group_label="normal",
             account_types=("Tier 1",), cities=("London", "Bristol")),
    ])
    result = propose_groups(
        _fake_conn(wip_by_sf={
            "001A": {"id": 1, "salesforce_account_id": "001A", "canonical_company_id": 100},
            "001B": {"id": 2, "salesforce_account_id": "001B", "canonical_company_id": 101},
        }),
        path,
    )
    assert len(result) == 1
    assert result[0].seed_warning is None


def test_suggested_primary_canonical_picks_lowest_id(tmp_path):
    """Per WO-14B §6.2 — lowest canonical_company_id wins as the seed's
    suggestion. Operator overrides via set_primary_sf_id."""
    path = _write_candidates_csv(tmp_path, [
        _row(group_label="testfirm", canonical_ids=(20425, 13030, 23743)),
    ])
    result = propose_groups(
        _fake_conn(wip_by_sf={
            "001A": {"id": 1, "salesforce_account_id": "001A", "canonical_company_id": 13030},
            "001B": {"id": 2, "salesforce_account_id": "001B", "canonical_company_id": 20425},
        }),
        path,
    )
    assert result[0].suggested_primary_canonical_id == 13030
