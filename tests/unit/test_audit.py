"""Audit-writer tests — architect rule: only write rows for material changes."""

import json
from unittest.mock import MagicMock

from src.wip_app.audit import diff_fields, write_audit


# ---------------------------------------------------------------------------
# diff_fields — the gate that decides whether anything material changed
# ---------------------------------------------------------------------------

def test_diff_fields_returns_only_changed_keys():
    before = {"a": 1, "b": 2, "c": 3}
    after  = {"a": 1, "b": 99, "c": 3}
    assert diff_fields(before, after) == {"b": {"before": 2, "after": 99}}


def test_diff_fields_unchanged_returns_empty():
    before = {"a": 1, "b": 2}
    assert diff_fields(before, dict(before)) == {}


def test_diff_fields_respects_ignore_list():
    """Housekeeping fields like last_seen_in_salesforce_at must be ignored —
    they update on every sync but are not material changes.
    """
    before = {"name": "X", "last_seen": "2026-01-01"}
    after  = {"name": "X", "last_seen": "2026-05-28"}
    assert diff_fields(before, after, ignore=("last_seen",)) == {}


def test_diff_fields_detects_added_and_removed_keys():
    assert diff_fields({"a": 1}, {"a": 1, "b": 2}) == {"b": {"before": None, "after": 2}}
    assert diff_fields({"a": 1, "b": 2}, {"a": 1}) == {"b": {"before": 2, "after": None}}


# ---------------------------------------------------------------------------
# write_audit — must skip "update" actions with empty changed_fields
# ---------------------------------------------------------------------------

def _make_conn():
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.executed = []
    cur.execute.side_effect = lambda sql, params=(): cur.executed.append((sql, params))
    conn.cursor.return_value = cur
    return conn, cur


def test_write_audit_skips_update_with_no_changes():
    conn, cur = _make_conn()
    write_audit(
        conn, entity_type="wip_company", entity_id=1, action="update",
        after={"x": 1}, changed_fields=None, actor="sf_sync",
    )
    assert cur.executed == []  # NO write happened


def test_write_audit_writes_insert_even_without_changed_fields():
    conn, cur = _make_conn()
    write_audit(
        conn, entity_type="wip_company", entity_id=42, action="insert",
        after={"salesforce_account_id": "001ABC"}, actor="sf_sync",
        sync_run_id=7,
    )
    assert len(cur.executed) == 1
    sql, params = cur.executed[0]
    assert "INSERT INTO wip_audit_log" in sql
    assert params[0] == "wip_company"
    assert params[1] == 42
    assert params[2] == "insert"


def test_write_audit_writes_update_with_changed_fields():
    conn, cur = _make_conn()
    changes = {"industry": {"before": "Law", "after": "Legal Services"}}
    write_audit(
        conn, entity_type="wip_company", entity_id=42, action="update",
        after={"industry": "Legal Services"}, changed_fields=changes, actor="sf_sync",
    )
    assert len(cur.executed) == 1
    _, params = cur.executed[0]
    cf_json = json.loads(params[3])
    assert cf_json == changes


def test_write_audit_writes_soft_delete():
    conn, cur = _make_conn()
    write_audit(
        conn, entity_type="wip_company", entity_id=42, action="soft_delete",
        after={"is_active_in_salesforce": 0},
        changed_fields={"is_active_in_salesforce": {"before": 1, "after": 0}},
        actor="sf_sync", sync_run_id=7,
    )
    assert len(cur.executed) == 1
