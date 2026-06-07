"""WO-21 — Duplicate Review Wizard.

Live-integration. GET pages smoke-tested against real data; the decision/undo
flows are exercised on a DISPOSABLE throwaway group (created + torn down here)
so no real review state is touched. Skips if nif_wip is unreachable.

Proves: yes -> approved, no -> rejected (sticky), not-sure -> needs_review, the
Change-decision / Reopen-review reversals, reviewer recording, and that the
flows only change status (nothing merged or deleted).
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
NORM = "wo21 selftest firm"


def _status(conn, gid):
    with conn.cursor() as c:
        c.execute("SELECT review_status FROM wip_company_group WHERE id=%s", (gid,))
        return c.fetchone()["review_status"]


@pytest.fixture()
def disposable_group():
    from src.wip_app.db import wip_connection
    conn = wip_connection(autocommit=True)
    with conn.cursor() as c:
        c.execute("INSERT INTO wip_company (salesforce_account_id, salesforce_account_name, "
                  "account_name_normalised, is_active_in_salesforce, "
                  "last_seen_in_salesforce_at, last_salesforce_sync_at, created_at, updated_at) "
                  "VALUES ('TESTWO21ACC', 'WO21 Selftest Firm', %s, 1, NOW(6), NOW(6), NOW(6), NOW(6))", (NORM,))
        c.execute("SELECT id FROM wip_company WHERE salesforce_account_id='TESTWO21ACC'")
        wcid = c.fetchone()["id"]
        c.execute("INSERT INTO wip_company_group (group_name, group_name_normalised, group_type, "
                  "review_status) VALUES ('WO21 Selftest Firm', %s, 'FIRM_GROUP', 'PROPOSED')", (NORM,))
        c.execute("SELECT id FROM wip_company_group WHERE group_name_normalised=%s ORDER BY id DESC LIMIT 1", (NORM,))
        gid = c.fetchone()["id"]
        c.execute("INSERT INTO wip_company_group_member (group_id, wip_company_id, salesforce_account_id, "
                  "member_role, review_status, confidence_source) "
                  "VALUES (%s, %s, 'TESTWO21ACC', 'UNKNOWN', 'PROPOSED', 'OPERATOR_MANUAL')", (gid, wcid))
    yield {"gid": gid, "wcid": wcid, "conn": conn}
    with conn.cursor() as c:
        c.execute("DELETE FROM wip_company_group_member WHERE group_id=%s", (gid,))
        c.execute("DELETE FROM wip_company_group WHERE id=%s", (gid,))
        c.execute("DELETE FROM wip_company WHERE id=%s", (wcid,))
        c.execute("DELETE FROM wip_company_group_seed_reject WHERE group_name_normalised=%s", (NORM,))
    conn.close()


# ---- GET pages ----

def test_dashboard_plain_english():
    r = client.get("/duplicate-review")
    assert r.status_code == 200
    assert "If you're not sure" in r.text


def test_item_has_three_buttons_and_separated_money(disposable_group):
    r = client.get("/duplicate-review/item/%d" % disposable_group["gid"])
    assert r.status_code == 200
    # WO-23D: blunt single "Yes" replaced by selective confirmation.
    for label in ("Confirm selected firms", "These are different firms", "Not sure",
                  "Confirmed data", "Awaiting review", "not counted as WIP", "Show details"):
        assert label in r.text


def test_confirmed_and_rejected_pages():
    assert client.get("/duplicate-review/confirmed").status_code == 200
    assert client.get("/duplicate-review/rejected").status_code == 200


# ---- decision + undo flows (disposable group) ----

def test_not_sure_then_yes_then_change(disposable_group):
    gid, conn = disposable_group["gid"], disposable_group["conn"]
    client.post("/duplicate-review/item/%d/decide" % gid, data={"decision": "notsure", "reviewer": ""})
    assert _status(conn, gid) == "NEEDS_REVIEW"
    client.post("/duplicate-review/item/%d/decide" % gid,
                data={"decision": "yes", "reviewer": "Test User", "member": "TESTWO21ACC"})
    assert _status(conn, gid) == "APPROVED"
    with conn.cursor() as c:
        c.execute("SELECT reviewed_by FROM wip_company_group WHERE id=%s", (gid,))
        assert c.fetchone()["reviewed_by"] == "Test User"          # who decided is recorded
    client.post("/duplicate-review/item/%d/change" % gid, data={"action": "not_sure", "reviewer": ""})
    assert _status(conn, gid) == "NEEDS_REVIEW"                     # reversible


def test_no_is_sticky_then_reopen(disposable_group):
    gid, conn = disposable_group["gid"], disposable_group["conn"]
    client.post("/duplicate-review/item/%d/decide" % gid, data={"decision": "no", "reviewer": ""})
    assert _status(conn, gid) == "REJECTED"
    with conn.cursor() as c:
        c.execute("SELECT COUNT(*) n FROM wip_company_group_seed_reject WHERE group_name_normalised=%s", (NORM,))
        assert c.fetchone()["n"] == 1                              # sticky — won't be re-proposed
    client.post("/duplicate-review/item/%d/change" % gid, data={"action": "not_sure", "reviewer": ""})
    assert _status(conn, gid) == "NEEDS_REVIEW"                    # reopen works


def test_decision_is_non_destructive(disposable_group):
    gid, wcid, conn = disposable_group["gid"], disposable_group["wcid"], disposable_group["conn"]
    client.post("/duplicate-review/item/%d/decide" % gid,
                data={"decision": "yes", "reviewer": "", "member": "TESTWO21ACC"})
    with conn.cursor() as c:
        c.execute("SELECT COUNT(*) n FROM wip_company WHERE id=%s", (wcid,))
        assert c.fetchone()["n"] == 1                              # the SF-mirrored company still exists
        c.execute("SELECT COUNT(*) n FROM wip_company_group_member WHERE group_id=%s", (gid,))
        assert c.fetchone()["n"] == 1                              # members not deleted, only re-statused
