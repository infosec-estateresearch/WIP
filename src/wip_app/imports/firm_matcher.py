"""Firm matcher — looks up a CSV firm name against wip_company.

Architect rules (WO-11):
  - Match in this priority order: SF AccountId → exact SF account name →
    exact account_name_normalised.
  - One match → import; zero → reject NO_COMPANY_MATCH; many → reject
    MULTIPLE_COMPANY_MATCHES.
  - is_potential_duplicate companies: per architect, "Import only if
    unambiguous; otherwise review" — so if the match resolves to a single
    company that's flagged is_potential_duplicate, we still allow import
    (it IS unambiguous) but the operator-flagged duplicate state is visible
    to ERic via v_eric_wip_company_summary.is_potential_duplicate.
  - No fuzzy matching, no LLM. Exact only.

Pre-loads the wip_company lookup once per import so per-row matching is
in-process (no extra DB round trips).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Literal

from src.wip_app.normaliser import normalise_company_name

logger = logging.getLogger("wip.imports.firm_matcher")


MatchResult = Literal["match", "no_match", "multi_match"]


@dataclass
class CompanyMatch:
    result: MatchResult
    wip_company_id: int | None = None
    salesforce_account_id: str | None = None
    canonical_company_id: int | None = None
    is_potential_duplicate: bool = False
    candidate_ids: list[int] = None
    candidate_names: list[str] = None

    def __post_init__(self):
        if self.candidate_ids is None:
            self.candidate_ids = []
        if self.candidate_names is None:
            self.candidate_names = []


@dataclass
class _Row:
    wip_company_id: int
    salesforce_account_id: str
    salesforce_account_name: str
    canonical_company_id: int | None
    is_potential_duplicate: int


class FirmMatcher:
    """In-memory wip_company lookup, built once per import."""

    def __init__(self):
        self._by_sf_id: dict[str, _Row] = {}
        self._by_display_name: dict[str, list[_Row]] = defaultdict(list)
        self._by_normalised: dict[str, list[_Row]] = defaultdict(list)

    @classmethod
    def load(cls, conn) -> "FirmMatcher":
        m = cls()
        with conn.cursor() as c:
            c.execute("""
                SELECT id, salesforce_account_id, salesforce_account_name,
                       account_name_normalised, canonical_company_id,
                       is_potential_duplicate
                  FROM wip_company
                 WHERE is_active_in_salesforce = 1
            """)
            for r in c.fetchall():
                row = _Row(
                    wip_company_id=r["id"],
                    salesforce_account_id=r["salesforce_account_id"],
                    salesforce_account_name=r["salesforce_account_name"],
                    canonical_company_id=r["canonical_company_id"],
                    is_potential_duplicate=int(r["is_potential_duplicate"] or 0),
                )
                m._by_sf_id[row.salesforce_account_id] = row
                m._by_display_name[r["salesforce_account_name"]].append(row)
                if r["account_name_normalised"]:
                    m._by_normalised[r["account_name_normalised"]].append(row)
        logger.info("firm matcher loaded: %d active wip_company rows", len(m._by_sf_id))
        return m

    def match(self, *, firm_name: str | None = None, sf_account_id: str | None = None) -> CompanyMatch:
        """Resolve a CSV firm reference to a single wip_company."""
        candidates: list[_Row] = []

        # 1. SF AccountId direct hit
        if sf_account_id:
            row = self._by_sf_id.get(sf_account_id)
            if row:
                return self._make_match(row)
            # SF id given but not in WIP → no match (don't try free-text)
            return CompanyMatch(result="no_match")

        if not firm_name:
            return CompanyMatch(result="no_match")

        # 2 + 3. UNION of exact display + exact normalised matches. Both paths
        # contribute; if the combined unique set has >1 row, that's multi_match
        # and the caller rejects as MULTIPLE_COMPANY_MATCHES. Display-priority
        # would silently hide a duplicate cluster (e.g. SF has "Hugh James"
        # AND "Hugh James Solicitors" both normalised to "hugh james"); the
        # architect rule is "if multiple matches, reject" so we surface it.
        candidates = list(self._by_display_name.get(firm_name, []))
        norm = normalise_company_name(firm_name)
        if norm:
            candidates.extend(self._by_normalised.get(norm, []))

        # Dedupe by wip_company_id (same row may appear via both maps)
        seen: set[int] = set()
        unique_candidates: list[_Row] = []
        for r in candidates:
            if r.wip_company_id in seen:
                continue
            seen.add(r.wip_company_id)
            unique_candidates.append(r)

        if not unique_candidates:
            return CompanyMatch(result="no_match")
        if len(unique_candidates) == 1:
            return self._make_match(unique_candidates[0])
        return CompanyMatch(
            result="multi_match",
            candidate_ids=[r.wip_company_id for r in unique_candidates],
            candidate_names=[r.salesforce_account_name for r in unique_candidates],
        )

    @staticmethod
    def _make_match(r: _Row) -> CompanyMatch:
        return CompanyMatch(
            result="match",
            wip_company_id=r.wip_company_id,
            salesforce_account_id=r.salesforce_account_id,
            canonical_company_id=r.canonical_company_id,
            is_potential_duplicate=bool(r.is_potential_duplicate),
        )
