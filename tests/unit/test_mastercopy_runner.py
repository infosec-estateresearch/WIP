"""End-to-end runner tests — uses a fake conn + a fake parse summary so
no real Excel/DB is required.

Covers the architect-required classifications: every source row produces
exactly ONE of inserted/updated/unchanged/rejected. No silent fifth state.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from src.wip_app.imports.firm_matcher import CompanyMatch, FirmMatcher
from src.wip_app.imports.mastercopy_parser import ParsedRow, ParseSummary
from src.wip_app.imports.mastercopy_runner import ImportResult, run_import


def _row(*, firm, case, wip=None, fees=None, sheet="May 2026", row_num=10):
    return ParsedRow(
        source_file="MASTERCOPY-V2.xlsx",
        source_sheet=sheet,
        source_row=row_num,
        period_year=2026,
        period_month=5,
        raw={
            "firm_name": firm,
            "case_number": case,
            "case_name": "Smith, John",
            "case_type": "Probate",
            "status": "active",
            "wip": wip,
            "fees_invoiced": fees,
            "actual_yield": None,
            "estimated_yield": None,
            "instruction_date": None,
        },
    )


def _make_conn():
    """Fake pymysql connection. Records every write; serves SELECTs from an
    in-memory store keyed by case_reference / (case_id, period).
    """
    conn = MagicMock()
    state = {
        "case_store": {},      # case_reference -> wip_case row
        "fin_store": {},       # (case_id, period_year, period_month) -> wip_case_financial row
        "rejects": [],          # logged reject rows
        "batches": [],          # opened import_batch rows
        "batch_closes": [],
        "resolved_marks": [],
        "next_case_id": [0],
        "next_batch_id": [100],
        "autocommit": False,
    }

    def cursor():
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.rowcount = 0
        # Explicit defaults — MagicMock auto-creates attributes, so without
        # these the fetch* lambdas would return MagicMock instances instead
        # of None/[].
        cur._fetched = []
        cur._fetched_one = None

        def execute(sql, params=()):
            s = sql.strip().upper()
            # Reset per-execute so a follow-up fetch can't see a previous
            # call's stale payload on the same cursor.
            cur._fetched = []
            cur._fetched_one = None

            if s.startswith("INSERT INTO WIP_IMPORT_BATCH"):
                state["next_batch_id"][0] += 1
                cur.lastrowid = state["next_batch_id"][0]
                state["batches"].append({"id": cur.lastrowid, "params": params})
            elif s.startswith("UPDATE WIP_IMPORT_BATCH"):
                state["batch_closes"].append(params)
            elif "FROM WIP_COMPANY" in s and "IS_ACTIVE_IN_SALESFORCE = 1" in s:
                cur._fetched = [
                    {"id": 1, "salesforce_account_id": "001A",
                     "salesforce_account_name": "Hugh James",
                     "account_name_normalised": "hugh james",
                     "canonical_company_id": 42,
                     "is_potential_duplicate": 0},
                ]
            # More-specific WIP_CASE_FINANCIAL checks MUST come before the
            # broader WIP_CASE checks (both share the WIP_CASE prefix).
            elif "FROM WIP_CASE_FINANCIAL" in s and s.startswith("SELECT"):
                # wip_case_id, period_year, period_month order in _upsert_financial /
                # _classify_dry_run is identical.
                key = (params[0], params[1], params[2])
                cur._fetched_one = state["fin_store"].get(key)
            elif s.startswith("INSERT INTO WIP_CASE_FINANCIAL"):
                key = (params[0], params[2], params[3])
                state["fin_store"][key] = {
                    "wip_case_id": params[0], "as_of_date": params[1],
                    "period_year": params[2], "period_month": params[3],
                    "wip_value": params[4], "fees_invoiced": params[5],
                    "actual_yield": params[6], "estimated_yield": params[7],
                }
            elif s.startswith("UPDATE WIP_CASE_FINANCIAL"):
                pass
            elif "FROM WIP_CASE" in s and "WHERE CASE_REFERENCE" in s and s.startswith("SELECT"):
                # Matches both `SELECT * FROM wip_case ...` (apply path) and
                # `SELECT id, wip_company_id, ... FROM wip_case ...` (dry-run path).
                cur._fetched_one = state["case_store"].get(params[0])
            elif s.startswith("INSERT INTO WIP_CASE"):
                state["next_case_id"][0] += 1
                cur.lastrowid = state["next_case_id"][0]
                row = {
                    "id": cur.lastrowid,
                    "wip_company_id": params[0],
                    "salesforce_account_id": params[1],
                    "canonical_company_id": params[2],
                    "case_reference": params[3],
                    "case_name": params[4],
                    "case_type": params[5],
                    "status": params[6],
                    "opened_at": params[7],
                }
                state["case_store"][params[3]] = row
            elif s.startswith("UPDATE WIP_CASE SET"):
                pass
            elif s.startswith("INSERT INTO WIP_IMPORT_REJECT"):
                state["rejects"].append(params)
            elif s.startswith("UPDATE WIP_IMPORT_REJECT"):
                state["resolved_marks"].append(params)
                cur.rowcount = 0
        cur.execute.side_effect = execute
        cur.fetchall.side_effect = lambda: cur._fetched
        cur.fetchone.side_effect = lambda: cur._fetched_one
        return cur

    conn.cursor.side_effect = cursor
    conn.autocommit = MagicMock(side_effect=lambda v: state.__setitem__("autocommit", v))
    conn.get_autocommit = MagicMock(return_value=False)
    conn.commit = MagicMock()
    conn.rollback = MagicMock()
    conn.state = state
    return conn


def _patch_parse_to_return(rows: list[ParsedRow]):
    """Patch parse_mastercopy() inside the runner module to return our rows."""
    summary = ParseSummary(rows_total=len(rows), file_hash="deadbeef", rows=rows)
    return patch("src.wip_app.imports.mastercopy_runner.parse_mastercopy",
                 return_value=summary)


def _patch_file_hash():
    return patch("src.wip_app.imports.mastercopy_runner.file_sha256",
                 return_value="deadbeef" * 8)


# ---------------------------------------------------------------------------
# Architect rule: every row has one of 4 outcomes
# ---------------------------------------------------------------------------

def test_matched_row_imports_as_inserted():
    rows = [_row(firm="Hugh James", case=50001, wip=1500.0)]
    conn = _make_conn()
    with _patch_parse_to_return(rows), _patch_file_hash():
        r = run_import(conn, source_path="MASTERCOPY-V2.xlsx", apply=True)
    assert r.rows_seen == 1
    assert r.rows_inserted == 1
    assert r.rows_rejected == 0


def test_unknown_firm_rejected_no_company_match():
    rows = [_row(firm="Made Up Firm Ltd", case=50001)]
    conn = _make_conn()
    with _patch_parse_to_return(rows), _patch_file_hash():
        r = run_import(conn, source_path="x", apply=True)
    assert r.rows_rejected == 1
    assert r.reject_breakdown["NO_COMPANY_MATCH"] == 1
    assert len(conn.state["rejects"]) == 1
    assert conn.state["rejects"][0][7] == "NO_COMPANY_MATCH"


def test_missing_firm_name_rejected():
    rows = [_row(firm=None, case=50001)]
    conn = _make_conn()
    with _patch_parse_to_return(rows), _patch_file_hash():
        r = run_import(conn, source_path="x", apply=True)
    assert r.rows_rejected == 1
    assert r.reject_breakdown["MISSING_FIRM_NAME"] == 1


def test_missing_case_reference_rejected():
    rows = [_row(firm="Hugh James", case=None)]
    conn = _make_conn()
    with _patch_parse_to_return(rows), _patch_file_hash():
        r = run_import(conn, source_path="x", apply=True)
    assert r.rows_rejected == 1
    assert r.reject_breakdown["MISSING_CASE_REFERENCE"] == 1


def test_invalid_financial_value_rejected():
    """Garbage in the wip column rejects the row rather than silently
    coercing or skipping."""
    bad = _row(firm="Hugh James", case=50001, wip="not a number")
    conn = _make_conn()
    with _patch_parse_to_return([bad]), _patch_file_hash():
        r = run_import(conn, source_path="x", apply=True)
    assert r.rows_rejected == 1
    assert r.reject_breakdown["INVALID_FINANCIAL_VALUE"] == 1


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_reimport_of_same_row_classifies_as_unchanged():
    rows = [_row(firm="Hugh James", case=50001, wip="1500.00")]
    conn = _make_conn()
    with _patch_parse_to_return(rows), _patch_file_hash():
        r1 = run_import(conn, source_path="x", apply=True)
    assert r1.rows_inserted == 1

    # Same parsed rows, re-imported — should be unchanged, not duplicate-inserted
    with _patch_parse_to_return(rows), _patch_file_hash():
        r2 = run_import(conn, source_path="x", apply=True)
    assert r2.rows_inserted == 0
    assert r2.rows_unchanged == 1
    assert r2.rows_rejected == 0


# ---------------------------------------------------------------------------
# Dry-run safety
# ---------------------------------------------------------------------------

def test_dry_run_writes_no_case_no_financial_no_reject():
    rows = [
        _row(firm="Hugh James", case=50001, wip=1500.0),
        _row(firm="Unknown Firm", case=50002),
    ]
    conn = _make_conn()
    with _patch_parse_to_return(rows), _patch_file_hash():
        r = run_import(conn, source_path="x", apply=False)
    # Telemetry still classifies
    assert r.rows_seen == 2
    assert r.rows_inserted == 1   # would-insert
    assert r.rows_rejected == 1   # would-reject
    # But NO actual writes
    assert conn.state["case_store"] == {}
    assert conn.state["fin_store"] == {}
    assert conn.state["rejects"] == []


# ---------------------------------------------------------------------------
# No silent fifth state (architect rule)
# ---------------------------------------------------------------------------

def test_every_row_is_classified():
    """The headline architect rule: rows_seen MUST equal
    inserted + updated + unchanged + rejected. No invisible fifth state.
    """
    rows = [
        _row(firm="Hugh James", case=50001, wip=1.0),   # insert
        _row(firm=None, case=50002),                     # reject MISSING_FIRM_NAME
        _row(firm="Hugh James", case=None),              # reject MISSING_CASE_REF
        _row(firm="Unknown", case=50004),                # reject NO_COMPANY_MATCH
        _row(firm="Hugh James", case=50005, wip="bad"),  # reject INVALID_FINANCIAL_VALUE
    ]
    conn = _make_conn()
    with _patch_parse_to_return(rows), _patch_file_hash():
        r = run_import(conn, source_path="x", apply=True)
    total = r.rows_inserted + r.rows_updated + r.rows_unchanged + r.rows_rejected
    assert total == r.rows_seen == 5
