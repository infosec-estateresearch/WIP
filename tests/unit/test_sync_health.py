"""WO-10C — sync health classification tests.

Verifies the architect's status thresholds:

  GREEN  incremental ≤ 2h  AND  full ≤ 36h  AND  no newer-failed-run
  AMBER  incremental > 2h  OR   full > 36h   (but newer failed run = RED)
  RED    incremental > 6h  OR   full > 7d    OR   latest run failed and
         no newer success of any type.

Duplicate groups are informational only — never affect the GREEN/AMBER/RED
status by themselves (architect rule: "Salesforce duplicates are data
quality issues, not sync availability issues.")
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from scripts import sync_health as mod


def _conn_with(
    *,
    last_incremental: datetime | None,
    last_full: datetime | None,
    last_failed: tuple | None = None,
    duplicate_groups: int = 0,
    duplicate_companies: int = 0,
    total_companies: int = 0,
    active_companies: int = 0,
    latest_counters: dict | None = None,
):
    """Build a fake pymysql connection whose SELECTs return the configured
    state — lets us drive every classification path deterministically.
    """
    counters = latest_counters or {}
    conn = MagicMock()
    state = {"queries": []}

    def cursor():
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)

        def execute(sql, params=()):
            state["queries"].append(sql)
            s = sql.strip().upper()
            if "GROUP BY SYNC_TYPE" in s:
                # last_success per sync_type
                rows = []
                if last_incremental:
                    rows.append({"sync_type": "salesforce_account_incremental",
                                 "last_at": last_incremental})
                if last_full:
                    rows.append({"sync_type": "salesforce_account_full",
                                 "last_at": last_full})
                cur._fetched = rows
            elif "STATUS = 'FAILED'" in s:
                if last_failed:
                    sync_type, started_at, err = last_failed
                    cur._fetched_one = {
                        "sync_type": sync_type, "started_at": started_at,
                        "error_message": err,
                    }
                else:
                    cur._fetched_one = None
            elif "STATUS = 'COMPLETED'" in s and "ORDER BY COMPLETED_AT DESC LIMIT 1" in s:
                cur._fetched_one = counters or None
            elif "WIP_COMPANY_DUPLICATE_CANDIDATE" in s:
                cur._fetched_one = {"n": duplicate_groups}
            elif "IS_POTENTIAL_DUPLICATE" in s:
                cur._fetched_one = {"n": duplicate_companies}
            elif "WIP_COMPANY" in s and "TOTAL" in s.replace(" ", ""):
                cur._fetched_one = {"total": total_companies, "active": active_companies}
        cur.execute.side_effect = execute
        cur.fetchall.side_effect = lambda: getattr(cur, "_fetched", [])
        cur.fetchone.side_effect = lambda: getattr(cur, "_fetched_one", None)
        return cur

    conn.cursor.side_effect = cursor
    return conn


def _hours_ago(h):
    return datetime.now(timezone.utc) - timedelta(hours=h)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def test_green_both_recent_no_failed():
    conn = _conn_with(
        last_incremental=_hours_ago(0.5),
        last_full=_hours_ago(2),
        last_failed=None,
        latest_counters={"sync_type": "salesforce_account_incremental", "sf_accounts_fetched": 100,
                         "sf_accounts_inserted": 0, "sf_accounts_updated": 1,
                         "sf_accounts_unchanged": 99, "sf_accounts_soft_deleted": 0,
                         "canonical_resolved": 93, "canonical_unresolved": 7},
    )
    h = mod.collect_health(conn)
    assert h.status == "GREEN"
    assert h.canonical_resolution_pct == 93.0


def test_amber_when_incremental_warm_stale():
    conn = _conn_with(
        last_incremental=_hours_ago(3),   # > 2h, < 6h
        last_full=_hours_ago(2),
        latest_counters={},
    )
    h = mod.collect_health(conn)
    assert h.status == "AMBER"
    assert any("warm-stale" in r for r in h.reasons)


def test_red_when_incremental_old():
    conn = _conn_with(
        last_incremental=_hours_ago(7),   # > 6h RED threshold
        last_full=_hours_ago(2),
        latest_counters={},
    )
    h = mod.collect_health(conn)
    assert h.status == "RED"


def test_red_when_full_older_than_7_days():
    conn = _conn_with(
        last_incremental=_hours_ago(1),
        last_full=_hours_ago(8 * 24),     # > 7 days
        latest_counters={},
    )
    h = mod.collect_health(conn)
    assert h.status == "RED"


def test_amber_when_full_between_36h_and_7d():
    conn = _conn_with(
        last_incremental=_hours_ago(1),
        last_full=_hours_ago(48),         # > 36h, < 7d
        latest_counters={},
    )
    h = mod.collect_health(conn)
    assert h.status == "AMBER"


def test_red_when_latest_failed_newer_than_latest_success():
    """The architect's specific rule: failed run newer than any successful run → RED."""
    conn = _conn_with(
        last_incremental=_hours_ago(0.5),
        last_full=_hours_ago(2),
        last_failed=("salesforce_account_incremental", _hours_ago(0.1), "boom"),
        latest_counters={},
    )
    h = mod.collect_health(conn)
    assert h.status == "RED"
    assert any("latest run failed" in r for r in h.reasons)


def test_failed_run_older_than_success_does_not_flip_red():
    """Old failure followed by a successful run is fine — RED only if failure
    is the most recent thing.
    """
    conn = _conn_with(
        last_incremental=_hours_ago(0.5),
        last_full=_hours_ago(2),
        last_failed=("salesforce_account_incremental", _hours_ago(10), "old failure"),
        latest_counters={},
    )
    h = mod.collect_health(conn)
    assert h.status == "GREEN"


def test_red_when_no_full_sync_ever_recorded():
    conn = _conn_with(
        last_incremental=_hours_ago(0.5),
        last_full=None,
        latest_counters={},
    )
    h = mod.collect_health(conn)
    assert h.status == "RED"
    assert any("no successful full" in r for r in h.reasons)


def test_red_when_no_incremental_ever_recorded():
    conn = _conn_with(
        last_incremental=None,
        last_full=_hours_ago(2),
        latest_counters={},
    )
    h = mod.collect_health(conn)
    assert h.status == "RED"


# ---------------------------------------------------------------------------
# Duplicate groups — informational ONLY, never RED on their own
# ---------------------------------------------------------------------------

def test_duplicate_groups_dont_make_health_red():
    """100 active duplicate groups + everything else healthy = still GREEN."""
    conn = _conn_with(
        last_incremental=_hours_ago(0.5),
        last_full=_hours_ago(2),
        duplicate_groups=100,
        duplicate_companies=250,
        latest_counters={},
    )
    h = mod.collect_health(conn)
    assert h.status == "GREEN"
    assert h.duplicate_groups_active == 100
    assert h.duplicate_companies_flagged == 250


def test_duplicate_count_surfaced_in_output_even_when_zero():
    conn = _conn_with(
        last_incremental=_hours_ago(0.5),
        last_full=_hours_ago(2),
        duplicate_groups=0,
        latest_counters={},
    )
    h = mod.collect_health(conn)
    assert h.duplicate_groups_active == 0


# ---------------------------------------------------------------------------
# Strict-mode exit codes
# ---------------------------------------------------------------------------

def test_strict_mode_exit_codes(monkeypatch):
    """--strict → 0/1/2 for GREEN/AMBER/RED so a cron can alert on AMBER."""
    fake_states = {
        "GREEN": mod.SyncHealth(status="GREEN", reasons=[], latest_incremental_at=None,
                                 latest_incremental_age_hours=None, latest_full_at=None,
                                 latest_full_age_hours=None, latest_failed_at=None,
                                 latest_failed_sync_type=None, latest_failed_error=None,
                                 latest_run_counters={}, duplicate_groups_active=0,
                                 duplicate_companies_flagged=0, canonical_resolution_pct=None,
                                 total_wip_companies=0, active_wip_companies=0),
    }
    fake_states["AMBER"] = fake_states["GREEN"].__class__(**{**fake_states["GREEN"].__dict__, "status": "AMBER"})
    fake_states["RED"]   = fake_states["GREEN"].__class__(**{**fake_states["GREEN"].__dict__, "status": "RED"})
    monkeypatch.setattr(mod, "wip_connection", lambda *_a, **_kw: MagicMock(close=lambda: None))
    for status, expected_code in [("GREEN", 0), ("AMBER", 1), ("RED", 2)]:
        monkeypatch.setattr(mod, "collect_health", lambda conn, s=status: fake_states[s])
        assert mod.main(["--strict"]) == expected_code


# ---------------------------------------------------------------------------
# Systemd unit file shape — parse-as-text
# ---------------------------------------------------------------------------

import configparser
from pathlib import Path

UNIT_DIR = Path("/home/WIP/infra/systemd")


@pytest.mark.parametrize("unit_name,expected_in_exec", [
    ("wip-salesforce-sync.service", "--incremental --apply"),
    ("wip-salesforce-full-sync.service", "--full --apply"),
])
def test_service_unit_calls_correct_sync_command(unit_name, expected_in_exec):
    p = UNIT_DIR / unit_name
    assert p.is_file(), f"missing systemd unit: {p}"
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.read(p)
    exec_start = cp["Service"]["ExecStart"]
    assert "run_salesforce_sync" in exec_start
    assert expected_in_exec in exec_start


@pytest.mark.parametrize("timer_name,expected_calendar_substr", [
    ("wip-salesforce-sync.timer", "hourly"),
    ("wip-salesforce-full-sync.timer", "03:30:00 UTC"),
])
def test_timer_unit_uses_expected_calendar(timer_name, expected_calendar_substr):
    p = UNIT_DIR / timer_name
    assert p.is_file()
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.read(p)
    assert "Timer" in cp.sections()
    cal = cp["Timer"]["OnCalendar"]
    assert expected_calendar_substr in cal


def test_timer_unit_targets_correct_service_via_naming_convention():
    """systemd convention: foo.timer fires foo.service unless Unit= overrides.
    We don't override, so each timer fires its same-stem service."""
    for stem in ("wip-salesforce-sync", "wip-salesforce-full-sync"):
        assert (UNIT_DIR / f"{stem}.timer").is_file()
        assert (UNIT_DIR / f"{stem}.service").is_file()


def test_both_services_use_the_same_lock_file_via_shared_runner():
    """The architect's overlap protection rule: full and incremental must
    share one lock so they can never race. Both services exec the same
    run_salesforce_sync.py — which has exactly one LOCK_PATH constant.
    """
    from scripts.run_salesforce_sync import LOCK_PATH
    assert LOCK_PATH == "/tmp/wip-salesforce-sync.lock", \
        "lock path drift — overlap protection assumes both services use this exact path"
