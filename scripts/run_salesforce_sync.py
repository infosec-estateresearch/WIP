"""CLI entry point for the Salesforce → WIP sync (WO-10B).

Modes:
  --full           — fetch all Salesforce Accounts (reconciliation)
  --incremental    — fetch only those modified since last successful incremental

  --dry-run        — observe what would happen; no wip_company / candidate writes
  --apply          — execute the sync for real

Safeguards (same pattern as ERic's run_aggregation.py):
  - fcntl single-run lock (/tmp/eric-wip-sf-sync.lock) — concurrent invocations exit 2
  - non-zero exit on any failure (lock held, SF unreachable, DB write failed)
  - sync_run row recorded even on failure (status='failed' + error_message)
"""

from __future__ import annotations

import argparse
import fcntl
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.wip_app.db import wip_connection
from src.wip_app.sync.runner import run_sync


LOCK_PATH = "/tmp/eric-wip-sf-sync.lock"

EXIT_OK = 0
EXIT_LOCKED = 2
EXIT_FAILED = 3


def _acquire_lock():
    fd = open(LOCK_PATH, "w")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.stderr.write(f"another sync already holds {LOCK_PATH} — exiting\n")
        sys.exit(EXIT_LOCKED)
    try:
        fd.seek(0)
        fd.truncate()
        fd.write(str(os.getpid()))
        fd.flush()
    except OSError:
        pass
    return fd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wip.run_salesforce_sync")
    mode_g = parser.add_mutually_exclusive_group(required=True)
    mode_g.add_argument("--full", action="store_true", help="Fetch all Salesforce Accounts.")
    mode_g.add_argument("--incremental", action="store_true",
                        help="Fetch Accounts modified since last successful incremental sync.")
    apply_g = parser.add_mutually_exclusive_group(required=True)
    apply_g.add_argument("--dry-run", action="store_true", help="Inspect; no wip_company writes.")
    apply_g.add_argument("--apply", action="store_true", help="Apply the sync.")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    lock_fd = _acquire_lock()
    conn = wip_connection(autocommit=False)
    mode = "full" if args.full else "incremental"
    try:
        result = run_sync(conn, mode=mode, apply=args.apply)
        print()
        print(f"=== Salesforce → WIP sync ({mode.upper()}, "
              f"{'APPLY' if args.apply else 'DRY-RUN'}) ===")
        print(f"  sync_run_id              : {result.sync_run_id}")
        if result.incremental_since:
            print(f"  incremental_since        : {result.incremental_since}")
        print(f"  Accounts fetched         : {result.fetched}")
        if args.apply:
            print(f"  inserted                 : {result.inserted}")
            print(f"  updated                  : {result.updated}")
            print(f"  unchanged                : {result.unchanged}")
            print(f"  soft-deleted             : {result.soft_deleted}")
            print(f"  reactivated              : {result.reactivated}")
        else:
            print(f"  WOULD insert             : {result.inserted}")
            print(f"  WOULD update             : {result.updated}")
            print(f"  unchanged                : {result.unchanged}")
            print(f"  WOULD soft-delete        : {result.soft_deleted}")
            print(f"  WOULD reactivate         : {result.reactivated}")
        print(f"  canonical resolved       : {result.canonical_resolved}")
        print(f"  canonical unresolved     : {result.canonical_unresolved}")
        print(f"  canonical ambiguous      : {result.canonical_ambiguous}")
        if args.apply:
            print(f"  duplicate groups active  : {result.duplicates.groups_active}")
            print(f"  duplicate rows flagged   : {result.duplicates.rows_flagged}")
            print(f"  duplicate candidates +/Δ : {result.duplicates.candidates_inserted}"
                  f"/{result.duplicates.candidates_updated}")
        return EXIT_OK
    except Exception as e:
        logging.exception("sync failed: %s", e)
        return EXIT_FAILED
    finally:
        try:
            conn.close()
        except Exception:
            pass
        try:
            lock_fd.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main() or EXIT_OK)
