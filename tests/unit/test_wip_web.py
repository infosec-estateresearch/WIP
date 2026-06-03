"""WO-14A.4 — WIP GUI route tests.

Uses FastAPI TestClient with the DB dependency overridden by an
in-memory fake, so tests neither require a running MariaDB nor touch
the live nif_wip database.

Scope of these tests:
  - Render checks: each route returns 200 with the right template
  - Action endpoints route through the right service function
  - Filter / pagination params are passed through correctly
  - The architect-required "no merge button / no create-company button"
    invariants are encoded as negative assertions in the rendered HTML
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.wip_web.app import app
from src.wip_web.deps import get_db


def _scalar_row(value, key="n"):
    return {key: value}


class FakeCursor:
    """Routes execute() by SQL substring; tests inject what each shape
    should return. Order of routing matters — most-specific first."""

    def __init__(self, plans):
        self.plans = plans
        self._next_fetched = []
        self._next_fetched_one = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        s = " ".join(sql.split()).upper()
        for matcher, payload in self.plans:
            if matcher(s):
                if callable(payload):
                    fetched, fetched_one = payload(params)
                else:
                    fetched, fetched_one = payload
                self._next_fetched = fetched or []
                self._next_fetched_one = fetched_one
                return
        self._next_fetched = []
        self._next_fetched_one = None

    def fetchall(self):
        return self._next_fetched

    def fetchone(self):
        return self._next_fetched_one


def _fake_conn(plans):
    conn = MagicMock()
    state = {"commits": 0, "rollbacks": 0}
    conn.cursor.side_effect = lambda: FakeCursor(plans)
    conn.autocommit = MagicMock()
    conn.get_autocommit = MagicMock(return_value=False)
    conn.commit = MagicMock(side_effect=lambda: state.__setitem__("commits", state["commits"] + 1))
    conn.rollback = MagicMock(side_effect=lambda: state.__setitem__("rollbacks", state["rollbacks"] + 1))
    conn.state = state
    return conn


@pytest.fixture
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def test_dashboard_renders_with_live_counts(client):
    plans = [
        (lambda s: "FROM WIP_COMPANY WHERE IS_ACTIVE_IN_SALESFORCE" in s, ([], _scalar_row(7375))),
        (lambda s: "FROM WIP_CASE" in s and "COUNT(*)" in s,             ([], _scalar_row(3663))),
        (lambda s: "FROM WIP_IMPORT_REJECT" in s and "RESOLUTION_STATUS" in s, ([], _scalar_row(25478))),
        (lambda s: "FROM WIP_COMPANY_DUPLICATE_CANDIDATE" in s,          ([], _scalar_row(45))),
        (lambda s: "FROM WIP_COMPANY_GROUP" in s and "PROPOSED" in s and "REVIEW_STATUS IN" in s, ([], _scalar_row(278))),
        (lambda s: "FROM WIP_COMPANY_GROUP" in s and "= 'APPROVED'" in s,([], _scalar_row(1))),
        (lambda s: "FROM WIP_SYNC_RUN" in s,                              ([], {"status": "completed", "started_at": None, "completed_at": None, "sync_type": "salesforce_account_incremental"})),
        (lambda s: "FROM WIP_IMPORT_BATCH" in s,                          ([], {"status": "completed", "completed_at": None, "rows_seen": 29223, "rows_inserted": 3720, "rows_updated": 25, "rows_rejected": 25478})),
    ]
    app.dependency_overrides[get_db] = lambda: _fake_conn(plans)
    try:
        r = client.get("/")
        assert r.status_code == 200
        body = r.text
        assert "7,375" in body
        assert "3,663" in body
        assert "25,478" in body
        assert "278" in body
        assert "Dashboard" in body
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Groups list — every group has an impact, status badge, View link
# ---------------------------------------------------------------------------

def test_groups_list_renders_and_links_to_detail(client):
    rows = [
        {"group_id": 68, "group_name": "Hugh James", "group_name_normalised": "hugh james",
         "review_status": "APPROVED", "group_type": "FIRM_GROUP", "seed_warning": "wide_geographic_split",
         "source_cluster_id": "CC-FRAG-hugh-james", "primary_salesforce_account_id": "0015800000HzXcoAAF",
         "primary_canonical_company_id": 13030, "reviewed_by": "operator", "reviewed_at": None,
         "member_count": 5, "group_current_wip": 75.0, "group_active_case_count": 1},
    ]
    plans = [
        (lambda s: s.startswith("SELECT COUNT(*) AS N FROM WIP_COMPANY_GROUP"), ([], _scalar_row(1))),
        (lambda s: "FROM WIP_COMPANY_GROUP G" in s and "LEFT JOIN WIP_COMPANY_GROUP_MEMBER" in s, (rows, None)),
    ]
    app.dependency_overrides[get_db] = lambda: _fake_conn(plans)
    try:
        r = client.get("/data-review/groups?status=APPROVED")
        assert r.status_code == 200
        assert "Hugh James" in r.text
        assert 'href="/data-review/groups/68"' in r.text
        assert "APPROVED" in r.text
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Group detail — PROPOSED renders the decision card; APPROVED hides it
# ---------------------------------------------------------------------------

def test_group_detail_proposed_shows_decision_card(client):
    group = {
        "id": 200, "group_name": "Test Co", "group_name_normalised": "test co",
        "review_status": "PROPOSED", "group_type": "FIRM_GROUP",
        "seed_warning": None, "source_cluster_id": "CC-FRAG-test",
        "primary_salesforce_account_id": None, "primary_canonical_company_id": None,
        "proposed_by": "wo-13-seed", "reviewed_by": None, "reviewed_at": None,
        "resolution_note": None,
    }
    members = [
        {"member_id": 1, "wip_company_id": 10, "salesforce_account_id": "0011000000000A1",
         "canonical_company_id": 100, "member_role": "UNKNOWN", "review_status": "PROPOSED",
         "salesforce_account_name": "Test Co (London)", "account_type": "Tier 2",
         "industry": "Legal", "billing_city": "London", "billing_postcode": "EC1",
         "is_potential_duplicate": 0, "current_wip_value": 1500.0, "current_wip_count": 2},
    ]
    plans = [
        (lambda s: s.startswith("SELECT SOURCE_CLUSTER_ID"), ([], {"source_cluster_id": "CC-FRAG-test"})),
        (lambda s: s.startswith("SELECT * FROM WIP_COMPANY_GROUP"), ([], group)),
        (lambda s: "FROM WIP_COMPANY_GROUP_MEMBER M" in s, (members, None)),
    ]
    app.dependency_overrides[get_db] = lambda: _fake_conn(plans)
    try:
        r = client.get("/data-review/groups/200")
        assert r.status_code == 200
        body = r.text
        assert "Test Co" in body
        # Decision card visible for PROPOSED (button copy is the
        # business-facing wording added in WO-14A.5; the underlying
        # action endpoints are unchanged).
        assert "Confirm group" in body
        assert "Reject group" in body
        assert "Set aside for now" in body
        # Architect rule: no split/merge UI in MVP — assert absence of
        # both the internal action names and the user-facing equivalents.
        assert "SPLIT_GROUP" not in body
        assert "MERGE_INTO" not in body
        for forbidden in ("Split group", "Merge into", "Split this group", "Merge into another"):
            assert forbidden not in body
    finally:
        app.dependency_overrides.clear()


def test_group_detail_approved_hides_decision_card(client):
    group = {
        "id": 68, "group_name": "Hugh James", "group_name_normalised": "hugh james",
        "review_status": "APPROVED", "group_type": "FIRM_GROUP", "seed_warning": None,
        "source_cluster_id": "CC-FRAG-hugh-james", "primary_salesforce_account_id": "0015800000HzXcoAAF",
        "primary_canonical_company_id": 13030, "proposed_by": "wo-13-seed",
        "reviewed_by": "operator", "reviewed_at": None, "resolution_note": "London = HQ",
    }
    plans = [
        (lambda s: s.startswith("SELECT SOURCE_CLUSTER_ID"), ([], {"source_cluster_id": "CC-FRAG-hugh-james"})),
        (lambda s: s.startswith("SELECT * FROM WIP_COMPANY_GROUP"), ([], group)),
        (lambda s: "FROM WIP_COMPANY_GROUP_MEMBER M" in s, ([], None)),
    ]
    app.dependency_overrides[get_db] = lambda: _fake_conn(plans)
    try:
        r = client.get("/data-review/groups/68")
        assert r.status_code == 200
        # Decision card hidden for APPROVED
        assert "Operator decision" not in r.text
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Group action — APPROVE refuses multi-member group without set_primary_sf_id
# (mirrors WO-14A.1 apply.py behavior — schema invariant guard)
# ---------------------------------------------------------------------------

def test_approve_action_redirects_to_detail_with_flash(client):
    group = {
        "id": 200, "group_name": "Test Co", "group_name_normalised": "test co",
        "review_status": "PROPOSED", "group_type": "FIRM_GROUP", "seed_warning": None,
        "source_cluster_id": "CC-FRAG-test", "primary_salesforce_account_id": None,
        "primary_canonical_company_id": None, "proposed_by": "wo-13-seed",
    }
    plans = [
        (lambda s: s.startswith("SELECT * FROM WIP_COMPANY_GROUP"), ([], group)),
        # Single-member fetch for _approve_group's PRIMARY decision
        (lambda s: "FROM WIP_COMPANY_GROUP_MEMBER" in s and "GROUP_ID" in s,
         ([{"id": 1, "salesforce_account_id": "0011000000000A1"}], None)),
        (lambda s: s.startswith("UPDATE WIP_COMPANY_GROUP "), ([], None)),
    ]
    app.dependency_overrides[get_db] = lambda: _fake_conn(plans)
    try:
        r = client.post(
            "/data-review/groups/200/approve",
            data={"set_primary_sf_id": "0011000000000A1", "operator_note": "test"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert "/data-review/groups/200" in r.headers["location"]
        assert "flash=" in r.headers["location"]
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Duplicates list — never shows a "merge" button
# ---------------------------------------------------------------------------

def test_duplicates_list_renders(client):
    rows = [
        {"id": 1, "duplicate_group_key": "russell", "candidate_count": 3, "status": "NEW",
         "first_detected_at": None, "last_seen_at": None, "reviewed_by": None,
         "reviewed_at": None, "resolution_note": None},
    ]
    plans = [
        (lambda s: s.startswith("SELECT COUNT(*) AS N FROM WIP_COMPANY_DUPLICATE_CANDIDATE"), ([], _scalar_row(1))),
        (lambda s: "FROM WIP_COMPANY_DUPLICATE_CANDIDATE" in s and "ORDER BY" in s, (rows, None)),
    ]
    app.dependency_overrides[get_db] = lambda: _fake_conn(plans)
    try:
        r = client.get("/data-review/duplicates")
        assert r.status_code == 200
        body = r.text
        assert "russell" in body
        # Architect rule: Salesforce owns the merge — no merge button
        assert "Merge" not in body or "no merge" in body.lower() or "owns the merge" in body.lower()
    finally:
        app.dependency_overrides.clear()


def test_duplicates_list_explicit_salesforce_owns_merge_message(client):
    """The Salesforce-owns-the-merge guidance must be visible to the
    operator — not buried. The exact wording changed in WO-14A.5 (more
    plain-English) but the principle is unchanged."""
    plans = [
        (lambda s: s.startswith("SELECT COUNT"), ([], _scalar_row(0))),
        (lambda s: "FROM WIP_COMPANY_DUPLICATE_CANDIDATE" in s, ([], None)),
    ]
    app.dependency_overrides[get_db] = lambda: _fake_conn(plans)
    try:
        r = client.get("/data-review/duplicates")
        assert "merge happens in Salesforce" in r.text or "Salesforce owns the merge" in r.text
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Rejects list — explicit "create in Salesforce first" guidance
# ---------------------------------------------------------------------------

def test_rejects_list_renders_with_create_in_sf_guidance(client):
    plans = [
        (lambda s: s.startswith("SELECT COUNT"), ([], _scalar_row(0))),
        (lambda s: "FROM WIP_IMPORT_REJECT" in s and "ORDER BY ID" in s, ([], None)),
        (lambda s: "SELECT DISTINCT IMPORT_BATCH_ID" in s, ([], None)),
    ]
    app.dependency_overrides[get_db] = lambda: _fake_conn(plans)
    try:
        r = client.get("/data-review/rejects")
        assert r.status_code == 200
        body = r.text
        # Architect rule: no create-company button; Salesforce first.
        # WO-14A.5 softened the wording.
        assert "Fix or add the firm in Salesforce first" in body
        # Architect rule: no create-company button
        assert "create company" not in body.lower()
    finally:
        app.dependency_overrides.clear()


def test_rejects_list_filter_params_are_passed(client):
    """Filter dropdowns must round-trip — operator-typed firm should
    survive into the URL of pagination links."""
    plans = [
        (lambda s: s.startswith("SELECT COUNT"), ([], _scalar_row(0))),
        (lambda s: "FROM WIP_IMPORT_REJECT" in s and "ORDER BY ID" in s, ([], None)),
        (lambda s: "SELECT DISTINCT" in s, ([], None)),
    ]
    app.dependency_overrides[get_db] = lambda: _fake_conn(plans)
    try:
        r = client.get("/data-review/rejects?reject_reason=NO_COMPANY_MATCH&firm=hugh&status=NEW")
        assert r.status_code == 200
        body = r.text
        # Selected option survives the round-trip
        assert 'value="NO_COMPANY_MATCH" selected' in body
        assert 'value="hugh"' in body
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Placeholders — visible but clearly marked
# ---------------------------------------------------------------------------

def test_placeholder_page_renders_not_yet_built(client):
    # /companies is now a real page (WO-20); /admin remains a placeholder.
    r = client.get("/admin")
    assert r.status_code == 200
    assert "not yet built" in r.text


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def test_health(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.text == "ok"


# ---------------------------------------------------------------------------
# WO-14A.5 — human_status filter
# ---------------------------------------------------------------------------

def test_human_status_filter_maps_enums_to_business_labels():
    """The filter is the single point that turns DB enum values into
    operator-facing labels. If anyone adds a new enum value, ensure it
    either has a label here or falls through gracefully."""
    from src.wip_web.app import human_status
    assert human_status("PROPOSED")     == "Pending review"
    assert human_status("APPROVED")     == "Confirmed"
    assert human_status("REJECTED")     == "Rejected"
    assert human_status("NEEDS_REVIEW") == "Needs review"
    assert human_status("IGNORED_FALSE_POSITIVE") == "Not a duplicate"
    assert human_status("PRIMARY")      == "Primary (HQ)"
    assert human_status("FIRM_GROUP")   == "Firm group"
    # Unknown enums fall through (don't crash) so a new ENUM value doesn't
    # break the page; the operator sees the raw string and the dev adds
    # a label here.
    assert human_status("MADE_UP_ENUM") == "MADE_UP_ENUM"
    assert human_status(None)           == ""


def test_dashboard_uses_business_card_labels(client):
    """WO-14A.5 — dashboard cards should use the operator-facing
    wording. Catches accidental regressions back to internal jargon."""
    plans = [
        (lambda s: True, ([], _scalar_row(0))),
    ]
    app.dependency_overrides[get_db] = lambda: _fake_conn(plans)
    try:
        r = client.get("/")
        body = r.text
        # Business labels present
        assert "Firm groups awaiting review" in body
        assert "Possible duplicate accounts" in body
        assert "Mastercopy rows that didn't match" in body
        # Internal jargon NOT present
        assert "Import rejects (open)" not in body
        assert "Duplicate groups (open)" not in body
    finally:
        app.dependency_overrides.clear()
