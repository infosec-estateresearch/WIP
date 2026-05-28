"""WIP migration runner — applies numbered .sql files idempotently.

Each migration file is one DDL statement (CREATE TABLE / CREATE VIEW / ...).
Applied migrations are recorded in `schema_migration` with the file's
SHA-256 checksum so we can detect drift if a previously-applied file is
later edited.

Usage:
    python -m scripts.run_migration --dry-run
    python -m scripts.run_migration --apply
    python -m scripts.run_migration --apply --only 002_wip_sync_run.sql
    python -m scripts.run_migration --status
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import pymysql

logger = logging.getLogger("wip.migration")

MIGRATIONS_DIR = Path("/home/WIP/migrations")

EXIT_OK = 0
EXIT_DRIFT = 2
EXIT_FAILED = 3


# ---------------------------------------------------------------------------
# DB connection — uses NIF_WIP_DB_* env (wip_app user)
# ---------------------------------------------------------------------------

def _connect():
    """Connect to nif_wip as wip_app. Reads env from /home/WIP/.env."""
    _load_dotenv("/home/WIP/.env")
    return pymysql.connect(
        host=os.environ["NIF_WIP_DB_HOST"],
        port=int(os.environ["NIF_WIP_DB_PORT"]),
        user=os.environ["NIF_WIP_DB_USER"],
        password=os.environ["NIF_WIP_DB_PASS"],
        database=os.environ["NIF_WIP_DB_NAME"],
        charset="utf8mb4",
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )


def _load_dotenv(path: str) -> None:
    """Minimal .env loader — no python-dotenv dependency."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        os.environ.setdefault(k, v)


# ---------------------------------------------------------------------------
# Migration discovery + bookkeeping
# ---------------------------------------------------------------------------

def _discover_migrations(only: str | None = None) -> list[Path]:
    """Numerically-ordered .sql files in MIGRATIONS_DIR."""
    if not MIGRATIONS_DIR.is_dir():
        return []
    files = sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))
    if only:
        files = [f for f in files if f.name == only]
    return files


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _ensure_schema_migration_table(conn) -> None:
    """Create the bookkeeping table if it doesn't exist yet. Idempotent."""
    with conn.cursor() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS schema_migration (
                id          BIGINT AUTO_INCREMENT PRIMARY KEY,
                filename    VARCHAR(255) NOT NULL,
                checksum    CHAR(64)     NOT NULL,
                applied_at  DATETIME(6)  NOT NULL,
                duration_ms INT          NOT NULL DEFAULT 0,
                UNIQUE KEY uq_schema_migration_filename (filename)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
    conn.commit()


def _applied_migrations(conn) -> dict[str, str]:
    """filename -> stored_checksum for everything already applied."""
    with conn.cursor() as c:
        c.execute("SELECT filename, checksum FROM schema_migration")
        return {r["filename"]: r["checksum"] for r in c.fetchall()}


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def apply_pending(conn, *, apply: bool, only: str | None = None) -> int:
    """Apply (or dry-run) migrations. Returns counter dict-like exit code.

    Drift detection: if a migration file's current checksum differs from the
    stored one, exit EXIT_DRIFT — never silently re-apply a changed file.
    """
    _ensure_schema_migration_table(conn)
    applied = _applied_migrations(conn)
    files = _discover_migrations(only=only)

    if not files:
        print("(no migration files found)")
        return EXIT_OK

    drift = []
    pending = []
    for f in files:
        text = f.read_text()
        cs = _sha256(text)
        prev = applied.get(f.name)
        if prev is None:
            pending.append((f, text, cs))
        elif prev != cs:
            drift.append((f.name, prev, cs))

    if drift:
        print("DRIFT DETECTED — these files have changed since they were applied:")
        for name, prev, cur in drift:
            print(f"  {name}  stored={prev[:12]}  current={cur[:12]}")
        print("Refusing to proceed. Create a NEW numbered migration instead of "
              "editing an applied one.")
        return EXIT_DRIFT

    if not pending:
        print(f"(all {len(files)} migrations already applied; nothing to do)")
        return EXIT_OK

    mode = "APPLY" if apply else "DRY-RUN"
    print(f"=== {mode} — {len(pending)} pending migration(s) ===")
    for f, text, cs in pending:
        print(f"  {f.name}  ({len(text)} bytes, checksum {cs[:12]})")
        if not apply:
            continue
        try:
            t0 = datetime.now(timezone.utc)
            with conn.cursor() as c:
                c.execute(text)
            with conn.cursor() as c:
                c.execute(
                    "INSERT INTO schema_migration "
                    "(filename, checksum, applied_at, duration_ms) "
                    "VALUES (%s, %s, %s, %s)",
                    (f.name, cs, t0, int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)),
                )
            conn.commit()
            print(f"    ✓ applied")
        except Exception as e:
            conn.rollback()
            logger.exception("failed to apply %s", f.name)
            print(f"    ✗ FAILED: {e}")
            return EXIT_FAILED

    return EXIT_OK


def show_status(conn) -> int:
    """Print every migration file and whether it's been applied."""
    _ensure_schema_migration_table(conn)
    applied = _applied_migrations(conn)
    files = _discover_migrations()
    print(f"{'STATUS':10}  {'CHECKSUM':14}  FILENAME")
    for f in files:
        text = f.read_text()
        cs = _sha256(text)
        prev = applied.get(f.name)
        if prev is None:
            status = "PENDING"
        elif prev == cs:
            status = "APPLIED"
        else:
            status = "DRIFT!"
        print(f"{status:10}  {cs[:12]:14}  {f.name}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="wip.run_migration")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="Show pending migrations without applying.")
    g.add_argument("--apply", action="store_true", help="Apply pending migrations.")
    g.add_argument("--status", action="store_true", help="Show applied/pending/drift for every file.")
    parser.add_argument("--only", default=None, help="Apply only a specific filename (e.g. 002_wip_sync_run.sql).")
    args = parser.parse_args(argv)

    conn = _connect()
    try:
        if args.status:
            return show_status(conn)
        return apply_pending(conn, apply=args.apply, only=args.only)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
