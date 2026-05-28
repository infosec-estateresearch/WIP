"""Salesforce client + Account fetcher for WIP sync.

Thin wrapper over simple_salesforce. Returns dicts shaped to match what
the runner expects (one dict per Account, normalised field names).

Credential resolution lives in src.wip_app.config — WIP-specific env wins
over ERic fallback.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable

from simple_salesforce import Salesforce

from src.wip_app.config import salesforce_credentials

logger = logging.getLogger("wip.sync.salesforce")


# Fields we mirror into wip_company. Add more here when needed.
_ACCOUNT_SOQL_FIELDS = (
    "Id",
    "Name",
    "Type",
    "Industry",
    "OwnerId",
    "Owner.Name",
    "BillingStreet",
    "BillingCity",
    "BillingPostalCode",
    "BillingCountry",
    "TLP__c",
    "IsDeleted",
    "LastModifiedDate",
)


def make_salesforce_client() -> Salesforce:
    """Build a logged-in Salesforce client using the resolved credentials."""
    c = salesforce_credentials()
    source = c.pop("_source", "?")
    logger.info("salesforce credentials source=%s username=%s", source, c.get("username"))
    return Salesforce(
        username=c["username"],
        password=c["password"],
        security_token="",
        consumer_key=c["consumer_key"],
        consumer_secret=c["consumer_secret"],
        instance_url=c["instance_url"],
    )


def fetch_accounts(
    sf: Salesforce,
    *,
    since: datetime | None = None,
    include_deleted: bool = True,
) -> list[dict]:
    """Fetch Salesforce Accounts.

    Args:
        sf: a logged-in Salesforce client.
        since: if set, filter to Accounts with LastModifiedDate > since
            (incremental sync). None means full fetch.
        include_deleted: True → use queryAll() to include IsDeleted rows
            so we see SF-side hard deletes and can soft-mark them in WIP.

    Returns: list of dicts with keys matching _ACCOUNT_SOQL_FIELDS, plus
    a flattened 'Owner_Name' key derived from the nested Owner.Name.
    """
    fields = ", ".join(_ACCOUNT_SOQL_FIELDS)
    soql = f"SELECT {fields} FROM Account"
    if since is not None:
        # Salesforce expects ISO 8601 UTC, no microseconds, with Z suffix.
        ts = since.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        soql += f" WHERE LastModifiedDate > {ts}"

    logger.info("SOQL: %s", soql)
    runner = sf.query_all if include_deleted else sf.query
    # query_all_iter would be friendlier for memory; AccountQA volume is small enough that
    # query_all into one list is fine.
    result = runner(soql) if not include_deleted else sf.query_all(soql)

    out = []
    for rec in result["records"]:
        flat = _flatten_account(rec)
        out.append(flat)

    logger.info("fetched %d Salesforce Accounts (since=%s, include_deleted=%s)",
                len(out), since, include_deleted)
    return out


def _flatten_account(rec: dict) -> dict:
    """Convert the SF response shape (nested Owner.Name, attributes metadata)
    into a flat dict the runner expects.
    """
    owner = rec.get("Owner") or {}
    owner_name = None
    if isinstance(owner, dict):
        owner.pop("attributes", None)
        owner_name = owner.get("Name")
    out = {
        "Id":                 rec.get("Id"),
        "Name":               rec.get("Name"),
        "Type":               rec.get("Type"),
        "Industry":           rec.get("Industry"),
        "OwnerId":            rec.get("OwnerId"),
        "Owner_Name":         owner_name,
        "BillingStreet":      rec.get("BillingStreet"),
        "BillingCity":        rec.get("BillingCity"),
        "BillingPostalCode":  rec.get("BillingPostalCode"),
        "BillingCountry":     rec.get("BillingCountry"),
        "TLP__c":             rec.get("TLP__c"),
        "IsDeleted":          bool(rec.get("IsDeleted")),
        "LastModifiedDate":   rec.get("LastModifiedDate"),
    }
    return out
