from __future__ import annotations

"""Duplicate-detector tests — uses a fake pymysql-shape connection so we
don't need a real DB.
"""

import json
from collections import defaultdict
from datetime import datetime, timezone
from unittest.mock import MagicMock

from src.wip_app.sync.duplicate_detector import detect_and_upsert_duplicates


def _make_conn(active_rows: list[dict], candidate_rows: list[dict] | None = None):
    """Fake conn that returns the configured active rows on SELECT and tracks
    every INSERT/UPDATE. Tests inspect conn.executed.
    """
    conn = MagicMock()
    candidate_store: dict[str, dict] = {
        c["duplicate_group_key"]: c for c in (candidate_rows or [])
    }
    state = {"flag_resets": 0, "flag_sets": 0, "candidate_inserts": [], "candidate_updates": []}

    cursor_idx = {"i": 0}

    def cursor():
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)

        def execute(sql, params=()):
            s = sql.strip().upper()
            if s.startswith("SELECT ID, SALESFORCE_ACCOUNT_ID"):
                cur._fetched = active_rows
            elif s.startswith("UPDATE WIP_COMPANY SET IS_POTENTIAL_DUPLICATE = 0"):
                state["flag_resets"] += 1
            elif s.startswith("UPDATE WIP_COMPANY SET IS_POTENTIAL_DUPLICATE = 1"):
                state["flag_sets"] += 1
            elif s.startswith("SELECT ID FROM WIP_COMPANY_DUPLICATE_CANDIDATE"):
                key = params[0]
                cur._fetched_one = (
                    {"id": candidate_store[key]["id"]} if key in candidate_store else None
                )
            elif s.startswith("INSERT INTO WIP_COMPANY_DUPLICATE_CANDIDATE"):
                state["candidate_inserts"].append(params)
            elif s.startswith("UPDATE WIP_COMPANY_DUPLICATE_CANDIDATE"):
                state["candidate_updates"].append(params)
        cur.execute.side_effect = execute
        cur.fetchall.side_effect = lambda: getattr(cur, "_fetched", [])
        cur.fetchone.side_effect = lambda: getattr(cur, "_fetched_one", None)
        return cur

    conn.cursor.side_effect = cursor
    conn.state = state
    return conn


def _row(id_, sf_id, name, key):
    return {
        "id": id_, "salesforce_account_id": sf_id, "salesforce_account_name": name,
        "account_type": "Tier 1", "industry": "Solicitor", "owner_name": "AN Other",
        "billing_postcode": "AB1 2CD", "duplicate_group_key": key,
        "last_seen_in_salesforce_at": datetime(2026, 5, 28, tzinfo=timezone.utc),
    }


def test_no_duplicates_no_candidates_written():
    """All unique group keys — no flags raised, no candidate rows written."""
    rows = [
        _row(1, "001A", "Hugh James", "hugh james"),
        _row(2, "001B", "WBW",        "wbw"),
        _row(3, "001C", "Birketts",   "birketts"),
    ]
    conn = _make_conn(rows)
    result = detect_and_upsert_duplicates(conn, sync_run_id=10)
    assert result.groups_active == 0
    assert result.rows_flagged == 0
    assert conn.state["flag_sets"] == 0
    assert conn.state["candidate_inserts"] == []
    # Flag-reset always runs once (defensive — clears stale flags)
    assert conn.state["flag_resets"] == 1


def test_three_way_duplicate_group_flags_all_three_and_inserts_one_candidate():
    """The architect's headline example: Hugh James / Hugh James Solicitors /
    Hugh James LLP — three SF Accounts, one normalised key, one candidate row.
    """
    rows = [
        _row(1, "001A", "Hugh James",            "hugh james"),
        _row(2, "001B", "Hugh James Solicitors", "hugh james"),
        _row(3, "001C", "Hugh James LLP",        "hugh james"),
        _row(4, "001D", "Birketts",              "birketts"),
    ]
    conn = _make_conn(rows)
    result = detect_and_upsert_duplicates(conn, sync_run_id=11)
    assert result.groups_active == 1
    assert result.rows_flagged == 3
    assert result.candidates_inserted == 1
    assert result.candidates_updated == 0
    # The candidate INSERT carried the three SF IDs
    assert len(conn.state["candidate_inserts"]) == 1
    params = conn.state["candidate_inserts"][0]
    sf_ids_json = params[2]
    names_json = params[3]
    assert set(json.loads(sf_ids_json)) == {"001A", "001B", "001C"}
    assert set(json.loads(names_json)) == {
        "Hugh James", "Hugh James Solicitors", "Hugh James LLP",
    }


def test_known_duplicate_group_re_seen_updates_existing_candidate():
    """If the candidate row already exists, it's UPDATEd (count+metadata
    refreshed, operator status preserved), not re-INSERTed.
    """
    rows = [
        _row(1, "001A", "Hugh James",            "hugh james"),
        _row(2, "001B", "Hugh James Solicitors", "hugh james"),
    ]
    existing = [{"id": 999, "duplicate_group_key": "hugh james"}]
    conn = _make_conn(rows, candidate_rows=existing)
    result = detect_and_upsert_duplicates(conn, sync_run_id=12)
    assert result.groups_active == 1
    assert result.candidates_inserted == 0
    assert result.candidates_updated == 1


def test_flag_is_reset_each_run():
    """Defensive — every run starts by clearing is_potential_duplicate=0, so
    a resolved-in-Salesforce duplicate that's no longer a group stops being
    flagged on the next sync.
    """
    rows = [_row(1, "001A", "Hugh James", "hugh james")]
    conn = _make_conn(rows)
    detect_and_upsert_duplicates(conn, sync_run_id=13)
    assert conn.state["flag_resets"] == 1


def test_empty_active_set_safe():
    conn = _make_conn([])
    result = detect_and_upsert_duplicates(conn, sync_run_id=14)
    assert result.groups_active == 0
    assert result.rows_flagged == 0
