"""Mastercopy → wip_case + wip_case_financial import runner (WO-11).

End-to-end orchestration:
  1. open wip_import_batch (dry-run flag set if applicable)
  2. parse the Excel workbook (mastercopy_parser)
  3. load firm matcher (in-memory snapshot of active wip_company)
  4. for each parsed row, classify into one of:
       inserted / updated / unchanged / rejected
  5. write rejects to wip_import_reject (apply mode only)
  6. mark any previously-rejected (firm, case) pair RESOLVED when it imports
  7. close wip_import_batch with telemetry

Architect rules enforced:
  - Every source row gets exactly one classification. No silent fifth state.
  - DECIMAL throughout for money.
  - Re-import is idempotent: existing (case_reference) upsert, existing
    (case_id, period) for financials.
  - No fuzzy matching, no manual company creation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Literal

from src.wip_app.imports.firm_matcher import CompanyMatch, FirmMatcher
from src.wip_app.imports.mastercopy_parser import (
    ParsedRow,
    ParseError,
    file_sha256,
    parse_case_reference,
    parse_date,
    parse_decimal,
    parse_mastercopy,
)
from src.wip_app.imports.reject_queue import log_reject, mark_resolved_for
from src.wip_app.normaliser import normalise_company_name

logger = logging.getLogger("wip.imports.mastercopy_runner")


@dataclass
class ImportResult:
    import_batch_id: int | None = None
    source_file: str = ""
    source_hash: str = ""
    dry_run: bool = True
    rows_seen: int = 0
    rows_inserted: int = 0   # wip_case + wip_case_financial inserts
    rows_updated: int = 0    # at least one of case/financial changed
    rows_unchanged: int = 0
    rows_rejected: int = 0
    reject_breakdown: dict[str, int] = field(default_factory=dict)
    rejects_resolved: int = 0
    error_message: str | None = None


def run_import(
    conn,
    *,
    source_path: str,
    apply: bool,
) -> ImportResult:
    """Run one CSV → WIP import end-to-end."""
    result = ImportResult(source_file=source_path, dry_run=not apply)
    result.source_hash = file_sha256(source_path)

    batch_id = _open_batch(
        conn,
        source_file=source_path,
        source_hash=result.source_hash,
        dry_run=not apply,
    )
    result.import_batch_id = batch_id

    try:
        summary = parse_mastercopy(source_path)
        matcher = FirmMatcher.load(conn)

        for prow in summary.rows:
            result.rows_seen += 1
            _process_row(
                conn,
                row=prow,
                matcher=matcher,
                batch_id=batch_id,
                apply=apply,
                result=result,
            )

        _close_batch(
            conn,
            batch_id=batch_id,
            status="completed",
            source_sheet=None,
            result=result,
        )
        if apply:
            conn.commit()
        else:
            conn.rollback()
            # Re-write the closed status under autocommit so dry-run telemetry survives
            _close_batch(conn, batch_id=batch_id, status="completed",
                         source_sheet=None, result=result)
            conn.commit()
        return result

    except Exception as e:
        result.error_message = str(e)
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            _close_batch(conn, batch_id=batch_id, status="failed",
                         source_sheet=None, result=result)
            conn.commit()
        except Exception:
            logger.exception("could not record batch failure")
        logger.exception("import failed: %s", e)
        raise


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _open_batch(conn, *, source_file: str, source_hash: str, dry_run: bool) -> int:
    """Insert wip_import_batch in its own auto-committed transaction."""
    now = datetime.now(timezone.utc)
    autocommit_before = getattr(conn, "get_autocommit", lambda: False)()
    conn.autocommit(True)
    try:
        with conn.cursor() as c:
            c.execute(
                """
                INSERT INTO wip_import_batch
                  (source_type, source_file, source_hash, dry_run,
                   started_at, status)
                VALUES ('csv_mastercopy', %s, %s, %s, %s, 'running')
                """,
                (source_file, source_hash, 1 if dry_run else 0, now),
            )
            batch_id = c.lastrowid
        logger.info("opened wip_import_batch id=%d dry_run=%s", batch_id, dry_run)
        return batch_id
    finally:
        conn.autocommit(autocommit_before)


def _close_batch(conn, *, batch_id: int, status: str, source_sheet: str | None,
                 result: ImportResult) -> None:
    autocommit_before = getattr(conn, "get_autocommit", lambda: False)()
    conn.autocommit(True)
    try:
        with conn.cursor() as c:
            c.execute(
                """
                UPDATE wip_import_batch SET
                  completed_at = %s,
                  status = %s,
                  source_sheet = %s,
                  rows_seen = %s,
                  rows_inserted = %s,
                  rows_updated = %s,
                  rows_unchanged = %s,
                  rows_rejected = %s,
                  error_message = %s
                WHERE id = %s
                """,
                (
                    datetime.now(timezone.utc), status, source_sheet,
                    result.rows_seen, result.rows_inserted, result.rows_updated,
                    result.rows_unchanged, result.rows_rejected,
                    result.error_message, batch_id,
                ),
            )
    finally:
        conn.autocommit(autocommit_before)


def _process_row(
    conn, *, row: ParsedRow, matcher: FirmMatcher,
    batch_id: int, apply: bool, result: ImportResult,
) -> None:
    """Classify and (if apply) write one source row."""
    raw = row.raw

    def _str(v):
        # Excel cells can come through as int/float when the spreadsheet
        # author typed a number into a text column. Coerce defensively so a
        # junk "firm name = 12345" row rejects as NO_COMPANY_MATCH instead
        # of crashing the whole batch.
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    firm_raw = _str(raw.get("firm_name"))
    case_ref_raw = parse_case_reference(raw.get("case_number"))
    firm_norm = normalise_company_name(firm_raw) if firm_raw else None

    def _reject(reason: str, *, candidates: CompanyMatch | None = None):
        result.rows_rejected += 1
        result.reject_breakdown[reason] = result.reject_breakdown.get(reason, 0) + 1
        if not apply:
            return
        cand_ids = candidates.candidate_ids if candidates else None
        cand_names = candidates.candidate_names if candidates else None
        log_reject(
            conn,
            import_batch_id=batch_id,
            source_file=row.source_file, source_sheet=row.source_sheet,
            source_row_number=row.source_row,
            firm_name_raw=firm_raw, firm_name_normalised=firm_norm,
            case_reference_raw=case_ref_raw,
            reject_reason=reason,
            row_json=raw,
            candidate_company_ids=cand_ids,
            candidate_company_names=cand_names,
        )

    # 1. Mandatory fields
    if not firm_raw:
        _reject("MISSING_FIRM_NAME")
        return
    if not case_ref_raw:
        _reject("MISSING_CASE_REFERENCE")
        return

    # 2. Firm matching against wip_company (active SF Accounts only)
    match = matcher.match(firm_name=firm_raw)
    if match.result == "no_match":
        _reject("NO_COMPANY_MATCH")
        return
    if match.result == "multi_match":
        _reject("MULTIPLE_COMPANY_MATCHES", candidates=match)
        return
    # match.result == "match"

    # 3. Parse financial / date fields (any parse error → reject row)
    try:
        wip_value           = parse_decimal(raw.get("wip"))
        fees_invoiced       = parse_decimal(raw.get("fees_invoiced"))
        actual_yield        = parse_decimal(raw.get("actual_yield"))
        estimated_yield     = parse_decimal(raw.get("estimated_yield") or raw.get("original_estimated_yield"))
    except ParseError as e:
        logger.warning("INVALID_FINANCIAL_VALUE row=%s:%d: %s", row.source_sheet, row.source_row, e)
        _reject("INVALID_FINANCIAL_VALUE")
        return

    try:
        opened_at = parse_date(raw.get("instruction_date"))
    except ParseError as e:
        logger.warning("INVALID_DATE row=%s:%d: %s", row.source_sheet, row.source_row, e)
        _reject("INVALID_DATE")
        return

    case_name = _str(raw.get("case_name")) or "(unnamed)"
    case_type = _str(raw.get("case_type"))
    status_val = _str(raw.get("status"))
    as_of = date(row.period_year, row.period_month, 1)

    # 4. Upsert wip_case + wip_case_financial
    if not apply:
        # Dry-run classification: we can't tell insert vs update vs unchanged
        # without a real read. Use a cheap read to classify accurately.
        change = _classify_dry_run(
            conn, case_reference=case_ref_raw,
            wip_company_id=match.wip_company_id,
            proposed_case_name=case_name,
            proposed_case_type=case_type,
            proposed_status=status_val,
            proposed_opened_at=opened_at,
            row=row, wip_value=wip_value, fees_invoiced=fees_invoiced,
            actual_yield=actual_yield, estimated_yield=estimated_yield,
        )
        if change == "insert":
            result.rows_inserted += 1
        elif change == "update":
            result.rows_updated += 1
        else:
            result.rows_unchanged += 1
        return

    case_id, case_changed = _upsert_case(
        conn,
        match=match,
        case_reference=case_ref_raw,
        case_name=case_name, case_type=case_type,
        status_val=status_val, opened_at=opened_at,
    )
    fin_changed = _upsert_financial(
        conn,
        wip_case_id=case_id,
        as_of_date=as_of,
        period_year=row.period_year, period_month=row.period_month,
        wip_value=wip_value, fees_invoiced=fees_invoiced,
        actual_yield=actual_yield, estimated_yield=estimated_yield,
        import_batch_id=batch_id,
    )

    if case_changed == "insert" or fin_changed == "insert":
        result.rows_inserted += 1
    elif case_changed == "update" or fin_changed == "update":
        result.rows_updated += 1
    else:
        result.rows_unchanged += 1

    # 5. Resolve any prior reject rows for this (firm, case) pair
    resolved = mark_resolved_for(
        conn,
        firm_name_normalised=firm_norm,
        case_reference_raw=case_ref_raw,
        resolution_note=f"resolved by import_batch_id={batch_id} on re-import",
    )
    result.rejects_resolved += resolved


def _upsert_case(
    conn, *, match: CompanyMatch, case_reference: str,
    case_name: str, case_type: str | None, status_val: str | None,
    opened_at: date | None,
) -> tuple[int, Literal["insert", "update", "unchanged"]]:
    """Insert wip_case or update if changed. Returns (case_id, classification)."""
    now = datetime.now(timezone.utc)
    with conn.cursor() as c:
        c.execute("SELECT * FROM wip_case WHERE case_reference = %s", (case_reference,))
        existing = c.fetchone()

    if existing is None:
        with conn.cursor() as c:
            c.execute(
                """
                INSERT INTO wip_case
                  (wip_company_id, salesforce_account_id, canonical_company_id,
                   case_reference, case_name, case_type, source,
                   status, opened_at, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, 'csv_import', %s, %s, %s, %s)
                """,
                (
                    match.wip_company_id, match.salesforce_account_id,
                    match.canonical_company_id,
                    case_reference, case_name, case_type,
                    status_val, opened_at, now, now,
                ),
            )
            return c.lastrowid, "insert"

    # Compare material fields
    changed = (
        existing.get("wip_company_id") != match.wip_company_id
        or (existing.get("case_name") or "") != case_name
        or (existing.get("case_type") or None) != case_type
        or (existing.get("status") or None) != status_val
        or existing.get("opened_at") != opened_at
        or existing.get("canonical_company_id") != match.canonical_company_id
    )
    if not changed:
        return existing["id"], "unchanged"

    with conn.cursor() as c:
        c.execute(
            """
            UPDATE wip_case SET
              wip_company_id        = %s,
              salesforce_account_id = %s,
              canonical_company_id  = %s,
              case_name             = %s,
              case_type             = %s,
              status                = %s,
              opened_at             = %s,
              updated_at            = %s
            WHERE id = %s
            """,
            (
                match.wip_company_id, match.salesforce_account_id,
                match.canonical_company_id,
                case_name, case_type, status_val, opened_at,
                now, existing["id"],
            ),
        )
    return existing["id"], "update"


def _upsert_financial(
    conn, *, wip_case_id: int, as_of_date: date,
    period_year: int, period_month: int,
    wip_value, fees_invoiced, actual_yield, estimated_yield,
    import_batch_id: int,
) -> Literal["insert", "update", "unchanged"]:
    """Insert or update one wip_case_financial row per (case, period)."""
    now = datetime.now(timezone.utc)
    with conn.cursor() as c:
        c.execute(
            "SELECT * FROM wip_case_financial WHERE wip_case_id = %s "
            "AND period_year = %s AND period_month = %s",
            (wip_case_id, period_year, period_month),
        )
        existing = c.fetchone()

    if existing is None:
        with conn.cursor() as c:
            c.execute(
                """
                INSERT INTO wip_case_financial
                  (wip_case_id, as_of_date, period_year, period_month,
                   wip_value, fees_invoiced, actual_yield, estimated_yield,
                   currency, source, import_batch_id,
                   created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                        'GBP', 'csv_import', %s, %s, %s)
                """,
                (
                    wip_case_id, as_of_date, period_year, period_month,
                    wip_value, fees_invoiced, actual_yield, estimated_yield,
                    import_batch_id, now, now,
                ),
            )
            return "insert"

    changed = (
        existing.get("wip_value")        != wip_value
        or existing.get("fees_invoiced") != fees_invoiced
        or existing.get("actual_yield")  != actual_yield
        or existing.get("estimated_yield") != estimated_yield
        or existing.get("as_of_date")    != as_of_date
    )
    if not changed:
        return "unchanged"

    with conn.cursor() as c:
        c.execute(
            """
            UPDATE wip_case_financial SET
              as_of_date      = %s,
              wip_value       = %s,
              fees_invoiced   = %s,
              actual_yield    = %s,
              estimated_yield = %s,
              import_batch_id = %s,
              updated_at      = %s
            WHERE id = %s
            """,
            (as_of_date, wip_value, fees_invoiced, actual_yield, estimated_yield,
             import_batch_id, now, existing["id"]),
        )
    return "update"


def _classify_dry_run(
    conn, *, case_reference: str, wip_company_id: int,
    proposed_case_name: str, proposed_case_type: str | None,
    proposed_status: str | None, proposed_opened_at: date | None,
    row: ParsedRow,
    wip_value, fees_invoiced, actual_yield, estimated_yield,
) -> Literal["insert", "update", "unchanged"]:
    """Cheap read-only classification so dry-run telemetry is accurate."""
    with conn.cursor() as c:
        c.execute("SELECT id, wip_company_id, case_name, case_type, status, opened_at "
                  "FROM wip_case WHERE case_reference = %s", (case_reference,))
        existing_case = c.fetchone()
        if existing_case is None:
            return "insert"
        case_id = existing_case["id"]
        case_changed = (
            existing_case.get("wip_company_id") != wip_company_id
            or (existing_case.get("case_name") or "") != proposed_case_name
            or (existing_case.get("case_type") or None) != proposed_case_type
            or (existing_case.get("status") or None) != proposed_status
            or existing_case.get("opened_at") != proposed_opened_at
        )
        c.execute(
            "SELECT wip_value, fees_invoiced, actual_yield, estimated_yield, as_of_date "
            "FROM wip_case_financial WHERE wip_case_id = %s "
            "AND period_year = %s AND period_month = %s",
            (case_id, row.period_year, row.period_month),
        )
        existing_fin = c.fetchone()
    as_of_proposed = date(row.period_year, row.period_month, 1)
    if existing_fin is None:
        return "insert"
    fin_changed = (
        existing_fin.get("wip_value")        != wip_value
        or existing_fin.get("fees_invoiced") != fees_invoiced
        or existing_fin.get("actual_yield")  != actual_yield
        or existing_fin.get("estimated_yield") != estimated_yield
        or existing_fin.get("as_of_date")    != as_of_proposed
    )
    if case_changed or fin_changed:
        return "update"
    return "unchanged"
