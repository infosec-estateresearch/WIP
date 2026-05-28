"""Salesforce-Account duplicate detector.

Runs AFTER the sync upsert loop is done. Re-reads active `wip_company`
rows, groups by `duplicate_group_key`, marks `is_potential_duplicate` on
rows in groups of size ≥2, and upserts the `wip_company_duplicate_candidate`
row for each such group.

Never merges. Never blocks the sync. Detection only.

Returns counts so the sync_run telemetry records:
- duplicate_groups_active   (count of groups of size ≥2)
- duplicate_rows_flagged    (count of wip_company rows now is_potential_duplicate=1)
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger("wip.sync.duplicate_detector")


@dataclass
class DuplicateDetectionResult:
    groups_active: int = 0
    rows_flagged: int = 0
    candidates_inserted: int = 0
    candidates_updated: int = 0


def detect_and_upsert_duplicates(conn, *, sync_run_id: int | None = None) -> DuplicateDetectionResult:
    """Scan active wip_company, group by duplicate_group_key, flag duplicates,
    upsert candidate-group rows.

    Args:
        conn: pymysql connection to nif_wip (autocommit DOESN'T matter — we
            commit at end).
        sync_run_id: stored on each candidate row so the next sync can see
            which run last confirmed it.
    """
    result = DuplicateDetectionResult()

    # 1. Group active rows by duplicate_group_key
    with conn.cursor() as c:
        c.execute(
            """
            SELECT id, salesforce_account_id, salesforce_account_name,
                   account_type, industry, owner_name, billing_postcode,
                   duplicate_group_key, last_seen_in_salesforce_at
              FROM wip_company
             WHERE is_active_in_salesforce = 1
               AND duplicate_group_key IS NOT NULL
               AND duplicate_group_key <> ''
            """
        )
        rows = list(c.fetchall())

    by_group: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_group[r["duplicate_group_key"]].append(r)

    # 2. Reset flags first (so cleaned-up groups don't keep their flag)
    with conn.cursor() as c:
        c.execute("UPDATE wip_company SET is_potential_duplicate = 0 WHERE is_potential_duplicate = 1")

    # 3. For each group of ≥2, flag the rows and upsert candidate
    now = datetime.now(timezone.utc)
    for key, members in by_group.items():
        if len(members) < 2:
            continue
        result.groups_active += 1
        result.rows_flagged += len(members)

        member_ids = [m["id"] for m in members]
        sf_ids = [m["salesforce_account_id"] for m in members]
        names = [m["salesforce_account_name"] for m in members]
        metadata = [
            {
                "salesforce_account_id": m["salesforce_account_id"],
                "name":                  m["salesforce_account_name"],
                "account_type":          m["account_type"],
                "industry":              m["industry"],
                "owner_name":            m["owner_name"],
                "billing_postcode":      m["billing_postcode"],
                "last_seen_in_salesforce_at": m["last_seen_in_salesforce_at"].isoformat()
                                              if m["last_seen_in_salesforce_at"] else None,
            }
            for m in members
        ]

        # Flag the wip_company rows
        with conn.cursor() as c:
            fmt = ",".join(["%s"] * len(member_ids))
            c.execute(
                f"UPDATE wip_company SET is_potential_duplicate = 1 WHERE id IN ({fmt})",
                tuple(member_ids),
            )

        # Upsert the candidate group row
        with conn.cursor() as c:
            c.execute(
                "SELECT id FROM wip_company_duplicate_candidate "
                "WHERE duplicate_group_key = %s",
                (key,),
            )
            existing = c.fetchone()

            if existing is None:
                c.execute(
                    """
                    INSERT INTO wip_company_duplicate_candidate
                      (duplicate_group_key, candidate_count,
                       salesforce_account_ids_json, account_names_json,
                       account_metadata_json, status,
                       first_detected_at, last_seen_at, last_salesforce_sync_run_id)
                    VALUES (%s, %s, %s, %s, %s, 'NEW', %s, %s, %s)
                    """,
                    (
                        key, len(member_ids),
                        json.dumps(sf_ids), json.dumps(names),
                        json.dumps(metadata),
                        now, now, sync_run_id,
                    ),
                )
                result.candidates_inserted += 1
            else:
                # Re-seen group — update counts and metadata, preserve
                # first_detected_at, do NOT change operator-managed
                # status / reviewed_by / reviewed_at / resolution_note.
                c.execute(
                    """
                    UPDATE wip_company_duplicate_candidate
                       SET candidate_count = %s,
                           salesforce_account_ids_json = %s,
                           account_names_json = %s,
                           account_metadata_json = %s,
                           last_seen_at = %s,
                           last_salesforce_sync_run_id = %s
                     WHERE id = %s
                    """,
                    (
                        len(member_ids),
                        json.dumps(sf_ids), json.dumps(names),
                        json.dumps(metadata), now, sync_run_id,
                        existing["id"],
                    ),
                )
                result.candidates_updated += 1

    conn.commit()
    logger.info(
        "duplicate detection: groups=%d rows_flagged=%d inserted=%d updated=%d",
        result.groups_active, result.rows_flagged,
        result.candidates_inserted, result.candidates_updated,
    )
    return result
