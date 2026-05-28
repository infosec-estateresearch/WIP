"""CLI: export the operator review pack for the live company-group queue.

Unlike `generate_company_group_review_pack.py` (which proposes NEW groups
from the WO-13 cluster CSV), this script reads the LIVE PROPOSED /
NEEDS_REVIEW groups already seeded into nif_wip and packages them into
a ranked operator review CSV — joined with the WO-13 impact signals so
operators see the high-value clusters first.

Usage:
  python -m scripts.export_groups_for_review --output PATH
  python -m scripts.export_groups_for_review --output PATH --top 50
  python -m scripts.export_groups_for_review --output PATH --include-needs-review
  python -m scripts.export_groups_for_review --output PATH \\
                                              --candidates /path/to/WO-13.csv

  --output PATH               : write the review CSV to this path
  --top N                     : keep only the top N by impact_score (default: all)
  --include-needs-review      : include NEEDS_REVIEW groups too (default: PROPOSED only)
  --candidates PATH           : path to WO-13 candidates CSV
                                (default: /home/WIP/ops/evidence/WO-13/sf_canonical_alignment_candidates.csv)

Output schema matches `apply_company_group_decisions` — operator fills in
the three blank columns (operator_decision, set_primary_sf_id,
operator_note) and runs that script.

Safety:
  Read-only DB access (no writes); no fcntl lock needed.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.wip_app.db import wip_connection


DEFAULT_CANDIDATES_PATH = "/home/WIP/ops/evidence/WO-13/sf_canonical_alignment_candidates.csv"

EXIT_OK = 0
EXIT_FAILED = 3


def _load_wo13_impact(candidates_path: Path) -> dict[str, dict]:
    """Load the WO-13 candidates CSV into {cluster_id: row}. The
    source_cluster_id on each PROPOSED group is the join key."""
    out: dict[str, dict] = {}
    with open(candidates_path, newline="") as f:
        for row in csv.DictReader(f):
            cid = row.get("cluster_id")
            if cid:
                out[cid] = row
    return out


def _load_live_groups(conn, *, include_needs_review: bool) -> list[dict]:
    """One row per PROPOSED (+ optionally NEEDS_REVIEW) group with its
    members and live current-WIP roll-up."""
    statuses = ["PROPOSED"]
    if include_needs_review:
        statuses.append("NEEDS_REVIEW")
    placeholders = ",".join(["%s"] * len(statuses))
    with conn.cursor() as c:
        c.execute(
            f"""
            SELECT g.id              AS group_id,
                   g.group_name,
                   g.group_name_normalised,
                   g.review_status,
                   g.group_type,
                   g.seed_warning,
                   g.source_cluster_id,
                   g.primary_canonical_company_id,
                   COUNT(DISTINCT m.id)                       AS member_count,
                   GROUP_CONCAT(DISTINCT m.wip_company_id         ORDER BY m.id SEPARATOR ' | ') AS member_wip_ids,
                   GROUP_CONCAT(DISTINCT m.salesforce_account_id  ORDER BY m.id SEPARATOR ' | ') AS member_sf_ids,
                   GROUP_CONCAT(DISTINCT m.canonical_company_id   ORDER BY m.id SEPARATOR ' | ') AS member_canonical_ids,
                   COALESCE(SUM(cs.current_wip_value), 0)     AS group_current_wip,
                   COALESCE(SUM(cs.current_wip_count), 0)     AS group_active_case_count
              FROM wip_company_group g
              JOIN wip_company_group_member m ON m.group_id = g.id
              LEFT JOIN v_eric_wip_company_summary cs ON cs.wip_company_id = m.wip_company_id
             WHERE g.review_status IN ({placeholders})
             GROUP BY g.id, g.group_name, g.group_name_normalised,
                      g.review_status, g.group_type, g.seed_warning,
                      g.source_cluster_id, g.primary_canonical_company_id
            """,
            statuses,
        )
        groups = list(c.fetchall())

        # Pull display names per wip_company for the operator
        member_names: dict[int, str] = {}
        wip_ids: set[int] = set()
        for g in groups:
            if g.get("member_wip_ids"):
                for s in g["member_wip_ids"].split("|"):
                    s = s.strip()
                    if s:
                        try:
                            wip_ids.add(int(s))
                        except ValueError:
                            pass
        if wip_ids:
            in_clause = ",".join(["%s"] * len(wip_ids))
            c.execute(
                f"SELECT id, salesforce_account_name, billing_city "
                f"FROM wip_company WHERE id IN ({in_clause})",
                list(wip_ids),
            )
            for r in c.fetchall():
                city = r["billing_city"] or ""
                member_names[int(r["id"])] = (
                    f"{r['salesforce_account_name']}" + (f" [{city}]" if city else "")
                )
        for g in groups:
            ids = [int(x) for x in (g["member_wip_ids"] or "").split("|") if x.strip()]
            g["member_account_names"] = " | ".join(member_names.get(i, "?") for i in ids)
    return groups


def _suggested_primary_sf_id(conn, group_id: int) -> str | None:
    """Pick the SF Account most likely to be the headquarters: the member
    with current_wip_value > 0 (operator can override). Falls back to the
    SF id whose city name matches the group's first member if any."""
    with conn.cursor() as c:
        c.execute(
            """
            SELECT m.salesforce_account_id, cs.current_wip_value
              FROM wip_company_group_member m
              LEFT JOIN v_eric_wip_company_summary cs
                ON cs.wip_company_id = m.wip_company_id
             WHERE m.group_id = %s
             ORDER BY (cs.current_wip_value IS NULL),
                      cs.current_wip_value DESC, m.id ASC
             LIMIT 1
            """,
            (group_id,),
        )
        r = c.fetchone()
        return r["salesforce_account_id"] if r else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wip.export_groups_for_review")
    parser.add_argument("--output", required=True, help="Output CSV path")
    parser.add_argument("--top", type=int, default=None,
                        help="Limit to top N groups by impact_score")
    parser.add_argument("--include-needs-review", action="store_true",
                        help="Also include NEEDS_REVIEW groups (default: PROPOSED only)")
    parser.add_argument("--candidates", default=DEFAULT_CANDIDATES_PATH,
                        help="Path to WO-13 candidates CSV (for impact_score join)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    candidates_path = Path(args.candidates)
    impact_by_cluster = (
        _load_wo13_impact(candidates_path) if candidates_path.exists() else {}
    )

    conn = wip_connection(autocommit=True)
    try:
        groups = _load_live_groups(conn, include_needs_review=args.include_needs_review)

        # Enrich each group with impact_score + suggested primary
        for g in groups:
            cid = g.get("source_cluster_id") or ""
            wo13 = impact_by_cluster.get(cid, {})
            try:
                g["impact_score"] = float(wo13.get("impact_score") or 0)
            except ValueError:
                g["impact_score"] = 0.0
            g["webinar_attendees"] = int(wo13.get("webinar_attendees") or 0)
            g["import_reject_count"] = int(wo13.get("import_reject_count") or 0)
            g["distinct_cities"] = wo13.get("distinct_cities", "")
            g["distinct_account_types"] = wo13.get("distinct_account_types", "")
            g["suggested_primary_sf_id"] = _suggested_primary_sf_id(conn, int(g["group_id"]))

        # Rank by impact_score, then by current_wip, then by member_count
        groups.sort(
            key=lambda g: (
                -g["impact_score"],
                -float(g.get("group_current_wip") or 0),
                -int(g.get("member_count") or 0),
            )
        )

        if args.top:
            groups = groups[: args.top]

        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow([
                # WO-14A.1 apply expects: cluster_id, group_name,
                # group_name_normalised, set_primary_sf_id,
                # operator_decision, operator_note. Other columns are
                # for operator visibility only.
                "cluster_id", "group_name", "group_name_normalised",
                "review_status", "group_type", "seed_warning",
                "impact_score", "webinar_attendees", "import_reject_count",
                "member_count",
                "group_current_wip", "group_active_case_count",
                "distinct_cities", "distinct_account_types",
                "member_sf_ids", "member_canonical_ids",
                "member_account_names",
                "suggested_primary_sf_id",
                # Operator fields (blank — to be filled in)
                "operator_decision", "set_primary_sf_id", "operator_note",
            ])
            for g in groups:
                w.writerow([
                    g.get("source_cluster_id") or "",
                    g["group_name"], g["group_name_normalised"],
                    g["review_status"], g["group_type"],
                    g.get("seed_warning") or "",
                    f"{g['impact_score']:.1f}",
                    g["webinar_attendees"], g["import_reject_count"],
                    g["member_count"],
                    f"{float(g.get('group_current_wip') or 0):.2f}",
                    int(g.get("group_active_case_count") or 0),
                    g.get("distinct_cities", ""),
                    g.get("distinct_account_types", ""),
                    g.get("member_sf_ids") or "",
                    g.get("member_canonical_ids") or "",
                    g.get("member_account_names") or "",
                    g.get("suggested_primary_sf_id") or "",
                    "", "", "",
                ])

        print(f"wrote {len(groups)} group(s) for review → {out_path}")
        print()
        # Quick summary so operator knows what to expect
        warnings: dict[str, int] = {}
        with_wip = 0
        for g in groups:
            tag = g.get("seed_warning") or "none"
            warnings[tag] = warnings.get(tag, 0) + 1
            if float(g.get("group_current_wip") or 0) > 0:
                with_wip += 1
        print(f"  groups carrying current WIP : {with_wip} / {len(groups)}")
        print(f"  seed_warning breakdown      :")
        for tag, n in sorted(warnings.items(), key=lambda kv: -kv[1]):
            print(f"    {tag:30} {n}")

        return EXIT_OK
    except Exception as e:
        logging.exception("export failed: %s", e)
        return EXIT_FAILED
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main() or EXIT_OK)
