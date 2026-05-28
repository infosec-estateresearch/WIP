"""Canonical-company resolver — cross-DB lookup against nif_elt.

Mirrors the three deterministic resolution paths used by ERic
(`firm_observation._build_canonical_lookup` in /home/ELT):

  A. canonical_company.canonical_name           = display name
  B. entity_alias.alias_normalised              = normalised name
  C. sf_account_company_bridge.sf_account_name  = display name

The resolver pre-loads ALL three lookup maps into memory at construction
time (one cross-DB round trip), then resolves each WIP company in-process.

For MVP we run on the same MariaDB instance so the cross-DB read is cheap.
The interface (resolve(display, normalised) → cc_id_or_None) is what the
runner depends on, so swapping to an API or a denormalised local mirror
later requires no runner change — that's the abstraction the architect
asked for.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal

from src.wip_app.db import eric_canonical_connection

logger = logging.getLogger("wip.sync.canonical_resolver")


Resolution = Literal["resolved", "ambiguous", "unresolved"]


@dataclass
class CanonicalResolver:
    by_display: dict = field(default_factory=lambda: defaultdict(set))
    by_normalised: dict = field(default_factory=lambda: defaultdict(set))

    @classmethod
    def load(cls) -> "CanonicalResolver":
        """Pre-load all 3 paths from nif_elt (one cross-DB connection)."""
        r = cls()
        conn = eric_canonical_connection()
        try:
            with conn.cursor() as c:
                c.execute(
                    "SELECT id, canonical_name FROM canonical_company "
                    "WHERE status='active'"
                )
                for row in c.fetchall():
                    r.by_display[row["canonical_name"]].add(row["id"])

                c.execute(
                    "SELECT entity_id, alias_normalised FROM entity_alias "
                    "WHERE entity_type='company' AND alias_normalised IS NOT NULL"
                )
                for row in c.fetchall():
                    r.by_normalised[row["alias_normalised"]].add(row["entity_id"])

                c.execute(
                    "SELECT canonical_company_id, sf_account_name "
                    "FROM sf_account_company_bridge "
                    "WHERE canonical_company_id IS NOT NULL"
                )
                for row in c.fetchall():
                    r.by_display[row["sf_account_name"]].add(row["canonical_company_id"])
        finally:
            conn.close()

        logger.info(
            "canonical resolver loaded — display_keys=%d normalised_keys=%d",
            len(r.by_display), len(r.by_normalised),
        )
        return r

    def resolve(self, display: str | None, normalised: str | None) -> tuple[int | None, Resolution]:
        """Return (canonical_company_id, resolution_status).

        - exactly 1 candidate → ('resolved', cc_id)
        - >1 candidates       → ('ambiguous', None)   — caller leaves NULL
        - 0 candidates        → ('unresolved', None)  — caller leaves NULL
        """
        if not display and not normalised:
            return None, "unresolved"
        candidates: set = set()
        if display:
            candidates |= self.by_display.get(display, set())
        if normalised:
            candidates |= self.by_normalised.get(normalised, set())
        if len(candidates) == 1:
            return next(iter(candidates)), "resolved"
        if len(candidates) > 1:
            return None, "ambiguous"
        return None, "unresolved"
