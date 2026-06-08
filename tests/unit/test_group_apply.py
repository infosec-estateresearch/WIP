"""WO-14A.1 — apply_decisions operator workflow.

Pins down the operator-decision branch behaviour: APPROVE_GROUP /
REJECT_GROUP / NEEDS_MORE_REVIEW; unknown decisions; idempotency
(re-applying the same CSV is a no-op); APPROVE without a way to pick
PRIMARY is refused.
"""
from __future__ import annotations

import csv
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.wip_app.groups.apply import apply_decisions


def _write_decisions_csv(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "decisions.csv"
    cols = [
        "cluster_id", "group_name", "group_name_normalised",
        "impact_score", "seed_warning", "skipped_reason",
        "member_count", "member_wip_ids", "member_sf_ids",
        "member_account_names", "canonical_ids", "distinct_cities",
        "suggested_primary_canonical_id",
        "operator_decision", "set_primary_sf_id", "operator_note",
    ]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            full = {c: "" for c in cols}
            full.update(r)
            w.writerow(full)
    return path


def _fake_conn(
    *, groups_by_cluster: dict = None, members_by_group_id: dict = None,
):
    """Minimal pymysql-shaped fake. groups_by_cluster keyed by cluster_id;
    members_by_group_id keyed by group_id → list of {id, salesforce_account_id}."""
    groups_by_cluster = groups_by_cluster or {}
    members_by_group_id = members_by_group_id or {}
    state = {
        "group_updates": [], "member_updates": [], "sticky_inserts": [],
        "autocommit": False, "commits": 0, "rollbacks": 0,
    }
    conn = MagicMock()

    def cursor():
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur._fetched = []
        cur._fetched_one = None
        def execute(sql, params=()):
            cur._fetched = []
            cur._fetched_one = None
            s = sql.strip().upper()
            if s.startswith("SELECT") and "FROM WIP_COMPANY_GROUP " in s + " " and "SOURCE_CLUSTER_ID" in s:
                row = groups_by_cluster.get(params[0])
                cur._fetched_one = row
            elif s.startswith("SELECT") and "FROM WIP_COMPANY_GROUP " in s + " " and "GROUP_NAME_NORMALISED" in s:
                # fallback lookup by name — return any with matching label
                cur._fetched_one = None
                for g in groups_by_cluster.values():
                    if g and g.get("group_name_normalised") == params[0]:
                        cur._fetched_one = g
                        break
            elif s.startswith("SELECT") and "FROM WIP_COMPANY_GROUP_MEMBER" in s:
                gid = int(params[0])
                cur._fetched = members_by_group_id.get(gid, [])
            elif s.startswith("UPDATE WIP_COMPANY_GROUP_MEMBER"):
                state["member_updates"].append({"sql": s, "params": params})
            elif s.startswith("UPDATE WIP_COMPANY_GROUP"):
                state["group_updates"].append({"sql": s, "params": params})
            elif s.startswith("INSERT INTO WIP_COMPANY_GROUP_SEED_REJECT"):
                state["sticky_inserts"].append(params)
        cur.execute.side_effect = execute
        cur.fetchall.side_effect = lambda: cur._fetched
        cur.fetchone.side_effect = lambda: cur._fetched_one
        return cur
    conn.cursor.side_effect = cursor
    conn.autocommit = MagicMock(side_effect=lambda v: state.__setitem__("autocommit", v))
    conn.get_autocommit = MagicMock(return_value=False)
    conn.commit = MagicMock(side_effect=lambda: state.__setitem__("commits", state["commits"] + 1))
    conn.rollback = MagicMock(side_effect=lambda: state.__setitem__("rollbacks", state["rollbacks"] + 1))
    conn.state = state
    return conn


def _proposed_group(cluster_id, label, gid=10):
    return {
        "id": gid,
        "group_name": label.title(),
        "group_name_normalised": label,
        "review_status": "PROPOSED",
        "source_cluster_id": cluster_id,
    }


def _members(gid, sf_ids):
    # WO-26: _approve_group now also reads canonical_company_id (to dedupe
    # shared-canonical members). Distinct values here → no dedup in these cases.
    return [{"id": gid * 100 + i, "salesforce_account_id": sf, "group_id": gid,
             "canonical_company_id": gid * 1000 + i}
            for i, sf in enumerate(sf_ids, 1)]


# ---------------------------------------------------------------------------
# Apply-mode positive paths
# ---------------------------------------------------------------------------

def test_approve_with_set_primary_sf_id(tmp_path):
    """The supported APPROVE path — operator picked PRIMARY explicitly.
    Group is promoted; member 002 becomes PRIMARY, others BRANCH."""
    csv_path = _write_decisions_csv(tmp_path, [
        {"cluster_id": "CC1", "group_name_normalised": "huge_firm",
         "operator_decision": "APPROVE_GROUP",
         "set_primary_sf_id": "001SF002",
         "operator_note": "primary=London office"},
    ])
    conn = _fake_conn(
        groups_by_cluster={"CC1": _proposed_group("CC1", "huge_firm", gid=10)},
        members_by_group_id={10: _members(10, ["001SF001", "001SF002", "001SF003"])},
    )
    result = apply_decisions(conn, csv_path, dry_run=False)
    assert result.groups_approved == 1
    assert result.rows_failed == 0
    assert conn.state["commits"] >= 1


def test_approve_single_member_no_set_primary_required(tmp_path):
    """A group with exactly one member: APPROVE picks the lone member
    as PRIMARY automatically — no need for set_primary_sf_id."""
    csv_path = _write_decisions_csv(tmp_path, [
        {"cluster_id": "CC2", "group_name_normalised": "only_one",
         "operator_decision": "APPROVE_GROUP"},
    ])
    conn = _fake_conn(
        groups_by_cluster={"CC2": _proposed_group("CC2", "only_one", gid=11)},
        members_by_group_id={11: _members(11, ["001SF001"])},
    )
    result = apply_decisions(conn, csv_path, dry_run=False)
    assert result.groups_approved == 1
    assert result.rows_failed == 0


def test_approve_multi_member_refuses_without_set_primary_sf_id(tmp_path):
    """APPROVE of a multi-member group without set_primary_sf_id must
    fail — the schema requires exactly one PRIMARY per APPROVED group,
    and the apply step refuses ambiguity rather than guessing."""
    csv_path = _write_decisions_csv(tmp_path, [
        {"cluster_id": "CC3", "group_name_normalised": "ambiguous",
         "operator_decision": "APPROVE_GROUP"},
    ])
    conn = _fake_conn(
        groups_by_cluster={"CC3": _proposed_group("CC3", "ambiguous", gid=12)},
        members_by_group_id={12: _members(12, ["001SF001", "001SF002"])},
    )
    result = apply_decisions(conn, csv_path, dry_run=False)
    assert result.groups_approved == 0
    assert result.rows_failed == 1
    assert "no set_primary_sf_id" in result.errors[0]


def test_approve_with_invalid_primary_sf_id_fails(tmp_path):
    """If set_primary_sf_id isn't a member of the group, the apply step
    refuses rather than silently choosing a different PRIMARY."""
    csv_path = _write_decisions_csv(tmp_path, [
        {"cluster_id": "CC4", "group_name_normalised": "bad_primary",
         "operator_decision": "APPROVE_GROUP",
         "set_primary_sf_id": "001NOTINTHISGROUP"},
    ])
    conn = _fake_conn(
        groups_by_cluster={"CC4": _proposed_group("CC4", "bad_primary", gid=13)},
        members_by_group_id={13: _members(13, ["001SF001", "001SF002"])},
    )
    result = apply_decisions(conn, csv_path, dry_run=False)
    assert result.groups_approved == 0
    assert result.rows_failed == 1
    assert "not a member" in result.errors[0]


def test_reject_writes_to_sticky_reject_set(tmp_path):
    """REJECT must update the group AND insert into seed_reject so the
    next seed run doesn't re-propose the same name."""
    csv_path = _write_decisions_csv(tmp_path, [
        {"cluster_id": "CC5", "group_name_normalised": "nope_firm",
         "operator_decision": "REJECT_GROUP",
         "operator_note": "two different firms with same name root"},
    ])
    conn = _fake_conn(
        groups_by_cluster={"CC5": _proposed_group("CC5", "nope_firm", gid=14)},
        members_by_group_id={14: _members(14, ["001SF001", "001SF002"])},
    )
    result = apply_decisions(conn, csv_path, dry_run=False)
    assert result.groups_rejected == 1
    assert result.rows_failed == 0
    assert len(conn.state["sticky_inserts"]) == 1
    # The sticky-insert params start with (normalised, rejected_by, ts, reason, ...)
    assert conn.state["sticky_inserts"][0][0] == "nope_firm"


def test_needs_more_review_does_not_touch_members(tmp_path):
    """Flipping the group to NEEDS_REVIEW must not change member state —
    the operator is buying time, not making a decision yet."""
    csv_path = _write_decisions_csv(tmp_path, [
        {"cluster_id": "CC6", "group_name_normalised": "later",
         "operator_decision": "NEEDS_MORE_REVIEW",
         "operator_note": "ask commercial team"},
    ])
    conn = _fake_conn(
        groups_by_cluster={"CC6": _proposed_group("CC6", "later", gid=15)},
        members_by_group_id={15: _members(15, ["001SF001"])},
    )
    result = apply_decisions(conn, csv_path, dry_run=False)
    assert result.groups_needs_review == 1
    assert len(conn.state["member_updates"]) == 0


# ---------------------------------------------------------------------------
# Defensive paths
# ---------------------------------------------------------------------------

def test_unknown_decision_is_reported_and_skipped(tmp_path):
    """SPLIT_GROUP and MERGE_INTO are deferred per WO-14B; if an operator
    types them, the apply step must surface the mistake clearly, not act
    on it silently."""
    csv_path = _write_decisions_csv(tmp_path, [
        {"cluster_id": "CC7", "group_name_normalised": "splitme",
         "operator_decision": "SPLIT_GROUP"},
    ])
    conn = _fake_conn(
        groups_by_cluster={"CC7": _proposed_group("CC7", "splitme")},
        members_by_group_id={10: _members(10, ["001SF001"])},
    )
    result = apply_decisions(conn, csv_path, dry_run=False)
    assert result.rows_skipped == 1
    assert result.groups_approved == 0
    assert "SPLIT_GROUP" in result.decisions_unknown[0]


def test_empty_decision_rows_are_ignored(tmp_path):
    """The review CSV contains every PROPOSED group; the operator only
    fills in the rows they decided on. Empty decision rows must be
    treated as 'no input yet', not as errors."""
    csv_path = _write_decisions_csv(tmp_path, [
        {"cluster_id": "CC8a", "group_name_normalised": "a",
         "operator_decision": ""},
        {"cluster_id": "CC8b", "group_name_normalised": "b",
         "operator_decision": "APPROVE_GROUP",
         "set_primary_sf_id": "001SF001"},
    ])
    conn = _fake_conn(
        groups_by_cluster={"CC8b": _proposed_group("CC8b", "b", gid=20)},
        members_by_group_id={20: _members(20, ["001SF001"])},
    )
    result = apply_decisions(conn, csv_path, dry_run=False)
    # Only the APPROVE_GROUP row counts as "seen" — empty decisions skip
    # during CSV parsing and don't appear in rows_seen.
    assert result.rows_seen == 1
    assert result.groups_approved == 1


def test_missing_group_row_reports_error(tmp_path):
    """If the seed never created the group (cluster_id unknown to the
    DB), the apply step must report rather than silently succeed."""
    csv_path = _write_decisions_csv(tmp_path, [
        {"cluster_id": "CC_UNKNOWN", "group_name_normalised": "ghost",
         "operator_decision": "APPROVE_GROUP",
         "set_primary_sf_id": "001SF001"},
    ])
    conn = _fake_conn(groups_by_cluster={})  # nothing
    result = apply_decisions(conn, csv_path, dry_run=False)
    assert result.rows_failed == 1
    assert "no group found" in result.errors[0]


def test_dry_run_rolls_back_each_row(tmp_path):
    """In --dry-run mode, every per-row transaction must rollback so the
    DB is unchanged after the run, but the per-row counters still report
    what would have happened."""
    csv_path = _write_decisions_csv(tmp_path, [
        {"cluster_id": "CC9", "group_name_normalised": "preview",
         "operator_decision": "APPROVE_GROUP",
         "set_primary_sf_id": "001SF001"},
    ])
    conn = _fake_conn(
        groups_by_cluster={"CC9": _proposed_group("CC9", "preview", gid=30)},
        members_by_group_id={30: _members(30, ["001SF001"])},
    )
    result = apply_decisions(conn, csv_path, dry_run=True)
    assert result.groups_approved == 1
    # Dry-run rolled back rather than committed
    assert conn.state["rollbacks"] >= 1


def test_idempotent_already_approved_skips(tmp_path):
    """Re-running an applied CSV must not double-promote; the row
    becomes a skip, not a failure."""
    csv_path = _write_decisions_csv(tmp_path, [
        {"cluster_id": "CC10", "group_name_normalised": "done",
         "operator_decision": "APPROVE_GROUP",
         "set_primary_sf_id": "001SF001"},
    ])
    already_approved = _proposed_group("CC10", "done", gid=40)
    already_approved["review_status"] = "APPROVED"
    conn = _fake_conn(
        groups_by_cluster={"CC10": already_approved},
        members_by_group_id={40: _members(40, ["001SF001"])},
    )
    result = apply_decisions(conn, csv_path, dry_run=False)
    assert result.groups_approved == 0
    assert result.rows_skipped == 1
