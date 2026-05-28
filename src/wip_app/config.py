"""WIP application configuration / env loader.

Loads /home/WIP/.env (gitignored) and exposes helpers for the WIP-side
database connections and Salesforce credentials.

Credential design (WO-10B): WIP-specific Salesforce env vars are checked
first, with fallback to ERic's NIF_SRC_SALESFORCE_* vars. This means MVP
runs against ERic's read-only Salesforce credentials with no code change,
and a dedicated WIP integration user can be dropped in later just by
setting the WIP-specific env.
"""

from __future__ import annotations

import os
from pathlib import Path


WIP_ENV_PATH = os.environ.get("NIF_WIP_ENV_PATH", "/home/WIP/.env")
ERIC_ENV_PATH = os.environ.get("NIF_ENV_PATH", "/home/ELT/.env")


def load_env() -> None:
    """Load /home/WIP/.env then /home/ELT/.env (in that order — WIP wins).

    Idempotent — repeated calls do nothing if the keys are already set.
    Minimal parser to avoid python-dotenv dependency.
    """
    for path in (WIP_ENV_PATH, ERIC_ENV_PATH):
        p = Path(path)
        if not p.is_file():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip()
            # WIP wins because we load WIP first and use setdefault.
            os.environ.setdefault(k, v)


def wip_db_settings() -> dict:
    """Connection dict for the nif_wip database (wip_app user)."""
    load_env()
    return {
        "host": os.environ["NIF_WIP_DB_HOST"],
        "port": int(os.environ["NIF_WIP_DB_PORT"]),
        "user": os.environ["NIF_WIP_DB_USER"],
        "password": os.environ["NIF_WIP_DB_PASS"],
        "database": os.environ["NIF_WIP_DB_NAME"],
    }


def eric_canonical_db_settings() -> dict:
    """Read-only connection to nif_elt for canonical resolution lookups.

    Uses the same wip_app user — it was granted SELECT on the three
    nif_elt.* canonical-resolution tables during WO-10A bootstrap.
    """
    load_env()
    return {
        "host": os.environ.get("NIF_DB_HOST", os.environ["NIF_WIP_DB_HOST"]),
        "port": int(os.environ.get("NIF_DB_PORT", os.environ["NIF_WIP_DB_PORT"])),
        "user": os.environ.get("NIF_DB_USER", os.environ["NIF_WIP_DB_USER"]),
        "password": os.environ.get("NIF_DB_PASS", os.environ["NIF_WIP_DB_PASS"]),
        "database": os.environ.get("NIF_DB_NAME", "nif_elt"),
    }


def salesforce_credentials() -> dict:
    """Salesforce OAuth credentials.

    Prefers WIP-specific env (NIF_WIP_SF_*) so a dedicated integration
    user can be dropped in later. Falls back to ERic's NIF_SRC_SALESFORCE_*
    for the MVP per the architect's directive: "do not block the WIP spine
    on account provisioning."
    """
    load_env()
    e = os.environ

    def _pick(*keys: str, required: bool = True) -> str | None:
        for k in keys:
            if e.get(k):
                return e[k]
        if required:
            raise KeyError(f"none of {keys} set in env")
        return None

    return {
        "username":        _pick("NIF_WIP_SF_USER",          "NIF_SRC_SALESFORCE_USER"),
        "password":        _pick("NIF_WIP_SF_PASS",          "NIF_SRC_SALESFORCE_PASS"),
        "consumer_key":    _pick("NIF_WIP_SF_CONSUMER_KEY",  "NIF_SRC_SALESFORCE_CONSUMER_KEY"),
        "consumer_secret": _pick("NIF_WIP_SF_CONSUMER_SECRET", "NIF_SRC_SALESFORCE_CONSUMER_SECRET"),
        "instance_url":    _pick("NIF_WIP_SF_INSTANCE_URL",  "NIF_SRC_SALESFORCE_INSTANCE_URL"),
        "_source":         "wip_specific" if e.get("NIF_WIP_SF_USER") else "eric_fallback",
    }
