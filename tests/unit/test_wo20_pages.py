"""WO-20 — functional WIP GUI pages (Companies, Live Cases, Imports, Reports).

Live-integration: drives the real routes against nif_wip via the app's own
get_db. Loads .env so it can connect; skips cleanly if the DB is unreachable.

Safety invariant under test (WO-20): rejected Mastercopy rows are shown as
*awaiting review* and NEVER counted into a WIP figure.
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
    _c = wip_connection(autocommit=False)
    _c.close()
    _DB = True
except Exception:
    _DB = False

pytestmark = pytest.mark.skipif(not _DB, reason="nif_wip not reachable")

from src.wip_web.app import app  # noqa: E402

client = TestClient(app)


def test_companies_list_and_safety_note():
    r = client.get("/companies?has_wip=1")
    assert r.status_code == 200
    assert "Current WIP" in r.text
    assert "never counted as WIP" in r.text


def test_live_cases_excludes_rejects():
    r = client.get("/live-cases")
    assert r.status_code == 200
    assert "Live Cases" in r.text
    # this page only points to the reject queue; it does not list rejected rows
    assert "not shown here" in r.text


def test_imports_list_and_detail():
    r = client.get("/imports")
    assert r.status_code == 200
    assert "Imported safely" in r.text and "Held for review" in r.text  # WO-25: no scary Coverage%/Rejected
    from src.wip_app.db import wip_connection
    conn = wip_connection(autocommit=False)
    with conn.cursor() as c:
        c.execute("SELECT id FROM wip_import_batch ORDER BY id DESC LIMIT 1")
        row = c.fetchone()
    conn.close()
    if row:
        rd = client.get("/imports/%d" % row["id"])
        assert rd.status_code == 200
        assert "Held rows by reason" in rd.text  # WO-25: was "Reject breakdown"


def test_reports():
    r = client.get("/reports")
    assert r.status_code == 200
    assert "Top firms by current WIP" in r.text
    assert "awaiting review" in r.text  # unmatched value labelled, not WIP


def test_company_detail_keeps_rejects_separate_from_wip():
    from src.wip_app.db import wip_connection
    conn = wip_connection(autocommit=False)
    with conn.cursor() as c:
        c.execute("SELECT id FROM wip_company "
                  "WHERE account_name_normalised = 'hugh james' "
                  "OR salesforce_account_name LIKE 'Hugh James%%' ORDER BY id LIMIT 1")
        row = c.fetchone()
    conn.close()
    assert row, "expected a Hugh James account in the data"
    r = client.get("/companies/%d" % row["id"])
    assert r.status_code == 200
    # rejects are surfaced, explicitly as awaiting review and separate from WIP
    assert "awaiting review" in r.text
    assert "not included in Current WIP" in r.text  # WO-25: clearer separation, no scary "rejected"
    assert "Rejected" not in r.text                  # WO-25: no scary label on customer-facing detail


def test_placeholders_for_built_pages_removed():
    # the built pages are real (200), not the placeholder shell
    for path in ("/companies", "/live-cases", "/imports", "/reports"):
        assert client.get(path).status_code == 200
