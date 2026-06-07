"""WIP import reject queue writer + reconciler.

Two responsibilities (WO-11 architect addition):
  1. log_reject() — append a wip_import_reject row when a CSV row can't
     import. Includes raw row JSON, normalised firm name, candidate IDs.
  2. mark_resolved_for() — when a re-import succeeds for a (firm, case)
     pair that previously rejected, flip the prior reject rows to RESOLVED
     with a note. Closes the loop the architect described:
       "CSV row rejected → operator fixes Salesforce → next sync brings
        company into wip_company → rerun import → row imports → old reject
        marked RESOLVED"
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Iterable

logger = logging.getLogger("wip.imports.reject_queue")


_VALID_REASONS = {
    "NO_COMPANY_MATCH", "MULTIPLE_COMPANY_MATCHES", "POTENTIAL_DUPLICATE_COMPANY",
    "MISSING_FIRM_NAME", "MISSING_CASE_REFERENCE", "INVALID_FINANCIAL_VALUE",
    "INVALID_DATE", "DUPLICATE_SOURCE_ROW", "UNSUPPORTED_ROW_SHAPE",
}


def log_reject(
    conn,
    *,
    import_batch_id: int,
    source_file: str,
    source_sheet: str,
    source_row_number: int,
    firm_name_raw: str | None,
    firm_name_normalised: str | None,
    case_reference_raw: str | None,
    reject_reason: str,
    row_json: dict,
    candidate_company_ids: list[int] | None = None,
    candidate_company_names: list[str] | None = None,
) -> int:
    """Append one wip_import_reject row. Returns the new id."""
    if reject_reason not in _VALID_REASONS:
        raise ValueError(f"unknown reject_reason {reject_reason!r}")

    with conn.cursor() as c:
        # WO-23: idempotent on the source row's identity
        # (source_file, source_sheet, source_row_number) — UNIQUE key
        # uq_reject_source. A re-import of the same Mastercopy row UPDATES the
        # existing reject in place instead of appending a duplicate, and PRESERVES
        # any operator decision (resolution_status / note / reviewer) so a row an
        # operator already ignored/resolved does not silently re-open.
        c.execute(
            """
            INSERT INTO wip_import_reject
              (import_batch_id, source_file, source_sheet, source_row_number,
               firm_name_raw, firm_name_normalised, case_reference_raw,
               reject_reason,
               candidate_company_ids_json, candidate_company_names_json,
               row_json, resolution_status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'NEW')
            ON DUPLICATE KEY UPDATE
              import_batch_id            = VALUES(import_batch_id),
              firm_name_raw              = VALUES(firm_name_raw),
              firm_name_normalised       = VALUES(firm_name_normalised),
              case_reference_raw         = VALUES(case_reference_raw),
              reject_reason              = VALUES(reject_reason),
              candidate_company_ids_json = VALUES(candidate_company_ids_json),
              candidate_company_names_json = VALUES(candidate_company_names_json),
              row_json                   = VALUES(row_json),
              updated_at                 = CURRENT_TIMESTAMP(6)
            """,
            (
                import_batch_id, source_file, source_sheet, source_row_number,
                firm_name_raw, firm_name_normalised, case_reference_raw,
                reject_reason,
                json.dumps(candidate_company_ids) if candidate_company_ids else None,
                json.dumps(candidate_company_names) if candidate_company_names else None,
                json.dumps(_jsonable(row_json)),
            ),
        )
        return c.lastrowid


def mark_resolved_for(
    conn,
    *,
    firm_name_normalised: str | None,
    case_reference_raw: str | None,
    resolution_note: str,
) -> int:
    """Mark every NEW/NEEDS_REVIEW reject matching this (firm, case) as RESOLVED.

    Returns the number of reject rows updated. Idempotent — already-RESOLVED
    rows are left alone.

    The (firm_name_normalised, case_reference_raw) pair is the natural key
    we use to associate a successful re-import with prior rejects.
    """
    if not (firm_name_normalised or case_reference_raw):
        return 0
    now = datetime.now(timezone.utc)
    with conn.cursor() as c:
        c.execute(
            """
            UPDATE wip_import_reject
               SET resolution_status = 'RESOLVED',
                   resolution_note = %s,
                   reviewed_at = %s
             WHERE resolution_status IN ('NEW', 'NEEDS_REVIEW')
               AND (firm_name_normalised <=> %s)
               AND (case_reference_raw   <=> %s)
            """,
            (resolution_note, now, firm_name_normalised, case_reference_raw),
        )
        return c.rowcount


def _jsonable(d):
    """Coerce non-JSON types so json.dumps never throws mid-batch."""
    if isinstance(d, dict):
        return {k: _jsonable(v) for k, v in d.items()}
    if isinstance(d, (list, tuple)):
        return [_jsonable(x) for x in d]
    if d is None or isinstance(d, (str, int, float, bool)):
        return d
    from datetime import date, datetime
    if isinstance(d, (date, datetime)):
        return d.isoformat()
    try:
        from decimal import Decimal
        if isinstance(d, Decimal):
            return str(d)
    except ImportError:
        pass
    return str(d)
