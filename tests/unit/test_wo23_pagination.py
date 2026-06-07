"""WO-23 Part C — pagination is safe and preserves context.

Every list page shares the `_pagination.html` pager. The routes clamp `page`,
so an out-of-range page (0, negative, far past the end) renders the nearest
valid page instead of a 422/500. Next/Previous carry the active filters. Skips
if nif_wip is unreachable.
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

# The five paginated list pages, each with a representative filter set.
PAGES = [
    "/companies?has_wip=1",
    "/live-cases",
    "/data-review/rejects?status=ALL",
    "/data-review/groups?status=ALL",
    "/data-review/duplicates?status=ALL",
]


@pytest.mark.parametrize("path", PAGES)
@pytest.mark.parametrize("page", [0, -5, 1, 999999])
def test_out_of_range_page_never_crashes(path, page):
    sep = "&" if "?" in path else "?"
    r = client.get(f"{path}{sep}page={page}")
    assert r.status_code == 200          # clamped, never 422/500


def test_next_link_preserves_filter():
    # A filtered rejects view deep enough to have a second page.
    r = client.get("/data-review/rejects?reject_reason=NO_COMPANY_MATCH&status=ALL")
    assert r.status_code == 200
    if "page=2" in r.text:               # only assert when a 2nd page exists
        # the Next link keeps the reason filter alongside the page bump
        assert "reject_reason=NO_COMPANY_MATCH" in r.text
        import re
        nexts = re.findall(r'href="\?[^"]*page=2"', r.text)
        assert nexts and "reject_reason=NO_COMPANY_MATCH" in nexts[0]


def test_pager_macro_renders_single_nav():
    # Groups list uses the shared macro — exactly one pager nav when multi-page.
    r = client.get("/data-review/groups?status=ALL")
    assert r.status_code == 200
    # macro emits the "page X of Y" marker; never more than one pager block
    assert r.text.count('aria-label="pagination"') <= 1
