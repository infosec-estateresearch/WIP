"""WO-27 — Add/Edit WIP entry UI, cutover-gated.

Proves the gate (GET forms render, POST writes refused while
WIP_MANUAL_WRITES_ENABLED is off, no mutation) AND that flipping the flag
enables the already-built write path (create + validation + duplicate block +
no company creation + manual-only edit) — so WO-27B is a flag flip, not a
redesign. Live-integration on a DISPOSABLE company. Skips if nif_wip unreachable.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


def _load_env(path="/home/WIP/.env"):
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
    except OSError:
        pass


_load_env()
try:
    from src.wip_app.db import wip_connection
    wip_connection(autocommit=False).close()
    _DB = True
except Exception:
    _DB = False

pytestmark = pytest.mark.skipif(not _DB, reason="nif_wip not reachable")

from src.wip_web.app import app  # noqa: E402

client = TestClient(app)
SF = "TESTWO27ACC"


@pytest.fixture(autouse=True)
def _writes_off(monkeypatch):
    # Default every test to writes OFF; enabled tests opt in explicitly.
    monkeypatch.setenv("WIP_MANUAL_WRITES_ENABLED", "false")
    yield


@pytest.fixture()
def firm():
    from src.wip_app.db import wip_connection
    conn = wip_connection(autocommit=True)
    with conn.cursor() as c:
        c.execute("INSERT INTO wip_company (salesforce_account_id, salesforce_account_name, "
                  "account_name_normalised, is_active_in_salesforce, last_seen_in_salesforce_at, "
                  "last_salesforce_sync_at, created_at, updated_at) "
                  "VALUES (%s,'WO27 Selftest Firm','wo27 selftest firm',1,NOW(6),NOW(6),NOW(6),NOW(6))", (SF,))
        c.execute("SELECT id FROM wip_company WHERE salesforce_account_id=%s", (SF,))
        cid = c.fetchone()["id"]
    yield {"id": cid, "conn": conn}
    with conn.cursor() as c:
        c.execute("SELECT id FROM wip_case WHERE wip_company_id=%s", (cid,))
        case_ids = [r["id"] for r in c.fetchall()]
        for cs in case_ids:
            c.execute("SELECT id FROM wip_case_financial WHERE wip_case_id=%s", (cs,))
            fin_ids = [r["id"] for r in c.fetchall()]
            for fid in fin_ids:
                c.execute("DELETE FROM wip_audit_log WHERE entity_type='wip_case_financial' AND entity_id=%s", (fid,))
            c.execute("DELETE FROM wip_case_financial WHERE wip_case_id=%s", (cs,))
            c.execute("DELETE FROM wip_audit_log WHERE entity_type='wip_case' AND entity_id=%s", (cs,))
        c.execute("DELETE FROM wip_case WHERE wip_company_id=%s", (cid,))
        c.execute("DELETE FROM wip_company WHERE id=%s", (cid,))
    conn.close()


def _count_manual(conn, cid):
    with conn.cursor() as c:
        c.execute("SELECT COUNT(*) n FROM wip_case WHERE wip_company_id=%s AND source='manual'", (cid,))
        return c.fetchone()["n"]


# ---- gate OFF ----

def test_add_form_renders_with_submit_disabled(firm):
    r = client.get("/company/%d/wip/new" % firm["id"])
    assert r.status_code == 200
    assert "Save WIP entry" in r.text
    assert "still the active source for WIP" in r.text       # cutover explanation
    assert "Available after WIP cutover" in r.text
    assert 'btn-success btn-lg" disabled' in r.text          # submit greyed out


def test_select_firm_search_renders(firm):
    assert client.get("/wip/new").status_code == 200
    r = client.get("/wip/new?q=WO27 Selftest")
    assert r.status_code == 200 and "WO27 Selftest Firm" in r.text


def test_post_blocked_while_writes_off(firm):
    r = client.post("/company/%d/wip/new" % firm["id"],
                    data={"case_reference": "WO27-OFF-1", "case_name": "Blocked",
                          "wip_value": "100", "as_of_date": "2026-06-08"},
                    follow_redirects=False)
    assert r.status_code == 403
    assert _count_manual(firm["conn"], firm["id"]) == 0      # no mutation


def test_no_company_creation_for_unknown_id(firm):
    # Posting against a non-existent company never creates one — it 404s.
    r = client.post("/company/999000111/wip/new",
                    data={"case_reference": "X", "case_name": "Y", "wip_value": "1",
                          "as_of_date": "2026-06-08"}, follow_redirects=False)
    assert r.status_code == 404


# ---- gate ON (proves the write path is complete for WO-27B) ----

def test_add_succeeds_when_enabled(firm, monkeypatch):
    monkeypatch.setenv("WIP_MANUAL_WRITES_ENABLED", "true")
    cid, conn = firm["id"], firm["conn"]
    r = client.post("/company/%d/wip/new" % cid,
                    data={"case_reference": "WO27-ON-1", "case_name": "Real Manual Case",
                          "status": "Open", "wip_value": "5000", "estimated_yield": "4000",
                          "fees_invoiced": "0", "as_of_date": "2026-06-08",
                          "manual_note": "added in demo"}, follow_redirects=False)
    assert r.status_code == 303
    assert "flash=" in r.headers["location"]
    with conn.cursor() as c:
        c.execute("SELECT id, source, manual_note, created_by FROM wip_case "
                  "WHERE case_reference='WO27-ON-1'")
        case = c.fetchone()
        assert case and case["source"] == "manual" and case["created_by"] == "operator"
        c.execute("SELECT wip_value FROM wip_case_financial WHERE wip_case_id=%s", (case["id"],))
        assert str(c.fetchone()["wip_value"]) == "5000.00"
        # appears in the ERic-facing current-cases view (visible in Live Cases)
        c.execute("SELECT current_wip_value FROM v_eric_wip_current_cases WHERE wip_case_id=%s", (case["id"],))
        assert c.fetchone() is not None
        # audited
        c.execute("SELECT COUNT(*) n FROM wip_audit_log WHERE entity_type='wip_case' "
                  "AND entity_id=%s AND action='insert'", (case["id"],))
        assert c.fetchone()["n"] == 1


def test_duplicate_case_reference_blocked_when_enabled(firm, monkeypatch):
    monkeypatch.setenv("WIP_MANUAL_WRITES_ENABLED", "true")
    cid, conn = firm["id"], firm["conn"]
    data = {"case_reference": "WO27-DUP", "case_name": "First", "wip_value": "10",
            "as_of_date": "2026-06-08"}
    assert client.post("/company/%d/wip/new" % cid, data=data, follow_redirects=False).status_code == 303
    r2 = client.post("/company/%d/wip/new" % cid, data=dict(data, case_name="Second"),
                     follow_redirects=False)
    assert r2.status_code == 422
    assert "already exists" in r2.text
    with conn.cursor() as c:
        c.execute("SELECT COUNT(*) n FROM wip_case WHERE case_reference='WO27-DUP'")
        assert c.fetchone()["n"] == 1                        # only the first


def test_validation_failure_when_enabled(firm, monkeypatch):
    monkeypatch.setenv("WIP_MANUAL_WRITES_ENABLED", "true")
    cid, conn = firm["id"], firm["conn"]
    r = client.post("/company/%d/wip/new" % cid,
                    data={"case_reference": "WO27-BAD", "case_name": "",   # missing name
                          "wip_value": "-5", "as_of_date": ""},            # negative + missing date
                    follow_redirects=False)
    assert r.status_code == 422
    assert _count_manual(conn, cid) == 0                     # nothing written


def test_imported_row_edit_blocked_even_when_enabled(firm, monkeypatch):
    monkeypatch.setenv("WIP_MANUAL_WRITES_ENABLED", "true")
    conn = firm["conn"]
    # an imported (csv_import) case from real data
    with conn.cursor() as c:
        c.execute("SELECT id FROM wip_case WHERE source='csv_import' LIMIT 1")
        imported = c.fetchone()
    if not imported:
        pytest.skip("no imported case available")
    r = client.post("/wip/case/%d/edit" % imported["id"],
                    data={"case_reference": "HACK", "case_name": "tampered",
                          "wip_value": "999999", "as_of_date": "2026-06-08"},
                    follow_redirects=False)
    assert r.status_code == 403                              # imported rows never editable here
    with conn.cursor() as c:
        c.execute("SELECT case_name FROM wip_case WHERE id=%s", (imported["id"],))
        assert c.fetchone()["case_name"] != "tampered"


def test_manual_row_edit_succeeds_when_enabled(firm, monkeypatch):
    monkeypatch.setenv("WIP_MANUAL_WRITES_ENABLED", "true")
    cid, conn = firm["id"], firm["conn"]
    client.post("/company/%d/wip/new" % cid,
                data={"case_reference": "WO27-EDIT", "case_name": "Before",
                      "wip_value": "100", "as_of_date": "2026-06-08"})
    with conn.cursor() as c:
        c.execute("SELECT id FROM wip_case WHERE case_reference='WO27-EDIT'")
        case_id = c.fetchone()["id"]
    r = client.post("/wip/case/%d/edit" % case_id,
                    data={"case_reference": "WO27-EDIT", "case_name": "After",
                          "status": "Instructed", "wip_value": "250", "as_of_date": "2026-06-09"},
                    follow_redirects=False)
    assert r.status_code == 303
    with conn.cursor() as c:
        c.execute("SELECT case_name, updated_by FROM wip_case WHERE id=%s", (case_id,))
        row = c.fetchone()
        assert row["case_name"] == "After" and row["updated_by"] == "operator"
        c.execute("SELECT wip_value FROM wip_case_financial WHERE wip_case_id=%s", (case_id,))
        assert str(c.fetchone()["wip_value"]) == "250.00"
