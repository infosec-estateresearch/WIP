"""WO-27 — manual WIP entry create/edit against an EXISTING firm.

These functions never create a company: they require a resolved wip_company and
attach a wip_case (source='manual') plus a wip_case_financial snapshot, writing
audit rows for both. Validation is centralised in `validate_entry` so the route
can re-render the form with field errors before any write.

The route only calls create/update when the WIP_MANUAL_WRITES_ENABLED flag is on
(see src/wip_web/feature_flags.py). Until cutover these are exercised by tests
with the flag toggled, proving the path works without going live.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation


REQUIRED_FIELDS = ("case_reference", "case_name", "wip_value", "as_of_date")


class ValidationError(Exception):
    """Carries a {field: message} map for re-rendering the form."""

    def __init__(self, errors: dict[str, str]):
        self.errors = errors
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))


def _parse_decimal(raw: str, field: str, errors: dict, *, required: bool) -> Decimal | None:
    raw = (raw or "").strip().replace(",", "").replace("£", "")
    if not raw:
        if required:
            errors[field] = "This value is required."
        return None
    try:
        val = Decimal(raw)
    except (InvalidOperation, ValueError):
        errors[field] = "Enter a number."
        return None
    if val < 0:
        errors[field] = "Cannot be negative."
        return None
    return val


def _parse_date(raw: str, field: str, errors: dict) -> date | None:
    raw = (raw or "").strip()
    if not raw:
        errors[field] = "This value is required."
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        errors[field] = "Use the date picker (YYYY-MM-DD)."
        return None


def validate_entry(form: dict, *, require_reference: bool = True) -> dict:
    """Validate raw form input. Raises ValidationError on any problem;
    otherwise returns a cleaned dict ready for create/update."""
    errors: dict[str, str] = {}

    case_reference = (form.get("case_reference") or "").strip()
    if require_reference and not case_reference:
        errors["case_reference"] = "This value is required."
    case_name = (form.get("case_name") or "").strip()
    if not case_name:
        errors["case_name"] = "This value is required."

    wip_value = _parse_decimal(form.get("wip_value"), "wip_value", errors, required=True)
    estimated_yield = _parse_decimal(form.get("estimated_yield"), "estimated_yield", errors, required=False)
    fees_invoiced = _parse_decimal(form.get("fees_invoiced"), "fees_invoiced", errors, required=False)
    as_of = _parse_date(form.get("as_of_date"), "as_of_date", errors)

    if errors:
        raise ValidationError(errors)

    return {
        "case_reference": case_reference,
        "case_name": case_name,
        "status": (form.get("status") or "").strip() or None,
        "manual_note": (form.get("manual_note") or "").strip() or None,
        "wip_value": wip_value,
        "estimated_yield": estimated_yield,
        "fees_invoiced": fees_invoiced,
        "as_of_date": as_of,
    }


def _audit(c, entity_type: str, entity_id: int, action: str, after: dict,
           actor: str, now: datetime, changed: dict | None = None) -> None:
    c.execute(
        "INSERT INTO wip_audit_log (entity_type, entity_id, action, "
        "  changed_fields_json, after_json, actor, changed_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (entity_type, entity_id, action,
         json.dumps(changed) if changed is not None else None,
         json.dumps(after, default=str), actor, now),
    )


def reference_exists(conn, case_reference: str, *, exclude_case_id: int | None = None) -> bool:
    with conn.cursor() as c:
        if exclude_case_id:
            c.execute("SELECT 1 FROM wip_case WHERE case_reference = %s AND id <> %s LIMIT 1",
                      (case_reference, exclude_case_id))
        else:
            c.execute("SELECT 1 FROM wip_case WHERE case_reference = %s LIMIT 1", (case_reference,))
        return c.fetchone() is not None


def create_manual_wip_case(conn, company: dict, cleaned: dict, actor: str) -> int:
    """Insert a manual wip_case + its financial snapshot, with audit. Returns the
    new wip_case id. `company` must be an existing wip_company row. Raises
    ValidationError({'case_reference': ...}) if the reference is already used."""
    if reference_exists(conn, cleaned["case_reference"]):
        raise ValidationError({"case_reference":
                               "A WIP entry with this case reference already exists."})

    now = datetime.now(timezone.utc)
    as_of = cleaned["as_of_date"]
    with conn.cursor() as c:
        c.execute(
            "INSERT INTO wip_case (wip_company_id, salesforce_account_id, "
            "  canonical_company_id, case_reference, case_name, status, manual_note, "
            "  created_by, source, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'manual', %s, %s)",
            (company["id"], company["salesforce_account_id"],
             company.get("canonical_company_id"), cleaned["case_reference"],
             cleaned["case_name"], cleaned["status"], cleaned["manual_note"],
             actor, now, now),
        )
        case_id = c.lastrowid
        c.execute(
            "INSERT INTO wip_case_financial (wip_case_id, as_of_date, period_year, "
            "  period_month, wip_value, fees_invoiced, estimated_yield, currency, "
            "  source, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 'GBP', 'manual', %s, %s)",
            (case_id, as_of, as_of.year, as_of.month, cleaned["wip_value"],
             cleaned["fees_invoiced"], cleaned["estimated_yield"], now, now),
        )
        fin_id = c.lastrowid
        _audit(c, "wip_case", case_id, "insert",
               {"case_reference": cleaned["case_reference"], "case_name": cleaned["case_name"],
                "status": cleaned["status"], "source": "manual",
                "wip_company_id": company["id"], "manual_note": cleaned["manual_note"]},
               actor, now)
        _audit(c, "wip_case_financial", fin_id, "insert",
               {"wip_case_id": case_id, "as_of_date": str(as_of),
                "wip_value": str(cleaned["wip_value"]),
                "estimated_yield": str(cleaned["estimated_yield"]) if cleaned["estimated_yield"] is not None else None,
                "fees_invoiced": str(cleaned["fees_invoiced"]) if cleaned["fees_invoiced"] is not None else None,
                "source": "manual"},
               actor, now)
    return case_id


def update_manual_wip_case(conn, case: dict, cleaned: dict, actor: str) -> None:
    """Update an existing MANUAL wip_case + its latest financial snapshot, with
    audit. Refuses to touch non-manual (imported) rows. `case` is a wip_case row."""
    if case.get("source") != "manual":
        raise ValidationError({"_": "Only manual entries can be edited here."})
    if cleaned["case_reference"] != case["case_reference"] and \
            reference_exists(conn, cleaned["case_reference"], exclude_case_id=int(case["id"])):
        raise ValidationError({"case_reference":
                               "A WIP entry with this case reference already exists."})

    now = datetime.now(timezone.utc)
    as_of = cleaned["as_of_date"]
    case_id = int(case["id"])
    with conn.cursor() as c:
        c.execute(
            "UPDATE wip_case SET case_reference = %s, case_name = %s, status = %s, "
            "  manual_note = %s, updated_by = %s, updated_at = %s "
            "WHERE id = %s AND source = 'manual'",
            (cleaned["case_reference"], cleaned["case_name"], cleaned["status"],
             cleaned["manual_note"], actor, now, case_id),
        )
        # Update the latest financial snapshot in place (one manual snapshot per
        # case in the MVP); insert one if somehow absent.
        c.execute("SELECT id FROM wip_case_financial WHERE wip_case_id = %s "
                  "ORDER BY as_of_date DESC, id DESC LIMIT 1", (case_id,))
        fin = c.fetchone()
        if fin:
            c.execute(
                "UPDATE wip_case_financial SET as_of_date = %s, period_year = %s, "
                "  period_month = %s, wip_value = %s, fees_invoiced = %s, "
                "  estimated_yield = %s, updated_at = %s WHERE id = %s",
                (as_of, as_of.year, as_of.month, cleaned["wip_value"],
                 cleaned["fees_invoiced"], cleaned["estimated_yield"], now, fin["id"]),
            )
            fin_id = int(fin["id"])
        else:
            c.execute(
                "INSERT INTO wip_case_financial (wip_case_id, as_of_date, period_year, "
                "  period_month, wip_value, fees_invoiced, estimated_yield, currency, "
                "  source, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, 'GBP', 'manual', %s, %s)",
                (case_id, as_of, as_of.year, as_of.month, cleaned["wip_value"],
                 cleaned["fees_invoiced"], cleaned["estimated_yield"], now, now),
            )
            fin_id = c.lastrowid
        _audit(c, "wip_case", case_id, "update",
               {"case_reference": cleaned["case_reference"], "case_name": cleaned["case_name"],
                "status": cleaned["status"], "manual_note": cleaned["manual_note"]},
               actor, now, changed={"edited_by_ui": True})
        _audit(c, "wip_case_financial", fin_id, "update",
               {"wip_case_id": case_id, "as_of_date": str(as_of),
                "wip_value": str(cleaned["wip_value"])},
               actor, now, changed={"edited_by_ui": True})
