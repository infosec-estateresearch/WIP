"""WIP database connection helpers."""

from __future__ import annotations

import pymysql

from src.wip_app.config import eric_canonical_db_settings, wip_db_settings


def wip_connection(autocommit: bool = False):
    """Connect to nif_wip as wip_app. Default autocommit=False so sync code
    can manage its own transactions."""
    s = wip_db_settings()
    return pymysql.connect(
        host=s["host"], port=s["port"], user=s["user"], password=s["password"],
        database=s["database"], charset="utf8mb4", autocommit=autocommit,
        cursorclass=pymysql.cursors.DictCursor,
    )


def eric_canonical_connection():
    """Read-only connection to nif_elt for canonical resolution lookups.

    Always autocommit=True since we only do SELECTs.
    """
    s = eric_canonical_db_settings()
    return pymysql.connect(
        host=s["host"], port=s["port"], user=s["user"], password=s["password"],
        database=s["database"], charset="utf8mb4", autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )
