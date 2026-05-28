"""CLI: WO-14A.3 — fold-in pass for under-membered PROPOSED groups.

Compares each live PROPOSED / NEEDS_REVIEW group against the corrected
WO-13 candidates CSV and INSERTs missing members under the same group.
Never touches APPROVED or REJECTED groups.

Usage:
  python -m scripts.fold_in_missing_group_members --dry-run
  python -m scripts.fold_in_missing_group_members --apply
  python -m scripts.fold_in_missing_group_members --apply \\
         --candidates /path/to/sf_canonical_alignment_candidates.csv

  --dry-run         : compute the plan; report what would be added; no writes
  --apply           : compute + INSERT missing members
  --candidates PATH : override the WO-13 candidates CSV
                      (default: /home/WIP/ops/evidence/WO-13/sf_canonical_alignment_candidates.csv)
  -v / --verbose

Safety:
  fcntl single-run lock (/tmp/wip-group-foldin.lock).
  Non-zero exit if any per-group apply fails.
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
from src.wip_app.groups.fold_in import apply_plan, build_plan


LOCK_PATH = "/tmp/wip-group-foldin.lock"
DEFAULT_CANDIDATES_PATH = "/home/WIP/ops/evidence/WO-13/sf_canonical_alignment_candidates.csv"


EXIT_OK = 0
EXIT_LOCKED = 2
EXIT_FAILED = 3


def _acquire_lock():
    fd = open(LOCK_PATH, "w")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.stderr.write(f"another fold-in run already holds {LOCK_PATH} — exiting\n")
        sys.exit(EXIT_LOCKED)
    try:
        fd.seek(0); fd.truncate(); fd.write(str(os.getpid())); fd.flush()
    except OSError:
        pass
    return fd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wip.fold_in_missing_group_members")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true",
                   help="Compute + report; no writes")
    g.add_argument("--apply", action="store_true",
                   help="Compute + INSERT missing members")
    parser.add_argument("--candidates", default=DEFAULT_CANDIDATES_PATH,
                        help="Path to corrected WO-13 candidates CSV")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    candidates_path = Path(args.candidates)
    if not candidates_path.exists():
        sys.stderr.write(f"candidates CSV not found: {candidates_path}\n")
        return EXIT_FAILED

    lock = _acquire_lock()
    conn = wip_connection(autocommit=False)
    try:
        result = build_plan(conn, candidates_path)

        if args.apply:
            apply_plan(conn, result)

        mode = "APPLY" if args.apply else "DRY-RUN"
        print()
        print(f"=== WO-14A.3 fold-in pass ({mode}) ===")
        print(f"  candidates source       : {candidates_path}")
        print(f"  groups examined         : {result.groups_examined}")
        print(f"  groups with missing     : {result.groups_with_missing}")
        print(f"  groups w/o cluster id   : {result.groups_skipped_no_cluster}")
        if args.apply:
            print(f"  members INSERTED        : {result.members_added}")
        else:
            print(f"  members WOULD INSERT    : "
                  f"{sum(len(p.missing_wip_companies) for p in result.per_group)}")
        print(f"  members skipped (in APPROVED elsewhere) : "
              f"{result.members_skipped_in_approved_elsewhere}")
        print(f"  members skipped (unknown SF id)         : "
              f"{result.members_skipped_unknown_sf}")

        # Per-group detail (only groups that actually changed)
        actionable = [p for p in result.per_group
                      if p.missing_wip_companies
                      or p.skipped_already_approved_elsewhere
                      or p.skipped_unknown_sf_ids]
        if actionable:
            print()
            print(f"  per-group detail ({len(actionable)} group(s) with changes/skips):")
            # Sort by biggest fold-in first
            actionable.sort(key=lambda p: -len(p.missing_wip_companies))
            for p in actionable[:25]:
                line = (
                    f"    group {p.group_id} ({p.group_name}) [{p.review_status}]: "
                    f"+{len(p.missing_wip_companies)} new"
                )
                if p.skipped_already_approved_elsewhere:
                    line += f", -{len(p.skipped_already_approved_elsewhere)} (in approved elsewhere)"
                if p.skipped_unknown_sf_ids:
                    line += f", -{len(p.skipped_unknown_sf_ids)} (unknown SF id)"
                print(line)
            if len(actionable) > 25:
                print(f"    ... (+{len(actionable) - 25} more)")

        if result.errors:
            print()
            print(f"  ERRORS ({len(result.errors)}):")
            for e in result.errors:
                print(f"    {e}")
            return EXIT_FAILED

        return EXIT_OK
    except Exception as e:
        logging.exception("fold-in failed: %s", e)
        return EXIT_FAILED
    finally:
        try: conn.close()
        except Exception: pass
        try: lock.close()
        except Exception: pass


if __name__ == "__main__":
    sys.exit(main() or EXIT_OK)
