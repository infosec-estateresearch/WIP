from __future__ import annotations

"""Sync runner tests — the insert / update / unchanged / soft-delete / reactivate
paths, plus dry-run safety. Uses a fake pymysql connection so no real DB needed.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.wip_app.sync.canonical_resolver import CanonicalResolver
from src.wip_app.sync.runner import SyncResult, run_sync


# ---------------------------------------------------------------------------
# Fake connection that exposes the wip_company state per salesforce_account_id
# ---------------------------------------------------------------------------

def _make_conn(existing_rows: list[dict] | None = None):
    """existing_rows: each represents one wip_company row keyed by sf id."""
    existing = {r["salesforce_account_id"]: dict(r) for r in (existing_rows or [])}
    next_id = {"i": max([r["id"] for r in existing.values()], default=0)}

    conn = MagicMock()
    state = {
        "inserts": [],
        "updates": [],
        "sync_runs": [],
        "audit_writes": [],
        "autocommit_value": False,
        "committed": 0,
        "rolledback": 0,
    }

    def cursor():
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.lastrowid = None

        def execute(sql, params=()):
            s = sql.strip().upper()
            if s.startswith("INSERT INTO WIP_SYNC_RUN"):
                state["sync_runs"].append({"action": "open", "params": params})
                next_id["i"] += 1
                cur.lastrowid = 1000 + next_id["i"]
            elif s.startswith("UPDATE WIP_SYNC_RUN"):
                state["sync_runs"].append({"action": "close", "params": params})
            elif s.startswith("SELECT COMPLETED_AT FROM WIP_SYNC_RUN"):
                cur._fetched_one = None  # no prior incremental
            elif s.startswith("SELECT * FROM WIP_COMPANY"):
                sf_id = params[0]
                cur._fetched_one = existing.get(sf_id)
            elif s.startswith("SELECT ID, SALESFORCE_ACCOUNT_ID FROM WIP_COMPANY"):
                cur._fetched = [{"id": r["id"], "salesforce_account_id": r["salesforce_account_id"]}
                                for r in existing.values() if r.get("is_active_in_salesforce", 1)]
            elif s.startswith("INSERT INTO WIP_COMPANY"):
                state["inserts"].append(dict(params))
                next_id["i"] += 1
                p = dict(params)
                p["id"] = next_id["i"]
                p["is_active_in_salesforce"] = p.get("is_active_in_salesforce", 1)
                existing[p["salesforce_account_id"]] = p
            elif s.startswith("UPDATE WIP_COMPANY SET SALESFORCE_ACCOUNT_NAME"):
                state["updates"].append(dict(params))
                p = dict(params)
                existing_id = p["existing_id"]
                for sfid, row in existing.items():
                    if row["id"] == existing_id:
                        row.update({k: p[k] for k in p if k != "now" and k != "existing_id"})
                        break
            elif s.startswith("UPDATE WIP_COMPANY SET LAST_SEEN"):
                pass  # housekeeping update on unchanged rows
            elif s.startswith("UPDATE WIP_COMPANY SET IS_ACTIVE_IN_SALESFORCE = 0"):
                state["updates"].append({"soft_delete": params})
            elif s.startswith("INSERT INTO WIP_AUDIT_LOG"):
                state["audit_writes"].append(params)
            elif s.startswith("UPDATE WIP_COMPANY SET IS_POTENTIAL_DUPLICATE"):
                pass  # duplicate detector — ignored in this fake
            elif s.startswith("SELECT ID, SALESFORCE_ACCOUNT_ID, SALESFORCE_ACCOUNT_NAME"):
                # duplicate detector reading active rows
                cur._fetched = []
        cur.execute.side_effect = execute
        cur.fetchall.side_effect = lambda: getattr(cur, "_fetched", [])
        cur.fetchone.side_effect = lambda: getattr(cur, "_fetched_one", None)
        return cur

    conn.cursor.side_effect = cursor
    conn.autocommit = MagicMock(side_effect=lambda v: state.__setitem__("autocommit_value", v))
    conn.get_autocommit = MagicMock(return_value=False)
    conn.commit = MagicMock(side_effect=lambda: state.__setitem__("committed", state["committed"] + 1))
    conn.rollback = MagicMock(side_effect=lambda: state.__setitem__("rolledback", state["rolledback"] + 1))
    conn.state = state
    conn._existing = existing
    return conn


def _resolver(*, display=None, normalised=None):
    r = CanonicalResolver()
    for n, ids in (display or {}).items():
        for i in ids:
            r.by_display[n].add(i)
    for n, ids in (normalised or {}).items():
        for i in ids:
            r.by_normalised[n].add(i)
    return r


def _sf_client(accounts: list[dict]):
    """Build a fake simple_salesforce client whose query_all returns the
    provided list shaped like the SF API response.
    """
    sf = MagicMock()
    sf.query_all.return_value = {"records": accounts, "totalSize": len(accounts)}
    sf.query.return_value = {"records": accounts, "totalSize": len(accounts)}
    return sf


def _acct(sf_id, name, **kwargs):
    base = {
        "attributes": {"type": "Account", "url": f"/services/data/v52.0/sobjects/Account/{sf_id}"},
        "Id": sf_id, "Name": name,
        "Type": kwargs.get("Type"), "Industry": kwargs.get("Industry"),
        "OwnerId": kwargs.get("OwnerId"),
        "Owner": {"Name": kwargs.get("Owner_Name"), "attributes": {}} if kwargs.get("Owner_Name") else None,
        "BillingStreet": kwargs.get("BillingStreet"),
        "BillingCity": kwargs.get("BillingCity"),
        "BillingPostalCode": kwargs.get("BillingPostalCode"),
        "BillingCountry": kwargs.get("BillingCountry"),
        "TLP__c": kwargs.get("TLP__c"),
        "IsDeleted": kwargs.get("IsDeleted", False),
        "LastModifiedDate": kwargs.get("LastModifiedDate", "2026-05-28T07:00:00.000+0000"),
    }
    return base


# ---------------------------------------------------------------------------
# Test scenarios
# ---------------------------------------------------------------------------

def test_full_apply_inserts_new_account():
    conn = _make_conn(existing_rows=[])
    sf = _sf_client([_acct("001A", "Hugh James", Type="Tier 1", Industry="Solicitor")])
    result = run_sync(conn, mode="full", apply=True, sf_client=sf, canonical_resolver=_resolver())
    assert result.fetched == 1
    assert result.inserted == 1
    assert result.updated == 0
    assert result.unchanged == 0
    assert len(conn.state["inserts"]) == 1
    # Audit got an 'insert' row
    audits = [a for a in conn.state["audit_writes"]]
    assert len(audits) == 1
    assert audits[0][2] == "insert"


def test_full_apply_updates_changed_account_with_audit():
    existing = [{
        "id": 1, "salesforce_account_id": "001A", "salesforce_account_name": "Hugh James",
        "account_name_normalised": "hugh james", "duplicate_group_key": "hugh james",
        "account_type": "Tier 2", "industry": "Solicitor", "owner_salesforce_id": None,
        "owner_name": None, "billing_street": None, "billing_city": None,
        "billing_postcode": None, "billing_country": None, "tlp_flag": 0,
        "canonical_company_id": None, "is_active_in_salesforce": 1,
        "salesforce_deleted_at": None,
    }]
    conn = _make_conn(existing_rows=existing)
    # Same SF account but Type is now Tier 1 (changed) and Industry same.
    sf = _sf_client([_acct("001A", "Hugh James", Type="Tier 1", Industry="Solicitor")])
    result = run_sync(conn, mode="full", apply=True, sf_client=sf, canonical_resolver=_resolver())
    assert result.updated == 1
    assert result.inserted == 0
    # Audit fired with action='update' and changed_fields containing account_type
    update_audits = [a for a in conn.state["audit_writes"] if a[2] == "update"]
    assert len(update_audits) == 1


def test_full_apply_unchanged_writes_no_audit():
    existing = [{
        "id": 1, "salesforce_account_id": "001A", "salesforce_account_name": "Hugh James",
        "account_name_normalised": "hugh james", "duplicate_group_key": "hugh james",
        "account_type": "Tier 1", "industry": "Solicitor", "owner_salesforce_id": None,
        "owner_name": None, "billing_street": None, "billing_city": None,
        "billing_postcode": None, "billing_country": None, "tlp_flag": 0,
        "canonical_company_id": None, "is_active_in_salesforce": 1,
        "salesforce_deleted_at": None,
    }]
    conn = _make_conn(existing_rows=existing)
    sf = _sf_client([_acct("001A", "Hugh James", Type="Tier 1", Industry="Solicitor")])
    result = run_sync(conn, mode="full", apply=True, sf_client=sf, canonical_resolver=_resolver())
    assert result.unchanged == 1
    assert result.updated == 0
    # No audit entries for the unchanged row
    assert conn.state["audit_writes"] == []


def test_full_apply_soft_retires_unseen_active_company():
    """An active wip_company row that doesn't appear in the full SF fetch
    must be soft-deleted (is_active_in_salesforce → 0) with an audit row.
    Never hard-deleted.
    """
    existing = [{
        "id": 1, "salesforce_account_id": "001Z", "salesforce_account_name": "GoneFirm",
        "account_name_normalised": "gonefirm", "duplicate_group_key": "gonefirm",
        "account_type": None, "industry": None, "owner_salesforce_id": None,
        "owner_name": None, "billing_street": None, "billing_city": None,
        "billing_postcode": None, "billing_country": None, "tlp_flag": 0,
        "canonical_company_id": None, "is_active_in_salesforce": 1,
        "salesforce_deleted_at": None,
    }]
    conn = _make_conn(existing_rows=existing)
    sf = _sf_client([_acct("001A", "Hugh James")])  # 001Z not in fetch
    result = run_sync(conn, mode="full", apply=True, sf_client=sf, canonical_resolver=_resolver())
    assert result.soft_deleted == 1
    soft_delete_audits = [a for a in conn.state["audit_writes"] if a[2] == "soft_delete"]
    assert len(soft_delete_audits) == 1


def test_dry_run_inserts_zero_writes():
    conn = _make_conn(existing_rows=[])
    sf = _sf_client([_acct("001A", "Hugh James")])
    result = run_sync(conn, mode="full", apply=False, sf_client=sf, canonical_resolver=_resolver())
    assert result.inserted == 1  # would insert
    # But NO actual INSERT INTO wip_company happened
    assert conn.state["inserts"] == []
    # And no audit either
    assert conn.state["audit_writes"] == []


def test_canonical_resolver_counters_track_status():
    """Resolved / unresolved / ambiguous counters must reflect what the
    resolver returned for each account.
    """
    # 'X' normalises to 'x'; for ambiguity we need display['X']=6 AND
    # normalised['x']=5 — two different cc_ids reachable, so resolver returns
    # ambiguous.
    resolver = _resolver(
        display={"Hugh James": [42], "X": [6]},  # Hugh James → resolved; X has one display hit
        normalised={"x": [5]},                    # …but normalised says cc_id=5 → ambiguous
    )
    conn = _make_conn(existing_rows=[])
    sf = _sf_client([
        _acct("001A", "Hugh James"),       # resolved
        _acct("001B", "X"),                 # ambiguous
        _acct("001C", "Totally Unknown"),  # unresolved
    ])
    result = run_sync(conn, mode="full", apply=False, sf_client=sf, canonical_resolver=resolver)
    assert result.canonical_resolved == 1
    assert result.canonical_ambiguous == 1
    assert result.canonical_unresolved == 1


def test_sync_run_row_created_even_on_dry_run():
    """The sync_run telemetry row must be created so dry-runs are observable."""
    conn = _make_conn(existing_rows=[])
    sf = _sf_client([])
    result = run_sync(conn, mode="full", apply=False, sf_client=sf, canonical_resolver=_resolver())
    opens = [s for s in conn.state["sync_runs"] if s["action"] == "open"]
    closes = [s for s in conn.state["sync_runs"] if s["action"] == "close"]
    assert len(opens) == 1
    assert len(closes) >= 1
    assert result.sync_run_id is not None
