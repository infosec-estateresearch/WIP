"""WIP audit-log writer.

Architect rule (WO-10B #6): write audit ENTRIES ONLY for actual changes.
Unchanged-row sync touches (last_seen_in_salesforce_at, last_salesforce_sync_at)
are NOT material changes and produce NO audit row.

Entry shape (writes to wip_audit_log per WO-10A migration 003):
- entity_type     — 'wip_company' / 'wip_case' / 'wip_case_financial' / 'wip_status_history'
- entity_id       — row id in the target table
- action          — 'insert' / 'update' / 'soft_delete' / 'reactivate'
- changed_fields_json — for 'update': map of {field: {before, after}} (NULL for insert)
- after_json      — full row state after the change (always present)
- actor           — e.g. 'sf_sync', 'csv_import', operator id
- sync_run_id     — FK to wip_sync_run.id when the change came from a sync (NULL otherwise)
- changed_at      — UTC datetime
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


_AUDITABLE_INSERT_ACTIONS = ("insert",)
_AUDITABLE_UPDATE_ACTIONS = ("update", "soft_delete", "reactivate")


def diff_fields(before: dict, after: dict, ignore: tuple[str, ...] = ()) -> dict[str, dict]:
    """Compare before/after dicts; return {field: {"before": x, "after": y}}
    for every key whose value differs. Keys in `ignore` are skipped (used to
    exclude housekeeping fields like last_*_sync_at).
    """
    changes: dict[str, dict] = {}
    ignore_set = set(ignore)
    all_keys = set(before.keys()) | set(after.keys())
    for k in all_keys:
        if k in ignore_set:
            continue
        b = before.get(k)
        a = after.get(k)
        if b != a:
            changes[k] = {"before": _jsonable(b), "after": _jsonable(a)}
    return changes


def write_audit(
    conn,
    *,
    entity_type: str,
    entity_id: int,
    action: str,
    after: dict,
    changed_fields: dict | None = None,
    actor: str,
    sync_run_id: int | None = None,
) -> None:
    """Append one row to wip_audit_log. Caller decides whether to call —
    if there are no material changes, DON'T call this.
    """
    if action in _AUDITABLE_UPDATE_ACTIONS and not changed_fields:
        # Update-class action but nothing actually changed — caller bug.
        # Refuse to write so we don't pollute the audit with noise.
        return
    now = datetime.now(timezone.utc)
    with conn.cursor() as c:
        c.execute(
            """
            INSERT INTO wip_audit_log
              (entity_type, entity_id, action, changed_fields_json, after_json,
               actor, sync_run_id, changed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                entity_type, entity_id, action,
                json.dumps(changed_fields, default=_jsonable) if changed_fields else None,
                json.dumps(after, default=_jsonable),
                actor, sync_run_id, now,
            ),
        )


def _jsonable(v: Any) -> Any:
    """Coerce non-JSON-native types (datetime, Decimal) to str so json.dumps
    never raises mid-audit. Audit log readability matters more than perfect
    round-tripping.
    """
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v)
