"""CLI: apply operator decisions on the company-group review pack.

Usage:
  python -m scripts.apply_company_group_decisions --input PATH --dry-run
  python -m scripts.apply_company_group_decisions --input PATH --apply

  --input PATH  : marked review CSV (operator_decision column populated)
  --dry-run     : preview; rolls back every per-row transaction
  --apply       : commit changes per row (failure on one row does not
                  roll back earlier rows)

Safety:
  fcntl single-run lock (/tmp/wip-group-apply.lock).
  Non-zero exit if any row failed (so a CI/automation wrapper notices).

MVP `operator_decision` values: APPROVE_GROUP / REJECT_GROUP /
NEEDS_MORE_REVIEW. Unknown decisions are reported and skipped.
"""

from __future__ import annotations

import argparse
import fcntl
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.wip_app.db import wip_connection
from src.wip_app.groups.apply import apply_decisions


LOCK_PATH = "/tmp/wip-group-apply.lock"

EXIT_OK = 0
EXIT_LOCKED = 2
EXIT_FAILED = 3


def _acquire_lock():
    fd = open(LOCK_PATH, "w")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.stderr.write(f"another apply run already holds {LOCK_PATH} — exiting\n")
        sys.exit(EXIT_LOCKED)
    try:
        fd.seek(0); fd.truncate(); fd.write(str(os.getpid())); fd.flush()
    except OSError:
        pass
    return fd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wip.apply_company_group_decisions")
    parser.add_argument("--input", required=True, help="Marked review CSV")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true",
                   help="Preview; rolls back every per-row transaction")
    g.add_argument("--apply", action="store_true",
                   help="Commit changes per row")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    input_path = Path(args.input)
    if not input_path.exists():
        sys.stderr.write(f"input CSV not found: {input_path}\n")
        return EXIT_FAILED

    lock = _acquire_lock()
    conn = wip_connection(autocommit=False)
    try:
        result = apply_decisions(conn, input_path, dry_run=args.dry_run)

        mode = "DRY-RUN" if args.dry_run else "APPLY"
        print()
        print(f"=== WO-14A.1 group decision applier ({mode}) ===")
        print(f"  input             : {input_path}")
        print(f"  rows seen         : {result.rows_seen}")
        print(f"  groups APPROVED   : {result.groups_approved}")
        print(f"  groups REJECTED   : {result.groups_rejected}")
        print(f"  groups NEEDS_REV. : {result.groups_needs_review}")
        print(f"  rows skipped      : {result.rows_skipped}")
        print(f"  rows failed       : {result.rows_failed}")
        if result.decisions_unknown:
            print(f"  unknown decisions :")
            for u in result.decisions_unknown:
                print(f"    {u}")
        if result.errors:
            print(f"  errors            :")
            for err in result.errors:
                print(f"    {err}")

        # Non-zero exit if any row failed so CI/automation notices.
        if result.rows_failed > 0:
            return EXIT_FAILED
        return EXIT_OK
    except Exception as e:
        logging.exception("apply run failed: %s", e)
        return EXIT_FAILED
    finally:
        try: conn.close()
        except Exception: pass
        try: lock.close()
        except Exception: pass


if __name__ == "__main__":
    sys.exit(main() or EXIT_OK)
