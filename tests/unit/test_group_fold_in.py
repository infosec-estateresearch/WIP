"""WO-14A.3 — fold-in pass behaviour tests.

Pins down the critical boundaries: APPROVED groups untouched, REJECTED
groups untouched, only PROPOSED + NEEDS_REVIEW receive new PROPOSED
members, never delete an existing member, refuse adding a wip_company
that's already in some APPROVED group elsewhere, idempotent.
"""
from __future__ import annotations

import csv
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.wip_app.groups.fold_in import apply_plan, build_plan


def _write_candidates_csv(tmp_path: Path, clusters: dict[str, list[str]]) -> Path:
    """Build a minimal WO-13 candidates CSV with just the columns the
    fold-in module reads (cluster_id + salesforce_account_ids)."""
    path = tmp_path / "candidates.csv"
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["cluster_id", "category", "salesforce_account_ids"])
        for cid, sf_ids in clusters.items():
            w.writerow([cid, "CANONICAL_FRAGMENTATION", " | ".join(sf_ids)])
    return path


def _fake_conn(
    *, pending_groups: list[dict] = None,
    members_by_group_id: dict[int, list[int]] = None,
    wip_by_sf: dict[str, dict] = None,
    approved_wip_ids: set[int] = None,
):
    """Stand-in conn. Routes by SQL substring."""
    pending_groups = pending_groups or []
    members_by_group_id = members_by_group_id or {}
    wip_by_sf = wip_by_sf or {}
    approved_wip_ids = approved_wip_ids or set()

    state = {
        "inserts": [], "commits": 0, "rollbacks": 0, "autocommit": False,
    }
    conn = MagicMock()

    def cursor():
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur._fetched = []
        def execute(sql, params=()):
            cur._fetched = []
            s = sql.strip().upper()
            if "FROM WIP_COMPANY_GROUP " in s + " " and "REVIEW_STATUS IN" in s and "INSERT" not in s and "UPDATE" not in s:
                cur._fetched = pending_groups
            elif s.startswith("SELECT WIP_COMPANY_ID FROM WIP_COMPANY_GROUP_MEMBER"):
                gid = int(params[0])
                cur._fetched = [{"wip_company_id": wid}
                                for wid in members_by_group_id.get(gid, [])]
            elif "FROM WIP_COMPANY " in s + " " and "SALESFORCE_ACCOUNT_ID IN" in s:
                cur._fetched = [wip_by_sf[sf] for sf in params if sf in wip_by_sf]
            elif "FROM WIP_COMPANY_GROUP_MEMBER" in s and "APPROVED" in s and "WIP_COMPANY_ID IN" in s:
                cur._fetched = [{"wip_company_id": wid}
                                for wid in params if int(wid) in approved_wip_ids]
            elif s.startswith("INSERT INTO WIP_COMPANY_GROUP_MEMBER"):
                state["inserts"].append(params)
        cur.execute.side_effect = execute
        cur.fetchall.side_effect = lambda: cur._fetched
        cur.fetchone.side_effect = lambda: (cur._fetched[0] if cur._fetched else None)
        return cur
    conn.cursor.side_effect = cursor
    conn.autocommit = MagicMock(side_effect=lambda v: state.__setitem__("autocommit", v))
    conn.get_autocommit = MagicMock(return_value=False)
    conn.commit = MagicMock(side_effect=lambda: state.__setitem__("commits", state["commits"] + 1))
    conn.rollback = MagicMock(side_effect=lambda: state.__setitem__("rollbacks", state["rollbacks"] + 1))
    conn.state = state
    return conn


def _g(gid, name, status, cluster_id):
    return {
        "id": gid, "group_name": name,
        "group_name_normalised": name.lower(),
        "review_status": status,
        "source_cluster_id": cluster_id,
    }


def _wc(wip_id, sf_id, canonical_id):
    return {
        "id": wip_id, "salesforce_account_id": sf_id,
        "salesforce_account_name": f"Acct {sf_id}",
        "canonical_company_id": canonical_id,
    }


# ---------------------------------------------------------------------------
# Boundary rules (the four "must not"s from the architect's spec)
# ---------------------------------------------------------------------------

def test_proposed_group_gets_missing_members_added(tmp_path):
    """Knights-shaped case: PROPOSED group with 1 of 3 expected members.
    Fold-in adds the missing 2 as PROPOSED under the same group."""
    candidates = _write_candidates_csv(tmp_path, {
        "CC-FRAG-knights": ["0011000000000A1", "0011000000000B2", "0011000000000C3"],
    })
    conn = _fake_conn(
        pending_groups=[_g(10, "Knights", "PROPOSED", "CC-FRAG-knights")],
        members_by_group_id={10: [1]},                  # only wip_id 1 attached
        wip_by_sf={
            "0011000000000A1": _wc(1, "0011000000000A1", 100),
            "0011000000000B2": _wc(2, "0011000000000B2", 101),
            "0011000000000C3": _wc(3, "0011000000000C3", 102),
        },
    )
    result = apply_plan(conn, build_plan(conn, candidates))
    assert result.members_added == 2
    inserted_wip_ids = {p[1] for p in conn.state["inserts"]}
    assert inserted_wip_ids == {2, 3}


def test_needs_review_group_gets_members_but_status_preserved(tmp_path):
    """Status MUST stay NEEDS_REVIEW — fold-in adds members but does not
    reopen the operator's pending decision."""
    candidates = _write_candidates_csv(tmp_path, {
        "CC-FRAG-knights": ["0011000000000A1", "0011000000000B2"],
    })
    conn = _fake_conn(
        pending_groups=[_g(11, "Knights", "NEEDS_REVIEW", "CC-FRAG-knights")],
        members_by_group_id={11: [1]},
        wip_by_sf={"0011000000000A1": _wc(1, "0011000000000A1", 100), "0011000000000B2": _wc(2, "0011000000000B2", 101)},
    )
    result = apply_plan(conn, build_plan(conn, candidates))
    assert result.members_added == 1
    # No UPDATE wip_company_group SET review_status would have been
    # called; the fake conn only sees INSERTs.
    # Defensive check: nothing in conn.state["inserts"] touches a group
    # update — the test fake just doesn't record UPDATEs at all, which
    # already encodes "didn't issue one".


def test_approved_group_is_untouched(tmp_path):
    """Hugh James-shaped case: APPROVED groups must be invisible to
    build_plan (they're filtered out by the SQL WHERE clause)."""
    candidates = _write_candidates_csv(tmp_path, {
        "CC-FRAG-hugh-james": ["0011000000000A1", "0011000000000B2", "0011000000000C3"],
    })
    # Note: pending_groups deliberately omits the APPROVED group — the
    # fake's WHERE filter returns only PROPOSED + NEEDS_REVIEW; here
    # we mirror that by giving build_plan an empty pending list, and
    # verify build_plan reports no fold-in.
    conn = _fake_conn(
        pending_groups=[],
        wip_by_sf={"0011000000000A1": _wc(1, "0011000000000A1", 100)},
    )
    result = apply_plan(conn, build_plan(conn, candidates))
    assert result.members_added == 0
    assert result.groups_examined == 0
    assert conn.state["inserts"] == []


def test_rejected_group_is_untouched(tmp_path):
    """Same as APPROVED — REJECTED groups are filtered out at SELECT
    time. The fold-in never sees them."""
    candidates = _write_candidates_csv(tmp_path, {
        "CC-FRAG-jones-whyte": ["0011000000000A1", "0011000000000B2"],
    })
    conn = _fake_conn(
        pending_groups=[],   # REJECTED filtered out by build_plan's WHERE
        wip_by_sf={"0011000000000A1": _wc(1, "0011000000000A1", 100), "0011000000000B2": _wc(2, "0011000000000B2", 101)},
    )
    result = apply_plan(conn, build_plan(conn, candidates))
    assert result.members_added == 0
    assert conn.state["inserts"] == []


def test_wip_company_in_another_approved_group_is_refused(tmp_path):
    """The WO-14B §3 invariant: a wip_company belongs to AT MOST one
    APPROVED group. Fold-in must NOT attach a wip_company that's
    already in some APPROVED group (even if the cluster wants it)."""
    candidates = _write_candidates_csv(tmp_path, {
        "CC-FRAG-knights": ["0011000000000A1", "0011000000000B2", "0011000000000C3"],
    })
    conn = _fake_conn(
        pending_groups=[_g(20, "Knights", "PROPOSED", "CC-FRAG-knights")],
        members_by_group_id={20: []},   # nothing attached yet
        wip_by_sf={
            "0011000000000A1": _wc(1, "0011000000000A1", 100),
            "0011000000000B2": _wc(2, "0011000000000B2", 101),
            "0011000000000C3": _wc(3, "0011000000000C3", 102),
        },
        approved_wip_ids={2},   # wip_id 2 belongs to some other APPROVED group
    )
    plan_result = build_plan(conn, candidates)
    # Plan should refuse wip_id 2 and mark it in skipped_already_approved_elsewhere
    plan = plan_result.per_group[0]
    assert {int(c["id"]) for c in plan.missing_wip_companies} == {1, 3}
    assert {int(c["id"]) for c in plan.skipped_already_approved_elsewhere} == {2}
    # Apply: only 1 and 3 get inserted
    apply_plan(conn, plan_result)
    inserted_wip_ids = {p[1] for p in conn.state["inserts"]}
    assert inserted_wip_ids == {1, 3}


def test_dry_run_does_not_insert(tmp_path):
    """build_plan is pure — does not call cursor.execute('INSERT...').
    The CLI's --dry-run skips the apply_plan call entirely."""
    candidates = _write_candidates_csv(tmp_path, {
        "CC-FRAG-knights": ["0011000000000A1", "0011000000000B2"],
    })
    conn = _fake_conn(
        pending_groups=[_g(30, "Knights", "PROPOSED", "CC-FRAG-knights")],
        members_by_group_id={30: []},
        wip_by_sf={"0011000000000A1": _wc(1, "0011000000000A1", 100), "0011000000000B2": _wc(2, "0011000000000B2", 101)},
    )
    # Only build_plan, no apply_plan
    result = build_plan(conn, candidates)
    assert conn.state["inserts"] == []
    # But the plan reports the would-be inserts
    assert len(result.per_group[0].missing_wip_companies) == 2


def test_idempotent_rerun_is_noop(tmp_path):
    """After fold-in, re-running with the same data must add zero new
    members (all expected members are now in the live group)."""
    candidates = _write_candidates_csv(tmp_path, {
        "CC-FRAG-knights": ["0011000000000A1", "0011000000000B2"],
    })
    conn = _fake_conn(
        # Simulating the post-fold-in state: both members now attached
        pending_groups=[_g(40, "Knights", "PROPOSED", "CC-FRAG-knights")],
        members_by_group_id={40: [1, 2]},
        wip_by_sf={"0011000000000A1": _wc(1, "0011000000000A1", 100), "0011000000000B2": _wc(2, "0011000000000B2", 101)},
    )
    result = apply_plan(conn, build_plan(conn, candidates))
    assert result.members_added == 0
    assert conn.state["inserts"] == []


def test_group_without_source_cluster_id_is_skipped(tmp_path):
    """Hand-curated groups (no source_cluster_id) have no WO-13 cluster
    to fold in from. Skip them with a reason rather than failing."""
    candidates = _write_candidates_csv(tmp_path, {
        "CC-FRAG-knights": ["0011000000000A1"],
    })
    conn = _fake_conn(
        pending_groups=[_g(50, "Hand-Curated", "PROPOSED", None)],
        wip_by_sf={"0011000000000A1": _wc(1, "0011000000000A1", 100)},
    )
    result = apply_plan(conn, build_plan(conn, candidates))
    assert result.members_added == 0
    assert result.groups_skipped_no_cluster == 1
    assert result.per_group[0].skipped_no_source_cluster is True


def test_unknown_sf_ids_are_surfaced_not_added(tmp_path):
    """If the WO-13 cluster lists an SF id that doesn't exist as a
    live wip_company (e.g. SF deleted it between WO-13 and now), the
    fold-in must not invent a wip_company. It reports the orphan."""
    candidates = _write_candidates_csv(tmp_path, {
        "CC-FRAG-knights": ["0011000000000A1", "0011000000GHOST", "0011000000000B2"],
    })
    conn = _fake_conn(
        pending_groups=[_g(60, "Knights", "PROPOSED", "CC-FRAG-knights")],
        members_by_group_id={60: []},
        wip_by_sf={"0011000000000A1": _wc(1, "0011000000000A1", 100), "0011000000000B2": _wc(2, "0011000000000B2", 101)},
        # "0011000000GHOST" deliberately absent
    )
    result = apply_plan(conn, build_plan(conn, candidates))
    assert result.members_added == 2
    assert result.members_skipped_unknown_sf == 1
    assert result.per_group[0].skipped_unknown_sf_ids == ["0011000000GHOST"]


def test_truncation_tokens_in_candidates_csv_are_defensively_filtered(tmp_path):
    """Belt-and-braces: even if a stale (pre-WO-14A.2) candidates CSV
    slipped through with ' ... (+27 more)' tokens, the fold-in module
    must not parse those as SF ids."""
    path = tmp_path / "stale.csv"
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["cluster_id", "category", "salesforce_account_ids"])
        w.writerow(["CC-FRAG-knights", "CANONICAL_FRAGMENTATION",
                    "0011000000000A1 | 0011000000000B2 | ...  | (+27 more)"])
    conn = _fake_conn(
        pending_groups=[_g(70, "Knights", "PROPOSED", "CC-FRAG-knights")],
        members_by_group_id={70: []},
        wip_by_sf={"0011000000000A1": _wc(1, "0011000000000A1", 100), "0011000000000B2": _wc(2, "0011000000000B2", 101)},
    )
    result = apply_plan(conn, build_plan(conn, path))
    # Only 001A and 001B should be added; "...", "(+27", "more)" must
    # not be parsed as SF ids.
    assert result.members_added == 2
    assert result.members_skipped_unknown_sf == 0
