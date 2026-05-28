"""Migration runner tests (architect closure #9).

Covers the discovery / drift / idempotency / dry-run logic without needing
a real database — patches the connection-bound execute path.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts import run_migration as mod


def _make_conn():
    """Fake pymysql connection that records every execute and answers SELECT
    against an in-memory schema_migration map.
    """
    conn = MagicMock()
    applied: dict[str, str] = {}
    executed = []

    def cursor():
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)

        def execute(sql, params=()):
            executed.append((sql, params))
            s = sql.strip().upper()
            if "CREATE TABLE IF NOT EXISTS SCHEMA_MIGRATION" in s:
                cur._fetched = []
            elif s.startswith("SELECT FILENAME, CHECKSUM FROM SCHEMA_MIGRATION"):
                cur._fetched = [{"filename": k, "checksum": v} for k, v in applied.items()]
            elif s.startswith("INSERT INTO SCHEMA_MIGRATION"):
                applied[params[0]] = params[1]
        cur.execute.side_effect = execute
        cur.fetchall.side_effect = lambda: getattr(cur, "_fetched", [])
        return cur

    conn.cursor.side_effect = cursor
    conn.commit = MagicMock()
    conn.rollback = MagicMock()
    conn.applied = applied
    conn.executed = executed
    return conn


@pytest.fixture
def migrations_dir(tmp_path, monkeypatch):
    """Point the runner at a temp directory so test files don't collide with
    real migrations.
    """
    d = tmp_path / "migrations"
    d.mkdir()
    monkeypatch.setattr(mod, "MIGRATIONS_DIR", d)
    return d


def _write(d: Path, name: str, sql: str) -> Path:
    f = d / name
    f.write_text(sql)
    return f


# ---------------------------------------------------------------------------
# Discovery / status
# ---------------------------------------------------------------------------

def test_no_migrations_no_op(migrations_dir):
    conn = _make_conn()
    assert mod.apply_pending(conn, apply=True) == mod.EXIT_OK
    # no INSERT INTO schema_migration happened
    inserts = [e for e in conn.executed if "INSERT INTO SCHEMA_MIGRATION" in e[0].upper()]
    assert inserts == []


def test_discovery_orders_numerically(migrations_dir):
    _write(migrations_dir, "002_b.sql", "CREATE TABLE b (id INT)")
    _write(migrations_dir, "001_a.sql", "CREATE TABLE a (id INT)")
    _write(migrations_dir, "003_c.sql", "CREATE TABLE c (id INT)")
    files = mod._discover_migrations()
    assert [f.name for f in files] == ["001_a.sql", "002_b.sql", "003_c.sql"]


def test_discovery_ignores_unnumbered_files(migrations_dir):
    """Only NNN_*.sql gets picked up — README.md / .gitkeep / bootstrap.sql
    must NOT be treated as migrations.
    """
    _write(migrations_dir, "001_a.sql", "CREATE TABLE a (id INT)")
    _write(migrations_dir, "README.md", "hi")
    _write(migrations_dir, ".gitkeep", "")
    _write(migrations_dir, "bootstrap.sql", "GRANT ALL")
    assert [f.name for f in mod._discover_migrations()] == ["001_a.sql"]


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def test_apply_runs_each_pending_in_order(migrations_dir):
    _write(migrations_dir, "001_a.sql", "CREATE TABLE a (id INT)")
    _write(migrations_dir, "002_b.sql", "CREATE TABLE b (id INT)")
    conn = _make_conn()
    rc = mod.apply_pending(conn, apply=True)
    assert rc == mod.EXIT_OK
    # Both migrations recorded in schema_migration
    assert "001_a.sql" in conn.applied
    assert "002_b.sql" in conn.applied


def test_apply_skips_already_applied(migrations_dir):
    f = _write(migrations_dir, "001_a.sql", "CREATE TABLE a (id INT)")
    conn = _make_conn()
    # Pre-mark as applied with the matching checksum
    conn.applied["001_a.sql"] = mod._sha256(f.read_text())
    rc = mod.apply_pending(conn, apply=True)
    assert rc == mod.EXIT_OK
    # No new INSERT INTO schema_migration this run
    fresh_inserts = [e for e in conn.executed
                     if "INSERT INTO SCHEMA_MIGRATION" in e[0].upper()]
    assert fresh_inserts == []


def test_dry_run_does_not_record_or_execute_ddl(migrations_dir):
    _write(migrations_dir, "001_a.sql", "CREATE TABLE a (id INT)")
    conn = _make_conn()
    rc = mod.apply_pending(conn, apply=False)
    assert rc == mod.EXIT_OK
    # No INSERT INTO schema_migration
    assert conn.applied == {}
    # And no CREATE TABLE a (id INT) was executed
    ddl = [e for e in conn.executed if "CREATE TABLE A" in e[0].upper()]
    assert ddl == []


# ---------------------------------------------------------------------------
# Drift detection — non-negotiable
# ---------------------------------------------------------------------------

def test_drift_detected_when_applied_file_is_edited(migrations_dir):
    """Architect-style protection: editing an already-applied migration
    must hard-fail rather than silently re-applying (or silently ignoring).
    """
    f = _write(migrations_dir, "001_a.sql", "CREATE TABLE a (id INT)")
    conn = _make_conn()
    # Pretend the file was applied with an OLD checksum (= different content)
    conn.applied["001_a.sql"] = "x" * 64  # wrong checksum
    rc = mod.apply_pending(conn, apply=True)
    assert rc == mod.EXIT_DRIFT
    # And no new INSERT happened despite apply=True
    fresh = [e for e in conn.executed
             if "INSERT INTO SCHEMA_MIGRATION" in e[0].upper()]
    assert fresh == []


# ---------------------------------------------------------------------------
# --only filter
# ---------------------------------------------------------------------------

def test_only_filter_runs_just_one_file(migrations_dir):
    _write(migrations_dir, "001_a.sql", "CREATE TABLE a (id INT)")
    _write(migrations_dir, "002_b.sql", "CREATE TABLE b (id INT)")
    conn = _make_conn()
    rc = mod.apply_pending(conn, apply=True, only="002_b.sql")
    assert rc == mod.EXIT_OK
    assert "001_a.sql" not in conn.applied
    assert "002_b.sql" in conn.applied
