"""WO-23 Part D — selective per-firm group confirmation.

The wizard's old single "Yes" approved every proposed member. Now the operator
ticks only the firms that belong: ticked members are confirmed together;
unticked members are removed from the group (kept as standalone companies for
later review — never approved, never deleted); confirming with nothing ticked is
refused. All on a DISPOSABLE throwaway group. Skips if nif_wip is unreachable.
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
NORM = "wo23 selftest firm"
ACCS = ["TESTWO23A", "TESTWO23B", "TESTWO23C"]


@pytest.fixture()
def trio():
    """A PROPOSED group with three members A, B, C."""
    from src.wip_app.db import wip_connection
    conn = wip_connection(autocommit=True)
    wcids = {}
    with conn.cursor() as c:
        for acc in ACCS:
            c.execute("INSERT INTO wip_company (salesforce_account_id, salesforce_account_name, "
                      "account_name_normalised, is_active_in_salesforce, "
                      "last_seen_in_salesforce_at, last_salesforce_sync_at, created_at, updated_at) "
                      "VALUES (%s, %s, %s, 1, NOW(6), NOW(6), NOW(6), NOW(6))",
                      (acc, acc, NORM))
            c.execute("SELECT id FROM wip_company WHERE salesforce_account_id=%s", (acc,))
            wcids[acc] = c.fetchone()["id"]
        c.execute("INSERT INTO wip_company_group (group_name, group_name_normalised, group_type, "
                  "review_status) VALUES ('WO23 Selftest Firm', %s, 'FIRM_GROUP', 'PROPOSED')", (NORM,))
        c.execute("SELECT id FROM wip_company_group WHERE group_name_normalised=%s ORDER BY id DESC LIMIT 1", (NORM,))
        gid = c.fetchone()["id"]
        for acc in ACCS:
            c.execute("INSERT INTO wip_company_group_member (group_id, wip_company_id, salesforce_account_id, "
                      "member_role, review_status, confidence_source) "
                      "VALUES (%s, %s, %s, 'UNKNOWN', 'PROPOSED', 'OPERATOR_MANUAL')",
                      (gid, wcids[acc], acc))
    yield {"gid": gid, "wcids": wcids, "conn": conn}
    with conn.cursor() as c:
        c.execute("DELETE FROM wip_company_group_member WHERE group_id=%s", (gid,))
        c.execute("DELETE FROM wip_company_group WHERE id=%s", (gid,))
        c.execute("DELETE FROM wip_company WHERE salesforce_account_id IN (%s,%s,%s)", tuple(ACCS))
        c.execute("DELETE FROM wip_company_group_seed_reject WHERE group_name_normalised=%s", (NORM,))
    conn.close()


def _group(conn, gid):
    with conn.cursor() as c:
        c.execute("SELECT review_status, reviewed_by, resolution_note FROM wip_company_group WHERE id=%s", (gid,))
        return c.fetchone()


def _member_accs(conn, gid):
    with conn.cursor() as c:
        c.execute("SELECT salesforce_account_id FROM wip_company_group_member WHERE group_id=%s", (gid,))
        return {r["salesforce_account_id"] for r in c.fetchall()}


def test_item_renders_a_checkbox_per_member(trio):
    r = client.get("/duplicate-review/item/%d" % trio["gid"])
    assert r.status_code == 200
    assert r.text.count('name="member"') == 3          # one tickbox per firm
    assert "Confirm selected firms" in r.text


def test_confirm_subset_keeps_only_ticked(trio):
    gid, conn = trio["gid"], trio["conn"]
    # Tick A and B, leave C unticked.
    client.post("/duplicate-review/item/%d/decide" % gid,
                data={"decision": "yes", "reviewer": "Op",
                      "member": ["TESTWO23A", "TESTWO23B"]})
    g = _group(conn, gid)
    assert g["review_status"] == "APPROVED"
    remaining = _member_accs(conn, gid)
    assert remaining == {"TESTWO23A", "TESTWO23B"}      # only ticked firms confirmed
    assert "TESTWO23C" not in remaining                  # C removed from the group...
    with conn.cursor() as c:                             # ...but the company is NOT deleted
        c.execute("SELECT COUNT(*) n FROM wip_company WHERE salesforce_account_id='TESTWO23C'")
        assert c.fetchone()["n"] == 1
    # Audit trail records both who was included and who was excluded.
    note = g.get("resolution_note") or ""
    assert "Confirmed" in note and "TESTWO23A" in note
    assert "Excluded" in note and "TESTWO23C" in note


def test_confirm_with_nothing_ticked_is_refused(trio):
    gid, conn = trio["gid"], trio["conn"]
    r = client.post("/duplicate-review/item/%d/decide" % gid,
                    data={"decision": "yes", "reviewer": ""},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "error=" in r.headers["location"]             # bounced with a message
    assert _group(conn, gid)["review_status"] == "PROPOSED"   # nothing approved
    assert _member_accs(conn, gid) == set(ACCS)          # all three still intact


def test_confirm_all_ticked_keeps_everyone(trio):
    gid, conn = trio["gid"], trio["conn"]
    client.post("/duplicate-review/item/%d/decide" % gid,
                data={"decision": "yes", "reviewer": "", "member": list(ACCS)})
    assert _group(conn, gid)["review_status"] == "APPROVED"
    assert _member_accs(conn, gid) == set(ACCS)          # no one dropped when all ticked
