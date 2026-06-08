"""WO-26 — approving a group whose Salesforce accounts share a canonical.

A firm can appear in Salesforce as several account records that all resolve to
the same canonical company. The unique index
ux_wcgm_canonical_when_approved allows only one APPROVED member-row per
canonical, so the old _approve_group raised
  (1062, "Duplicate entry ... for key 'ux_wcgm_canonical_when_approved'")
and the confirm crashed. Now approval keeps the canonical on one representative
and clears it on the redundant duplicate(s); every member is still APPROVED.
Live-integration on a DISPOSABLE group. Skips if nif_wip is unreachable.
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
NORM = "wo26 selftest firm"
ACCS = ["TESTWO26A", "TESTWO26B", "TESTWO26C"]
# Deliberately-fake canonical ids, well clear of real data. A and B share one.
CANON = {"TESTWO26A": 990026001, "TESTWO26B": 990026001, "TESTWO26C": 990026002}


@pytest.fixture()
def shared_canonical_group():
    from src.wip_app.db import wip_connection
    conn = wip_connection(autocommit=True)
    with conn.cursor() as c:
        for acc in ACCS:
            c.execute("INSERT INTO wip_company (salesforce_account_id, salesforce_account_name, "
                      "account_name_normalised, canonical_company_id, is_active_in_salesforce, "
                      "last_seen_in_salesforce_at, last_salesforce_sync_at, created_at, updated_at) "
                      "VALUES (%s, %s, %s, %s, 1, NOW(6), NOW(6), NOW(6), NOW(6))",
                      (acc, acc, NORM, CANON[acc]))
            c.execute("SELECT id FROM wip_company WHERE salesforce_account_id=%s", (acc,))
            wcid = c.fetchone()["id"]
            if acc == ACCS[0]:
                c.execute("INSERT INTO wip_company_group (group_name, group_name_normalised, "
                          "group_type, review_status) VALUES ('WO26 Selftest Firm', %s, "
                          "'FIRM_GROUP', 'PROPOSED')", (NORM,))
                c.execute("SELECT id FROM wip_company_group WHERE group_name_normalised=%s "
                          "ORDER BY id DESC LIMIT 1", (NORM,))
                gid = c.fetchone()["id"]
            c.execute("INSERT INTO wip_company_group_member (group_id, wip_company_id, "
                      "salesforce_account_id, canonical_company_id, member_role, review_status, "
                      "confidence_source) VALUES (%s, %s, %s, %s, 'UNKNOWN', 'PROPOSED', "
                      "'OPERATOR_MANUAL')", (gid, wcid, acc, CANON[acc]))
    yield {"gid": gid, "conn": conn}
    with conn.cursor() as c:
        c.execute("DELETE FROM wip_company_group_member WHERE group_id=%s", (gid,))
        c.execute("DELETE FROM wip_company_group WHERE id=%s", (gid,))
        c.execute("DELETE FROM wip_company WHERE salesforce_account_id IN (%s,%s,%s)", tuple(ACCS))
        c.execute("DELETE FROM wip_company_group_seed_reject WHERE group_name_normalised=%s", (NORM,))
    conn.close()


def _members(conn, gid):
    with conn.cursor() as c:
        c.execute("SELECT salesforce_account_id, review_status, canonical_company_id "
                  "FROM wip_company_group_member WHERE group_id=%s", (gid,))
        return {r["salesforce_account_id"]: r for r in c.fetchall()}


def test_confirm_with_shared_canonical_succeeds(shared_canonical_group):
    gid, conn = shared_canonical_group["gid"], shared_canonical_group["conn"]
    r = client.post("/duplicate-review/item/%d/decide" % gid,
                    data={"decision": "yes", "reviewer": "Op", "member": list(ACCS)},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "error=" not in r.headers["location"]            # no crash, no "Could not save"

    with conn.cursor() as c:
        c.execute("SELECT review_status FROM wip_company_group WHERE id=%s", (gid,))
        assert c.fetchone()["review_status"] == "APPROVED"

    members = _members(conn, gid)
    assert {m["review_status"] for m in members.values()} == {"APPROVED"}   # all approved

    # Exactly one of the two shared-canonical accounts keeps the canonical link;
    # the redundant one is cleared. The distinct-canonical account keeps its own.
    a, b, cc = members["TESTWO26A"], members["TESTWO26B"], members["TESTWO26C"]
    shared_links = [m["canonical_company_id"] for m in (a, b)]
    assert shared_links.count(990026001) == 1 and shared_links.count(None) == 1
    assert cc["canonical_company_id"] == 990026002


def test_friendly_message_on_cross_group_conflict(shared_canonical_group):
    """A second group that reuses an already-APPROVED canonical gets a friendly
    message, not a raw 1062."""
    gid, conn = shared_canonical_group["gid"], shared_canonical_group["conn"]
    # Approve the first group (claims canonical 990026002 via account C).
    client.post("/duplicate-review/item/%d/decide" % gid,
                data={"decision": "yes", "reviewer": "Op", "member": list(ACCS)})

    # Build a second disposable group whose member reuses canonical 990026002.
    with conn.cursor() as c:
        c.execute("INSERT INTO wip_company (salesforce_account_id, salesforce_account_name, "
                  "account_name_normalised, canonical_company_id, is_active_in_salesforce, "
                  "last_seen_in_salesforce_at, last_salesforce_sync_at, created_at, updated_at) "
                  "VALUES ('TESTWO26X','TESTWO26X','wo26 other', 990026002, 1, NOW(6), NOW(6), NOW(6), NOW(6))")
        c.execute("SELECT id FROM wip_company WHERE salesforce_account_id='TESTWO26X'")
        wx = c.fetchone()["id"]
        c.execute("INSERT INTO wip_company_group (group_name, group_name_normalised, group_type, "
                  "review_status) VALUES ('WO26 Other','wo26 other','FIRM_GROUP','PROPOSED')")
        c.execute("SELECT id FROM wip_company_group WHERE group_name_normalised='wo26 other' ORDER BY id DESC LIMIT 1")
        g2 = c.fetchone()["id"]
        c.execute("INSERT INTO wip_company_group_member (group_id, wip_company_id, salesforce_account_id, "
                  "canonical_company_id, member_role, review_status, confidence_source) "
                  "VALUES (%s,%s,'TESTWO26X',990026002,'UNKNOWN','PROPOSED','OPERATOR_MANUAL')", (g2, wx))
    try:
        r = client.post("/duplicate-review/item/%d/decide" % g2,
                        data={"decision": "yes", "reviewer": "Op", "member": ["TESTWO26X"]},
                        follow_redirects=False)
        assert r.status_code == 303
        from urllib.parse import unquote
        loc = unquote(r.headers["location"])
        assert "error=" in loc and "1062" not in loc            # friendly, not raw SQL
        assert "already confirmed" in loc.lower()
        with conn.cursor() as c:
            c.execute("SELECT review_status FROM wip_company_group WHERE id=%s", (g2,))
            assert c.fetchone()["review_status"] == "PROPOSED"  # nothing applied
    finally:
        with conn.cursor() as c:
            c.execute("DELETE FROM wip_company_group_member WHERE group_id=%s", (g2,))
            c.execute("DELETE FROM wip_company_group WHERE id=%s", (g2,))
            c.execute("DELETE FROM wip_company WHERE salesforce_account_id='TESTWO26X'")
