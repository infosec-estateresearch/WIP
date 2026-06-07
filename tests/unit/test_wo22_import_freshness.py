"""WO-22 — dashboard import-freshness health.

Pure-function tests (no DB): the GREEN < 24h / AMBER > 24h / RED > 7d (or never)
classification, so the dashboard can never show a false-green over a stale
Mastercopy import again.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from src.wip_web.routes.dashboard import _import_health


def _li(hours_ago):
    return {"status": "completed", "completed_at": datetime.now() - timedelta(hours=hours_ago)}


def test_green_under_24h():
    h = _import_health(_li(2))
    assert h["level"] == "green" and h["stale"] is False


def test_amber_between_1_and_7_days():
    assert _import_health(_li(48))["level"] == "amber"
    assert _import_health(_li(24 * 6))["level"] == "amber"


def test_red_over_7_days():
    h = _import_health(_li(24 * 8))
    assert h["level"] == "red" and h["stale"] is True


def test_red_when_no_completed_import():
    assert _import_health(None)["level"] == "red"
    assert _import_health({"status": "completed", "completed_at": None})["level"] == "red"


def test_boundary_just_under_24h_is_green():
    assert _import_health(_li(23))["level"] == "green"


def test_label_present():
    assert "ago" in _import_health(_li(3))["label"]
