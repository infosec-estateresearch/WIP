"""Mastercopy Excel parser (WO-11).

Reads monthly sheets from MASTERCOPY-V2.xlsx, returns one dict per data row
with normalised column names, sheet metadata, and parsed financial values.

Mirrors the pattern in ERic's src/adapters/xlsx/adapter.py but stays
self-contained — does NOT import from /home/ELT (repo boundary).

Config defaults match the existing eric.json excel_mastercopy entry so the
SAME file can be parsed identically.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

logger = logging.getLogger("wip.imports.mastercopy_parser")


SHEET_PATTERN = re.compile(
    r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{4})$",
)

# Marker keywords that signal the header row in a sheet
HEADER_MARKERS = ("case number", "case name")

# Map raw column names → normalised keys we use downstream
HEADER_ALIASES = {
    "inst. date":                       "instruction_date",
    "instruction date":                 "instruction_date",
    "date":                             "instruction_date",
    "case name":                        "case_name",
    "case number":                      "case_number",
    "case type":                        "case_type",
    "fee type":                         "fee_type",
    "estate value":                     "estate_value",
    "estimated yeald":                  "estimated_yield",
    "estimated yield":                  "estimated_yield",
    "original estimated yield":         "original_estimated_yield",
    "revised estimated or actual yield": "actual_yield",
    "revised revised estimated or actual yield": "actual_yield",
    "actual yield":                     "actual_yield",
    "solicitors name":                  "firm_name",
    "firm name":                        "firm_name",
    "contact":                          "contact_name",
    "contact name":                     "contact_name",
    "marketing":                        "marketing",
    "new client or repeat business":    "client_type",
    "approached via":                   "approach_method",
    "case status":                      "status",
    "status":                           "status",
    "allocated to":                     "assigned_to",
    "assigned to":                      "assigned_to",
    "fees invoiced":                    "fees_invoiced",
    "invoice date":                     "invoice_date",
    "wip":                              "wip",
    "contingency status":               "contingency_status",
}

_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


@dataclass
class ParsedRow:
    """One data row from a monthly sheet, with normalised keys + metadata."""
    source_file:    str
    source_sheet:   str
    source_row:     int
    period_year:    int
    period_month:   int
    # Raw values keyed by normalised header (case_number, firm_name, wip, ...)
    raw:            dict


@dataclass
class ParseSummary:
    rows_total:         int = 0
    sheets_processed:   int = 0
    sheets_skipped:     int = 0
    rows_skipped_empty: int = 0
    file_hash:          str = ""
    rows:               list[ParsedRow] = field(default_factory=list)


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_mastercopy(path: str | Path) -> ParseSummary:
    """Parse every monthly sheet in the workbook and return a ParseSummary."""
    import openpyxl

    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Mastercopy not found: {p}")

    summary = ParseSummary(file_hash=file_sha256(p))
    wb = openpyxl.load_workbook(str(p), read_only=True, data_only=True)
    try:
        for sheet_name in wb.sheetnames:
            m = SHEET_PATTERN.match(sheet_name)
            if not m:
                summary.sheets_skipped += 1
                continue

            period_month = _MONTHS[m.group(1)]
            period_year = int(m.group(2))

            ws = wb[sheet_name]
            rows = list(ws.rows)
            if len(rows) < 2:
                continue

            header_idx = _find_header_row(rows)
            if header_idx is None:
                logger.warning("sheet %r: header not found (markers=%s) — skipping",
                               sheet_name, HEADER_MARKERS)
                summary.sheets_skipped += 1
                continue

            raw_headers = [
                str(c.value).strip() if c.value is not None else f"_col{i}"
                for i, c in enumerate(rows[header_idx])
            ]
            headers = [HEADER_ALIASES.get(h.lower(), h.lower().replace(" ", "_")) for h in raw_headers]

            summary.sheets_processed += 1

            for row_num, row in enumerate(rows[header_idx + 1:], start=header_idx + 2):
                record: dict = {}
                for col_idx, header in enumerate(headers):
                    if col_idx < len(row):
                        v = row[col_idx].value
                        record[header] = v if v is not None else None
                    else:
                        record[header] = None

                # Skip all-empty rows
                if all(v is None or (isinstance(v, str) and v.strip() == "")
                       for v in record.values()):
                    summary.rows_skipped_empty += 1
                    continue

                summary.rows.append(ParsedRow(
                    source_file=p.name,
                    source_sheet=sheet_name,
                    source_row=row_num,
                    period_year=period_year,
                    period_month=period_month,
                    raw=record,
                ))
                summary.rows_total += 1
    finally:
        wb.close()

    logger.info(
        "mastercopy parsed: %d sheets processed, %d skipped; %d rows, %d empty-skipped",
        summary.sheets_processed, summary.sheets_skipped,
        summary.rows_total, summary.rows_skipped_empty,
    )
    return summary


def _find_header_row(rows) -> int | None:
    """Return the index of the row containing all HEADER_MARKERS, or None."""
    for i, row in enumerate(rows[:20]):  # headers are always in first ~20 rows
        cells_lower = [
            str(c.value).strip().lower() if c.value is not None else ""
            for c in row
        ]
        if all(any(marker in cell for cell in cells_lower) for marker in HEADER_MARKERS):
            return i
    return None


# ---------------------------------------------------------------------------
# Value parsing helpers (DECIMAL-safe, DATE-safe)
# ---------------------------------------------------------------------------

class ParseError(ValueError):
    """Raised when a value can't be safely converted (caller turns into reject)."""


def parse_decimal(v) -> Decimal | None:
    """Convert a CSV/Excel cell to Decimal. None/blank → None. Raises ParseError on bad."""
    if v is None or v == "":
        return None
    if isinstance(v, Decimal):
        return v
    if isinstance(v, (int, float)):
        # Excel often returns floats — round-trip through str to avoid binary float noise
        try:
            return Decimal(str(v))
        except (InvalidOperation, ValueError) as e:
            raise ParseError(f"cannot parse {v!r} as Decimal: {e}")
    s = str(v).strip()
    if not s:
        return None
    # Strip pound signs, commas, thousand separators
    s = s.replace("£", "").replace(",", "").replace(" ", "")
    if s in ("-", "—", "N/A", "n/a"):
        return None
    try:
        return Decimal(s)
    except InvalidOperation as e:
        raise ParseError(f"cannot parse {v!r} as Decimal: {e}")


def parse_date(v) -> date | None:
    """Convert an Excel cell to date. None/blank → None. Raises ParseError on bad."""
    if v is None or v == "":
        return None
    if isinstance(v, date):
        return v
    s = str(v).strip()
    if not s:
        return None
    from datetime import datetime
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    raise ParseError(f"cannot parse {v!r} as date")


def parse_case_reference(v) -> str | None:
    """Case reference comes through as int/float/str — return canonical string or None.
    Skip non-numeric (e.g. summary rows like 'Totals')."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        try:
            return str(int(v))
        except (ValueError, OverflowError):
            return None
    s = str(v).strip()
    if not s:
        return None
    # Mastercopy convention: skip non-numeric refs (summary rows, footers)
    try:
        return str(int(float(s)))
    except (ValueError, TypeError):
        return None
