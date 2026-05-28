"""FastAPI dependencies for wip-web.

A single per-request wip_app DB connection. Reuses the existing
src/wip_app/db.wip_connection() helper so credentials and config come
from the same place as the rest of the WIP application.
"""

from __future__ import annotations

from src.wip_app.db import wip_connection


def get_db():
    """Yield a per-request DB connection. Autocommit is intentionally OFF
    so action handlers can wrap multi-statement decisions in a single
    transaction (see src/wip_app/groups/apply.py)."""
    conn = wip_connection(autocommit=False)
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass
