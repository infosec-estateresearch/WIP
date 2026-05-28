"""CLI: generate the operator review pack for proposed company groups.

Usage:
  python -m scripts.generate_company_group_review_pack --dry-run
  python -m scripts.generate_company_group_review_pack --output PATH \\
                                                       [--apply-seed]

  --dry-run         : print summary only; no CSV written, no DB writes
  --output PATH     : write the operator review pack CSV
  --apply-seed      : INSERT the PROPOSED groups/members into the DB
                      (required before --output if you want the operator
                      decisions to land via apply_company_group_decisions
                      against real group rows)
  --candidates PATH : override path to WO-13 candidates CSV
                      (default: /home/WIP/ops/evidence/WO-13/sf_canonical_alignment_candidates.csv)
  --min-impact N    : impact_score threshold (default: 20.0)

Safety:
  fcntl single-run lock (/tmp/wip-group-seed.lock).
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
from src.wip_app.groups.seed import (
    MIN_IMPACT_SCORE,
    insert_proposed,
    propose_groups,
    write_review_pack_csv,
)


LOCK_PATH = "/tmp/wip-group-seed.lock"
DEFAULT_CANDIDATES_PATH = "/home/WIP/ops/evidence/WO-13/sf_canonical_alignment_candidates.csv"


EXIT_OK = 0
EXIT_LOCKED = 2
EXIT_FAILED = 3


def _acquire_lock():
    fd = open(LOCK_PATH, "w")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.stderr.write(f"another seed run already holds {LOCK_PATH} — exiting\n")
        sys.exit(EXIT_LOCKED)
    try:
        fd.seek(0); fd.truncate(); fd.write(str(os.getpid())); fd.flush()
    except OSError:
        pass
    return fd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wip.generate_company_group_review_pack")
    parser.add_argument("--candidates", default=DEFAULT_CANDIDATES_PATH,
                        help="Path to WO-13 candidates CSV")
    parser.add_argument("--min-impact", type=float, default=MIN_IMPACT_SCORE,
                        help="Skip clusters below this impact_score")
    parser.add_argument("--output", default=None,
                        help="Write review pack CSV to this path")
    parser.add_argument("--apply-seed", action="store_true",
                        help="INSERT proposed groups/members into the DB. "
                             "Without this flag, the script only computes "
                             "and (optionally) writes the review CSV.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print summary only; no CSV, no DB writes")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.dry_run and (args.output or args.apply_seed):
        sys.stderr.write("--dry-run is mutually exclusive with --output / --apply-seed\n")
        return EXIT_FAILED

    candidates = Path(args.candidates)
    if not candidates.exists():
        sys.stderr.write(f"candidates CSV not found: {candidates}\n")
        return EXIT_FAILED

    lock = _acquire_lock()
    conn = wip_connection(autocommit=False)
    try:
        seeded = propose_groups(conn, candidates, min_impact_score=args.min_impact)

        # Console summary
        actionable = [s for s in seeded if s.skipped_reason is None]
        skipped = [s for s in seeded if s.skipped_reason is not None]
        skip_reasons: dict[str, int] = {}
        for s in skipped:
            skip_reasons[s.skipped_reason] = skip_reasons.get(s.skipped_reason, 0) + 1

        warnings: dict[str, int] = {}
        for s in actionable:
            if s.seed_warning:
                warnings[s.seed_warning] = warnings.get(s.seed_warning, 0) + 1

        print()
        print("=== WO-14A.1 group seed proposer ===")
        print(f"  candidates source : {candidates}")
        print(f"  impact threshold  : {args.min_impact}")
        print(f"  proposed (new)    : {len(actionable)}")
        print(f"  skipped           : {len(skipped)}")
        for reason, n in sorted(skip_reasons.items(), key=lambda kv: -kv[1]):
            print(f"    {reason:38} {n}")
        if warnings:
            print(f"  seed warnings on actionable:")
            for tag, n in sorted(warnings.items(), key=lambda kv: -kv[1]):
                print(f"    {tag:38} {n}")

        if args.dry_run:
            print()
            print("DRY-RUN — no CSV written, no DB writes.")
            return EXIT_OK

        if args.apply_seed:
            n = insert_proposed(conn, seeded)
            print(f"  INSERTED {n} PROPOSED group(s) into wip_company_group")

        if args.output:
            out = Path(args.output)
            write_review_pack_csv(seeded, out)
            print(f"  wrote review CSV: {out}")

        return EXIT_OK
    except Exception as e:
        logging.exception("seed run failed: %s", e)
        return EXIT_FAILED
    finally:
        try: conn.close()
        except Exception: pass
        try: lock.close()
        except Exception: pass


if __name__ == "__main__":
    sys.exit(main() or EXIT_OK)
