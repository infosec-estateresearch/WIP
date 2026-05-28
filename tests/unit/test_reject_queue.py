"""Reject queue write + resolve tests."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from src.wip_app.imports.reject_queue import log_reject, mark_resolved_for


def _conn():
    conn = MagicMock()
    state = {"inserts": [], "updates": [], "update_rowcount": 0}

    def cursor():
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        def execute(sql, params=()):
            s = sql.strip().upper()
            if s.startswith("INSERT INTO WIP_IMPORT_REJECT"):
                state["inserts"].append(params)
                cur.lastrowid = len(state["inserts"])
            elif s.startswith("UPDATE WIP_IMPORT_REJECT"):
                state["updates"].append(params)
                cur.rowcount = state["update_rowcount"]
        cur.execute.side_effect = execute
        return cur
    conn.cursor.side_effect = cursor
    conn.state = state
    return conn


def test_log_reject_writes_one_row_with_json_payload():
    conn = _conn()
    rid = log_reject(
        conn,
        import_batch_id=10,
        source_file="MASTERCOPY.xlsx", source_sheet="May 2026", source_row_number=42,
        firm_name_raw="Hugh James Solicitors",
        firm_name_normalised="hugh james",
        case_reference_raw="50001",
        reject_reason="NO_COMPANY_MATCH",
        row_json={"firm_name": "Hugh James Solicitors", "wip": 1500.50},
    )
    assert rid == 1
    assert len(conn.state["inserts"]) == 1
    params = conn.state["inserts"][0]
    # row_json is the 11th param (0-indexed: import_batch_id, source_file, source_sheet,
    # source_row_number, firm_name_raw, firm_name_normalised, case_reference_raw,
    # reject_reason, candidate_ids_json, candidate_names_json, row_json)
    row_json = json.loads(params[10])
    assert row_json["firm_name"] == "Hugh James Solicitors"
    assert row_json["wip"] == 1500.50  # decimal-safe via str fallback


def test_log_reject_rejects_unknown_reason():
    conn = _conn()
    with pytest.raises(ValueError):
        log_reject(
            conn, import_batch_id=1, source_file="x", source_sheet="x",
            source_row_number=1, firm_name_raw="x", firm_name_normalised="x",
            case_reference_raw="1", reject_reason="MADE_UP_REASON",
            row_json={},
        )
    assert conn.state["inserts"] == []


def test_log_reject_handles_no_candidates():
    conn = _conn()
    log_reject(
        conn, import_batch_id=1, source_file="x", source_sheet="x",
        source_row_number=1, firm_name_raw="x", firm_name_normalised="x",
        case_reference_raw="1", reject_reason="MISSING_CASE_REFERENCE",
        row_json={},
    )
    params = conn.state["inserts"][0]
    assert params[8] is None  # candidate_ids_json
    assert params[9] is None  # candidate_names_json


def test_log_reject_serialises_candidate_lists():
    conn = _conn()
    log_reject(
        conn, import_batch_id=1, source_file="x", source_sheet="x",
        source_row_number=1, firm_name_raw="Knights", firm_name_normalised="knights",
        case_reference_raw="1", reject_reason="MULTIPLE_COMPANY_MATCHES",
        row_json={},
        candidate_company_ids=[7, 8, 9],
        candidate_company_names=["Knights plc", "Knights Solicitors", "Knights LLP"],
    )
    params = conn.state["inserts"][0]
    assert json.loads(params[8]) == [7, 8, 9]
    assert json.loads(params[9]) == ["Knights plc", "Knights Solicitors", "Knights LLP"]


def test_mark_resolved_for_updates_matching_open_rejects():
    conn = _conn()
    conn.state["update_rowcount"] = 3
    n = mark_resolved_for(
        conn,
        firm_name_normalised="hugh james",
        case_reference_raw="50001",
        resolution_note="resolved by import_batch_id=99",
    )
    assert n == 3
    assert len(conn.state["updates"]) == 1
    # The UPDATE filters on NEW/NEEDS_REVIEW only (idempotent — won't re-touch RESOLVED)
    sql_param_tuple = conn.state["updates"][0]
    # params are (resolution_note, now, firm_name_normalised, case_reference_raw)
    assert sql_param_tuple[0] == "resolved by import_batch_id=99"
    assert sql_param_tuple[2] == "hugh james"
    assert sql_param_tuple[3] == "50001"


def test_mark_resolved_for_no_keys_is_noop():
    conn = _conn()
    n = mark_resolved_for(conn, firm_name_normalised=None, case_reference_raw=None, resolution_note="x")
    assert n == 0
    assert conn.state["updates"] == []
