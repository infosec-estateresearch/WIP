"""Firm matcher tests."""

from __future__ import annotations

from unittest.mock import MagicMock

from src.wip_app.imports.firm_matcher import FirmMatcher


def _conn(rows: list[dict]):
    """Fake conn whose SELECT FROM wip_company returns the configured rows."""
    conn = MagicMock()

    def cursor():
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        def execute(sql, params=()):
            cur._fetched = rows
        cur.execute.side_effect = execute
        cur.fetchall.side_effect = lambda: cur._fetched
        return cur
    conn.cursor.side_effect = cursor
    return conn


def _row(id_, sf_id, name, normalised, is_dup=0, cc_id=None):
    return {
        "id": id_, "salesforce_account_id": sf_id,
        "salesforce_account_name": name, "account_name_normalised": normalised,
        "canonical_company_id": cc_id, "is_potential_duplicate": is_dup,
    }


def test_match_by_sf_account_id():
    m = FirmMatcher.load(_conn([_row(1, "001ABC", "Hugh James", "hugh james")]))
    r = m.match(sf_account_id="001ABC")
    assert r.result == "match" and r.wip_company_id == 1


def test_sf_id_given_but_not_in_wip_returns_no_match():
    """SF id is authoritative — if it's not in WIP, don't fall back to name."""
    m = FirmMatcher.load(_conn([_row(1, "001ABC", "Hugh James", "hugh james")]))
    r = m.match(sf_account_id="999ZZZ", firm_name="Hugh James")
    assert r.result == "no_match"


def test_exact_display_name_match():
    m = FirmMatcher.load(_conn([_row(1, "001A", "Hugh James", "hugh james")]))
    r = m.match(firm_name="Hugh James")
    assert r.result == "match" and r.wip_company_id == 1


def test_exact_normalised_match_when_display_misses():
    """CSV name 'Hugh James Solicitors' won't match display 'Hugh James',
    but the normalised form is identical so it resolves."""
    m = FirmMatcher.load(_conn([_row(1, "001A", "Hugh James", "hugh james")]))
    r = m.match(firm_name="Hugh James Solicitors")
    assert r.result == "match" and r.wip_company_id == 1


def test_no_match_when_unknown():
    m = FirmMatcher.load(_conn([_row(1, "001A", "Hugh James", "hugh james")]))
    r = m.match(firm_name="Totally Unknown Firm")
    assert r.result == "no_match"


def test_multi_match_returns_candidate_list():
    """Two SF Accounts share normalised key — caller must reject as ambiguous."""
    m = FirmMatcher.load(_conn([
        _row(1, "001A", "Hugh James",            "hugh james"),
        _row(2, "001B", "Hugh James Solicitors", "hugh james"),
    ]))
    r = m.match(firm_name="Hugh James")
    assert r.result == "multi_match"
    assert set(r.candidate_ids) == {1, 2}
    assert set(r.candidate_names) == {"Hugh James", "Hugh James Solicitors"}


def test_duplicate_flag_propagates_on_unambiguous_match():
    """Architect rule: import only if unambiguous; a single-match company
    that's flagged is_potential_duplicate still resolves."""
    m = FirmMatcher.load(_conn([_row(1, "001A", "Hugh James", "hugh james", is_dup=1)]))
    r = m.match(firm_name="Hugh James")
    assert r.result == "match"
    assert r.is_potential_duplicate is True


def test_empty_firm_name_is_no_match():
    m = FirmMatcher.load(_conn([_row(1, "001A", "Hugh James", "hugh james")]))
    assert m.match(firm_name="").result == "no_match"
    assert m.match(firm_name=None).result == "no_match"
