"""CLI for the Mastercopy → WIP import (WO-11).

Usage:
  python -m scripts.import_mastercopy_wip --source /path/to/MASTERCOPY-V2.xlsx --dry-run
  python -m scripts.import_mastercopy_wip --source /path/to/MASTERCOPY-V2.xlsx --apply

Safeguards (same pattern as the SF sync):
  - fcntl single-run lock (/tmp/wip-mastercopy-import.lock)
  - non-zero exit on any failure
  - wip_import_batch row recorded even on failure
  - dry-run rolls back any test writes but keeps the batch row visible
"""

from __future__ import annotations

import argparse
import fcntl
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.wip_app.db import wip_connection
from src.wip_app.imports.mastercopy_runner import run_import


LOCK_PATH = "/tmp/wip-mastercopy-import.lock"

EXIT_OK = 0
EXIT_LOCKED = 2
EXIT_FAILED = 3


def _acquire_lock():
    fd = open(LOCK_PATH, "w")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.stderr.write(f"another import already holds {LOCK_PATH} — exiting\n")
        sys.exit(EXIT_LOCKED)
    try:
        fd.seek(0); fd.truncate(); fd.write(str(os.getpid())); fd.flush()
    except OSError:
        pass
    return fd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wip.import_mastercopy")
    parser.add_argument("--source", required=True, help="Path to MASTERCOPY-V2.xlsx")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="Classify rows; no wip_case/financial/reject writes.")
    g.add_argument("--apply", action="store_true", help="Import for real.")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    lock = _acquire_lock()
    conn = wip_connection(autocommit=False)
    try:
        r = run_import(conn, source_path=args.source, apply=args.apply)
        print()
        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"=== Mastercopy → WIP import ({mode}) ===")
        print(f"  import_batch_id   : {r.import_batch_id}")
        print(f"  source file       : {r.source_file}")
        print(f"  source SHA256     : {r.source_hash[:16]}…")
        print(f"  rows seen         : {r.rows_seen}")
        if args.apply:
            print(f"  inserted          : {r.rows_inserted}")
            print(f"  updated           : {r.rows_updated}")
            print(f"  unchanged         : {r.rows_unchanged}")
            print(f"  rejected          : {r.rows_rejected}")
            print(f"  prior rejects RESOLVED : {r.rejects_resolved}")
        else:
            print(f"  WOULD insert      : {r.rows_inserted}")
            print(f"  WOULD update      : {r.rows_updated}")
            print(f"  unchanged         : {r.rows_unchanged}")
            print(f"  WOULD reject      : {r.rows_rejected}")
        if r.reject_breakdown:
            print(f"  reject breakdown  :")
            for reason, n in sorted(r.reject_breakdown.items(), key=lambda kv: -kv[1]):
                print(f"    {reason:30} {n}")
        return EXIT_OK
    except Exception as e:
        logging.exception("import failed: %s", e)
        return EXIT_FAILED
    finally:
        try: conn.close()
        except Exception: pass
        try: lock.close()
        except Exception: pass


if __name__ == "__main__":
    sys.exit(main() or EXIT_OK)
