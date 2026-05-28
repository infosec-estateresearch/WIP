"""WO-13 — Salesforce duplicate / canonical alignment review (READ-ONLY).

Produces a review report and two operator-ready CSVs that map where the
Salesforce ↔ WIP ↔ ERic canonical identity space is fragmented, duplicated,
or misaligned. The report is descriptive and ranked by business impact;
it does NOT mutate any table, write to Salesforce, or merge anything.

Data sources:
  - nif_wip.wip_company (active SF Account mirror)
  - nif_wip.wip_company_duplicate_candidate (WIP sync's own duplicate detector)
  - nif_wip.wip_import_reject (firms that didn't import — NO_COMPANY_MATCH /
    MULTIPLE_COMPANY_MATCHES)
  - nif_wip.v_eric_wip_firm_with_wip (current WIP totals per firm)
  - nif_elt.canonical_company (ERic's canonical firm list)
  - nif_elt.sf_account_company_bridge (SF ↔ ERic canonical bridge)
  - nif_elt.canonical_person + nif_elt.webinar_attendance (engagement signal)
  - nif_elt.person_company_relationship (person → firm)

Output:
  - ops/evidence/WO-13/sf_canonical_alignment_report.md
  - ops/evidence/WO-13/sf_canonical_alignment_candidates.csv
  - ops/evidence/WO-13/high_impact_duplicate_groups.csv

Usage:
  python3 ops/evidence/WO-13/generate_alignment_report.py
"""

from __future__ import annotations

import csv
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pymysql
import pymysql.cursors


# Cap how many clusters land in the "high-impact" CSV — operator review is
# a finite human resource. The full candidates CSV stays uncapped.
TOP_N_HIGH_IMPACT = 50

# Score weights for business-impact ranking. A cluster with £10k WIP and
# 5 webinar attendees gets the same priority as one with £5k WIP and 105
# attendees — both signal user-visible firms.
WEIGHT_WIP_PER_K = 1.0          # £1,000 of WIP = 1 point
WEIGHT_ACTIVE_CASE = 5.0        # one open case with WIP = 5 points
WEIGHT_WEBINAR_ATTENDEE = 2.0   # one webinar attendee = 2 points
WEIGHT_REJECT_ROW = 1.0         # one matching reject row = 1 point


# ---------------------------------------------------------------------------
# Categories — bound to the operator buckets the architect specified.
# ---------------------------------------------------------------------------
CAT_SF_DUPLICATE          = "SF_DUPLICATE_GROUP"              # 2+ SF Accounts likely same firm
CAT_CANONICAL_FRAGMENT    = "CANONICAL_FRAGMENTATION"         # ERic split across multiple canonical_ids
CAT_ERIC_NOT_IN_WIP       = "ERIC_CANONICAL_NOT_MAPPED_TO_WIP"# ERic knows firm; SF accounts exist; bridge missing
CAT_BRANCH_PER_CITY       = "BRANCH_PER_CITY_STRUCTURE"       # legitimate branches; needs business rule
CAT_FALSE_POSITIVE        = "FALSE_POSITIVE_DUPLICATE"        # similar names, distinct firms
CAT_NEEDS_SF_MERGE        = "NEEDS_SALESFORCE_MERGE"          # identity fix belongs in SF
CAT_NEEDS_BRIDGE_REPAIR   = "NEEDS_CANONICAL_BRIDGE_REPAIR"   # SF ok; ERic bridge wrong/incomplete
CAT_UNMAPPED_SF_ACCOUNT   = "UNMAPPED_SF_ACCOUNT"             # SF account exists in WIP but has no canonical_id


@dataclass
class Cluster:
    cluster_id: str
    category: str
    group_label: str
    wip_company_ids: list[int] = field(default_factory=list)
    salesforce_account_ids: list[str] = field(default_factory=list)
    account_names: list[str] = field(default_factory=list)
    canonical_company_ids: list[int] = field(default_factory=list)
    canonical_names: list[str] = field(default_factory=list)
    distinct_cities: list[str] = field(default_factory=list)
    distinct_postcodes: list[str] = field(default_factory=list)
    distinct_account_types: list[str] = field(default_factory=list)
    total_current_wip: Decimal = Decimal("0")
    active_case_count: int = 0
    webinar_attendees: int = 0
    import_reject_count: int = 0
    impact_score: float = 0.0
    recommendation: str = ""
    evidence_notes: str = ""

    def score(self) -> float:
        return (
            (float(self.total_current_wip) / 1000.0) * WEIGHT_WIP_PER_K
            + self.active_case_count * WEIGHT_ACTIVE_CASE
            + self.webinar_attendees * WEIGHT_WEBINAR_ATTENDEE
            + self.import_reject_count * WEIGHT_REJECT_ROW
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Strip any parenthetical content first so "(Cardiff)" / "(Potter Rees Dolan)"
# don't pollute the base name. Then apply the WIP normaliser, which strips
# common firm suffixes ("Solicitors", "LLP", "& Co", etc.). The result is the
# cluster key — so "Hugh James Solicitors", "Hugh James (Cardiff)", and
# "Hugh James (London)" all collapse to "hugh james".
PARENS_RE = re.compile(r"\([^)]*\)")
CITY_IN_PARENS_RE = re.compile(r"\(([^)]+)\)")

# Import the WIP normaliser so we share the exact same firm-suffix logic the
# import + sync pipelines use. Falls back to a local lowercase if WIP isn't
# on PYTHONPATH (defensive; the script normally runs from /home/WIP).
try:
    sys.path.insert(0, "/home/WIP")
    from src.wip_app.normaliser import normalise_company_name as _norm
except Exception:
    def _norm(s):  # type: ignore
        return (s or "").lower().strip()


def fragmentation_key(name: str) -> str:
    """Canonical-fragmentation grouping key. Drops parenthetical content
    (city / variant), then normalises firm suffixes."""
    if not name:
        return ""
    no_parens = PARENS_RE.sub(" ", name)
    return _norm(no_parens)


def city_from_name(name: str) -> str | None:
    """If the canonical/SF name contains '(City)', return 'City'."""
    if not name:
        return None
    m = CITY_IN_PARENS_RE.search(name)
    return m.group(1).strip() if m else None


def fmt_money(d: Decimal | float) -> str:
    return "£%s" % f"{float(d):,.2f}"


def fmt_list(items, limit=8, sep=" | "):
    """Comma-safe joiner that truncates long lists for CSV readability."""
    items = [str(x) for x in items if x is not None and str(x).strip() != ""]
    if len(items) > limit:
        return sep.join(items[:limit]) + f" ... (+{len(items) - limit} more)"
    return sep.join(items)


def fmt_id_list(items, sep=" | "):
    """ID-only joiner — NEVER truncate. The full CSV is consumed by the
    WO-14A.1 seed proposer (which parses these strings back into SF /
    canonical IDs to look up wip_company rows). Truncating produces
    under-membered groups."""
    items = [str(x) for x in items if x is not None and str(x).strip() != ""]
    return sep.join(items)


def wip_conn():
    return pymysql.connect(
        host="localhost", port=3336,
        user="wip_app", password="QuQZJyCSLK3Fys62sCX1gttH",
        database="nif_wip",
        cursorclass=pymysql.cursors.DictCursor,
    )


def elt_conn():
    return pymysql.connect(
        host="localhost", port=3336,
        user="nif_app", password="N1f-3lt-App-2026!",
        database="nif_elt",
        cursorclass=pymysql.cursors.DictCursor,
    )


# ---------------------------------------------------------------------------
# Stage 1 — Pull
# ---------------------------------------------------------------------------

def load_wip_companies(w):
    with w.cursor() as c:
        c.execute("""
            SELECT id, salesforce_account_id, salesforce_account_name,
                   account_name_normalised, duplicate_group_key,
                   is_potential_duplicate, account_type, industry,
                   billing_city, billing_postcode, canonical_company_id
              FROM wip_company
             WHERE is_active_in_salesforce = 1
        """)
        return list(c.fetchall())


def load_duplicate_candidates(w):
    with w.cursor() as c:
        c.execute("""
            SELECT id, duplicate_group_key, salesforce_account_ids_json,
                   account_names_json, candidate_count, status
              FROM wip_company_duplicate_candidate
             WHERE status IN ('NEW', 'NEEDS_REVIEW')
        """)
        return list(c.fetchall())


def load_wip_currencies(w):
    """canonical_company_id → (total_current_wip, active_case_count) from
    the approved firm-with-wip view."""
    out: dict[int, tuple[Decimal, int]] = {}
    with w.cursor() as c:
        c.execute("""
            SELECT canonical_company_id, total_current_wip, active_case_count
              FROM v_eric_wip_firm_with_wip
             WHERE canonical_company_id IS NOT NULL
        """)
        for r in c.fetchall():
            cid = int(r["canonical_company_id"])
            cur = out.get(cid, (Decimal("0"), 0))
            out[cid] = (
                cur[0] + (r["total_current_wip"] or Decimal("0")),
                cur[1] + int(r["active_case_count"] or 0),
            )
    return out


def load_reject_counts_by_normalised(w):
    """firm_name_normalised → count of NEW/NEEDS_REVIEW rejects."""
    out: dict[str, int] = {}
    with w.cursor() as c:
        c.execute("""
            SELECT firm_name_normalised, COUNT(*) AS n
              FROM wip_import_reject
             WHERE resolution_status IN ('NEW', 'NEEDS_REVIEW')
               AND firm_name_normalised IS NOT NULL
               AND firm_name_normalised <> ''
             GROUP BY firm_name_normalised
        """)
        for r in c.fetchall():
            out[r["firm_name_normalised"]] = int(r["n"] or 0)
    return out


def load_canonical_companies(e):
    """ERic canonical_company rows (active only) keyed by id."""
    out: dict[int, dict] = {}
    with e.cursor() as c:
        c.execute("""
            SELECT id, canonical_name, status, source_count
              FROM canonical_company
             WHERE status = 'active'
        """)
        for r in c.fetchall():
            out[int(r["id"])] = r
    return out


def load_sf_bridge(e):
    """sf_account_id → (canonical_company_id, canonical_company_name,
    match_method, match_confidence)."""
    out: dict[str, dict] = {}
    with e.cursor() as c:
        c.execute("""
            SELECT sf_account_id, sf_account_name, canonical_company_id,
                   canonical_company_name, match_method, match_confidence
              FROM sf_account_company_bridge
        """)
        for r in c.fetchall():
            out[r["sf_account_id"]] = r
    return out


def load_webinar_attendees_by_canonical(e):
    """canonical_company_id → number of distinct webinar attendees from
    that firm. Engagement-impact signal."""
    out: dict[int, int] = {}
    with e.cursor() as c:
        c.execute("""
            SELECT pcr.company_id, COUNT(DISTINCT wa.person_id) AS n
              FROM person_company_relationship pcr
              JOIN webinar_attendance wa ON wa.person_id = pcr.person_id
             WHERE pcr.status IN ('active', 'unverified')
               AND pcr.company_id IS NOT NULL
             GROUP BY pcr.company_id
        """)
        for r in c.fetchall():
            out[int(r["company_id"])] = int(r["n"] or 0)
    return out


# ---------------------------------------------------------------------------
# Stage 2 — Build clusters
# ---------------------------------------------------------------------------

def build_wip_duplicate_clusters(wip_companies, dup_candidates):
    """One cluster per duplicate_group_key in wip_company_duplicate_candidate.

    The candidate row tells us this group has 2+ SF Accounts sharing the
    same normalised name. We pull the matching wip_company rows by
    duplicate_group_key (since the WIP-side detector already populated it).
    """
    by_dgk: dict[str, list[dict]] = defaultdict(list)
    for w in wip_companies:
        if w["duplicate_group_key"]:
            by_dgk[w["duplicate_group_key"]].append(w)

    clusters: list[Cluster] = []
    for cand in dup_candidates:
        dgk = cand["duplicate_group_key"]
        members = by_dgk.get(dgk, [])
        if len(members) < 2:
            continue
        c = Cluster(
            cluster_id="WIP-DUP-%d" % cand["id"],
            category="",  # filled by classifier
            group_label=dgk,
        )
        for m in members:
            c.wip_company_ids.append(int(m["id"]))
            c.salesforce_account_ids.append(m["salesforce_account_id"])
            c.account_names.append(m["salesforce_account_name"])
            if m["canonical_company_id"]:
                c.canonical_company_ids.append(int(m["canonical_company_id"]))
            if m["billing_city"]:
                c.distinct_cities.append(m["billing_city"])
            if m["billing_postcode"]:
                c.distinct_postcodes.append(m["billing_postcode"])
            if m["account_type"]:
                c.distinct_account_types.append(m["account_type"])
        # Dedup
        c.distinct_cities = sorted(set(c.distinct_cities))
        c.distinct_postcodes = sorted(set(c.distinct_postcodes))
        c.distinct_account_types = sorted(set(c.distinct_account_types))
        c.canonical_company_ids = sorted(set(c.canonical_company_ids))
        clusters.append(c)
    return clusters


def _merge_sub_clusters(by_base: dict) -> dict:
    """Some firms have inconsistent naming — "Hugh James (Cardiff)" and
    "Hugh James Cardiff" produce different fragmentation_keys ("hugh james"
    vs "hugh james cardiff"). This pass merges a sub-key into a parent
    when (a) the parent already has 2+ canonicals, AND (b) the sub-key's
    remainder is a single token (likely a city). This is conservative —
    we never merge two parents together, only fold orphan branches in.
    """
    parents = {k: v for k, v in by_base.items() if len(v) >= 2}
    out = {k: list(v) for k, v in by_base.items()}
    for sub_key, sub_members in list(by_base.items()):
        if sub_key in parents:
            continue
        for parent_key in parents:
            prefix = parent_key + " "
            if sub_key.startswith(prefix):
                remainder = sub_key[len(prefix):].strip()
                if remainder and " " not in remainder:
                    # Single-token remainder — likely a city/branch label
                    out[parent_key].extend(sub_members)
                    out.pop(sub_key, None)
                    break
    return out


def build_canonical_fragmentation_clusters(canonical_by_id, wip_companies):
    """Find ERic canonical_companies whose name shares a base prefix with
    other ERic canonicals AND at least one of those canonicals is mapped
    to a wip_company. The Hugh James case: 'Hugh James Solicitors',
    'Hugh James (Cardiff)', 'Hugh James (London)', etc.

    Strategy: group active canonicals by fragmentation_key(canonical_name).
    A base name with 2+ members is a fragmentation candidate. We only
    surface clusters where at least one member appears in wip_company
    (otherwise it's an ERic-only naming issue, out of WO-13 scope).
    """
    wip_canonical_ids = {
        int(w["canonical_company_id"]) for w in wip_companies
        if w["canonical_company_id"]
    }

    by_base: dict[str, list[dict]] = defaultdict(list)
    for cc in canonical_by_id.values():
        base = fragmentation_key(cc["canonical_name"])
        if not base or len(base) < 3:
            continue
        by_base[base].append(cc)
    by_base = _merge_sub_clusters(dict(by_base))

    # Also build wip_company-by-canonical for cluster enrichment
    wip_by_canonical: dict[int, list[dict]] = defaultdict(list)
    for w in wip_companies:
        if w["canonical_company_id"]:
            wip_by_canonical[int(w["canonical_company_id"])].append(w)

    clusters: list[Cluster] = []
    for base, members in by_base.items():
        if len(members) < 2:
            continue
        wip_members = [m for m in members if int(m["id"]) in wip_canonical_ids]
        if not wip_members:
            continue
        # Skip if it's already captured by the WIP-side dup-detector
        # (single duplicate_group_key on the WIP side)
        wip_dgks = set()
        for cc in wip_members:
            for w in wip_by_canonical.get(int(cc["id"]), []):
                if w["duplicate_group_key"]:
                    wip_dgks.add(w["duplicate_group_key"])
        if len(wip_dgks) <= 1 and wip_dgks:
            # All WIP members share the same dgk — that's the WIP-DUP case,
            # don't double-count.
            continue

        c = Cluster(
            cluster_id="CC-FRAG-%s" % base.replace(" ", "-")[:40],
            category="",
            group_label=base,
        )
        for cc in members:
            c.canonical_company_ids.append(int(cc["id"]))
            c.canonical_names.append(cc["canonical_name"])
            # Even canonicals with no WIP-side row contribute a city signal
            # via their parenthetical name suffix — useful evidence for the
            # branch/per-city pattern.
            city_from_canonical = city_from_name(cc["canonical_name"])
            if city_from_canonical:
                c.distinct_cities.append(city_from_canonical)
            for w in wip_by_canonical.get(int(cc["id"]), []):
                c.wip_company_ids.append(int(w["id"]))
                c.salesforce_account_ids.append(w["salesforce_account_id"])
                c.account_names.append(w["salesforce_account_name"])
                if w["billing_city"]:
                    c.distinct_cities.append(w["billing_city"])
                else:
                    city_from_sf = city_from_name(w["salesforce_account_name"])
                    if city_from_sf:
                        c.distinct_cities.append(city_from_sf)
                if w["billing_postcode"]:
                    c.distinct_postcodes.append(w["billing_postcode"])
                if w["account_type"]:
                    c.distinct_account_types.append(w["account_type"])
        c.canonical_company_ids = sorted(set(c.canonical_company_ids))
        c.distinct_cities = sorted(set(c.distinct_cities))
        c.distinct_postcodes = sorted(set(c.distinct_postcodes))
        c.distinct_account_types = sorted(set(c.distinct_account_types))
        clusters.append(c)
    return clusters


def build_unmapped_wip_clusters(wip_companies):
    """wip_company rows that are active in Salesforce but have no
    canonical_company_id — ERic can't resolve them, so the WIP→ERic bridge
    is broken for these firms."""
    clusters: list[Cluster] = []
    unmapped = [w for w in wip_companies if not w["canonical_company_id"]]
    if not unmapped:
        return clusters
    # Aggregate by normalised name (so a duplicate cluster of unmapped
    # firms appears as one cluster, not N rows)
    by_norm: dict[str, list[dict]] = defaultdict(list)
    for w in unmapped:
        norm = w["account_name_normalised"] or w["salesforce_account_name"] or "(unknown)"
        by_norm[norm].append(w)
    for norm, members in by_norm.items():
        c = Cluster(
            cluster_id="UNMAPPED-%s" % norm.replace(" ", "-")[:40],
            category=CAT_UNMAPPED_SF_ACCOUNT,
            group_label=norm,
        )
        for m in members:
            c.wip_company_ids.append(int(m["id"]))
            c.salesforce_account_ids.append(m["salesforce_account_id"])
            c.account_names.append(m["salesforce_account_name"])
            if m["billing_city"]:
                c.distinct_cities.append(m["billing_city"])
            if m["billing_postcode"]:
                c.distinct_postcodes.append(m["billing_postcode"])
        c.distinct_cities = sorted(set(c.distinct_cities))
        c.distinct_postcodes = sorted(set(c.distinct_postcodes))
        clusters.append(c)
    return clusters


# ---------------------------------------------------------------------------
# Stage 3 — Classify
# ---------------------------------------------------------------------------

def classify(c: Cluster) -> None:
    """Assign a category + recommendation to a cluster. Operates on the
    populated fields only — no DB access."""
    # Already pre-classified (unmapped)
    if c.category:
        if c.category == CAT_UNMAPPED_SF_ACCOUNT:
            c.recommendation = (
                "Salesforce Account exists but ERic has not resolved it to a "
                "canonical_company. Either (a) seed a canonical_company in "
                "ERic and add to sf_account_company_bridge, or (b) confirm "
                "this SF Account is genuinely out-of-scope and mark it so."
            )
            c.evidence_notes = "active in Salesforce; canonical_company_id IS NULL"
        return

    n_accounts = len(c.salesforce_account_ids)
    n_cities = len(c.distinct_cities)
    n_postcodes = len(c.distinct_postcodes)
    n_types = len(c.distinct_account_types)
    n_canonicals = len(c.canonical_company_ids)

    if c.cluster_id.startswith("CC-FRAG-"):
        # Same base name fragmented across multiple ERic canonicals AND
        # multiple Salesforce Accounts — Hugh James pattern.
        c.category = CAT_CANONICAL_FRAGMENT
        if n_cities >= 2:
            c.recommendation = (
                "Branch/per-city pattern. Decide whether '%s' is one parent "
                "firm with location offices (then ERic needs a parent canonical "
                "+ child-canonical hierarchy or wip_company_group) or genuinely "
                "separate practices. Recommend keeping Salesforce as-is until "
                "the parent/group question is decided."
            ) % c.group_label
        else:
            c.recommendation = (
                "Same firm split across multiple ERic canonicals without an "
                "obvious branch reason. Likely needs canonical merging in ERic, "
                "not a Salesforce change."
            )
        c.evidence_notes = (
            "%d ERic canonicals, %d SF accounts, %d cities, %d postcodes"
            % (n_canonicals, n_accounts, n_cities, n_postcodes)
        )
        return

    # WIP-detected duplicate group (CAT_SF_DUPLICATE path)
    if n_types >= 2:
        c.category = CAT_FALSE_POSITIVE
        c.recommendation = (
            "Names match but account_types differ — likely distinct firms "
            "that happen to share a name root. Recommend IGNORED_FALSE_POSITIVE."
        )
    elif n_cities >= 2 or n_postcodes >= 2:
        c.category = CAT_BRANCH_PER_CITY
        c.recommendation = (
            "Multiple SF Accounts across distinct locations — likely "
            "legitimate branches. Surface to commercial team for the "
            "parent/group company decision before any merge."
        )
    else:
        c.category = CAT_NEEDS_SF_MERGE
        c.recommendation = (
            "%d SF Accounts share the same name AND the same location — "
            "high-confidence Salesforce duplicate. Recommend merge IN "
            "SALESFORCE (not in WIP or ERic)."
        ) % n_accounts
    c.evidence_notes = (
        "%d SF accounts, %d distinct city/cities, %d distinct postcodes, "
        "%d distinct account_types"
        % (n_accounts, n_cities, n_postcodes, n_types)
    )


# ---------------------------------------------------------------------------
# Stage 4 — Enrich with business-impact signals
# ---------------------------------------------------------------------------

def enrich(c: Cluster, wip_currencies, webinar_attendees, reject_counts) -> None:
    for cid in c.canonical_company_ids:
        wip_value, cases = wip_currencies.get(int(cid), (Decimal("0"), 0))
        c.total_current_wip += wip_value
        c.active_case_count += cases
        c.webinar_attendees += webinar_attendees.get(int(cid), 0)
    # Reject queue rows referencing this group_label as normalised firm name
    c.import_reject_count = reject_counts.get(c.group_label, 0)
    c.impact_score = c.score()


# ---------------------------------------------------------------------------
# Stage 5 — Emit
# ---------------------------------------------------------------------------

def write_full_csv(clusters: list[Cluster], path: Path) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "cluster_id", "category", "group_label",
            "wip_company_count", "salesforce_account_ids", "account_names",
            "canonical_company_count", "canonical_company_ids", "canonical_names",
            "distinct_cities", "distinct_postcodes_count",
            "distinct_account_types",
            "total_current_wip_gbp", "active_case_count",
            "webinar_attendees", "import_reject_count",
            "impact_score", "recommendation", "evidence_notes",
        ])
        for c in clusters:
            w.writerow([
                c.cluster_id, c.category, c.group_label,
                len(c.wip_company_ids),
                fmt_id_list(c.salesforce_account_ids),
                fmt_list(c.account_names),
                len(c.canonical_company_ids),
                fmt_id_list(c.canonical_company_ids),
                fmt_list(c.canonical_names),
                fmt_list(c.distinct_cities),
                len(c.distinct_postcodes),
                fmt_list(c.distinct_account_types),
                f"{float(c.total_current_wip):.2f}",
                c.active_case_count,
                c.webinar_attendees,
                c.import_reject_count,
                f"{c.impact_score:.1f}",
                c.recommendation,
                c.evidence_notes,
            ])


def write_high_impact_csv(clusters: list[Cluster], path: Path, top_n: int) -> None:
    ranked = sorted(clusters, key=lambda c: c.impact_score, reverse=True)[:top_n]
    write_full_csv(ranked, path)


def write_report(clusters: list[Cluster], path: Path, totals: dict) -> None:
    ranked = sorted(clusters, key=lambda c: c.impact_score, reverse=True)
    by_cat: dict[str, list[Cluster]] = defaultdict(list)
    for c in clusters:
        by_cat[c.category].append(c)

    def section_header(title, level=2):
        return ("#" * level) + " " + title + "\n\n"

    lines: list[str] = []
    lines.append(section_header("WO-13 — Salesforce duplicate / canonical alignment review", 1))
    lines.append(
        "Read-only review of where the Salesforce ↔ WIP ↔ ERic canonical "
        "identity space is fragmented, duplicated, or misaligned. "
        "**No mutations.** Operator review only.\n\n"
    )
    lines.append("Generated: %s UTC\n\n" % datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))

    lines.append(section_header("Headline numbers", 2))
    lines.append("| Signal | Count |\n|---|---|\n")
    lines.append("| Active SF Accounts in WIP | %d |\n" % totals["wip_active"])
    lines.append("| Active SF Accounts with no canonical bridge | %d |\n" % totals["wip_unmapped"])
    lines.append("| WIP-detected duplicate-group candidates (NEW/NEEDS_REVIEW) | %d |\n" % totals["wip_dup_candidates"])
    lines.append("| ERic canonical_companies in the bridge | %d |\n" % totals["bridge_canonicals"])
    lines.append("| Clusters surfaced by this report | %d |\n" % len(clusters))
    lines.append("| Open import-reject rows (NEW/NEEDS_REVIEW) | %d |\n" % totals["open_rejects"])
    lines.append("\n")

    lines.append(section_header("Bucket totals", 2))
    lines.append("| Category | Clusters | Σ current WIP | Σ webinar attendees | Σ import rejects |\n")
    lines.append("|---|---:|---:|---:|---:|\n")
    bucket_order = [
        CAT_NEEDS_SF_MERGE, CAT_BRANCH_PER_CITY, CAT_CANONICAL_FRAGMENT,
        CAT_FALSE_POSITIVE, CAT_ERIC_NOT_IN_WIP, CAT_NEEDS_BRIDGE_REPAIR,
        CAT_UNMAPPED_SF_ACCOUNT, CAT_SF_DUPLICATE,
    ]
    for cat in bucket_order:
        members = by_cat.get(cat, [])
        if not members:
            continue
        sum_wip = sum((m.total_current_wip for m in members), Decimal("0"))
        sum_attend = sum(m.webinar_attendees for m in members)
        sum_rej = sum(m.import_reject_count for m in members)
        lines.append("| %s | %d | %s | %d | %d |\n" % (
            cat, len(members), fmt_money(sum_wip), sum_attend, sum_rej,
        ))
    lines.append("\n")

    # Hugh James seed — explicit ground truth, not derived
    lines.append(section_header("Hugh James — seed case", 2))
    lines.append(
        "WO-12 surfaced this pattern. ERic users asking about \"Hugh James\" hit "
        "ERic canonical_company_id **13030** (\"Hugh James Solicitors\"). The WIP "
        "application has five sibling Salesforce Accounts under per-city canonical "
        "IDs, none of which is 13030:\n\n"
    )
    lines.append("| wip_company | SF Account ID | SF Account name | canonical_id (WIP) | Current WIP |\n")
    lines.append("|---:|---|---|---:|---:|\n")
    lines.append("| 2512 | 0015800000E2xdbAAB | Hugh James (Cardiff)     | 20425 | £75.00 |\n")
    lines.append("| 3193 | 0015800000HzXcoAAF | Hugh James (London)      | 23743 | £0.00  |\n")
    lines.append("| 7222 | 001Tv00000PnXxQIAV | Hugh James (Plymouth)    | 21545 | £0.00  |\n")
    lines.append("| 7223 | 001Tv00000Pne4NIAR | Hugh James (Southampton) | 21547 | £0.00  |\n")
    lines.append("| 7224 | 001Tv00000Pnj8rIAB | Hugh James (Manchester)  | 21549 | £0.00  |\n")
    lines.append("\n")
    lines.append(
        "ERic also has additional Hugh James canonicals not in WIP at all:\n\n"
        "- **13030** — `Hugh James Solicitors` (the one ERic resolves \"Hugh James\" to; 51 webinar attendees on record)\n"
        "- **18724** — `Hugh James Solicirors` (typo variant)\n"
        "- **19311** — `Hugh James (Potter Rees Dolan)` (sub-firm/branding variant)\n\n"
        "Effect: `firm_summary` for \"Hugh James\" correctly reports "
        "`wip_status = not_mapped_to_wip`, because canonical 13030 is not in "
        "the WIP-side bridge — even though Hugh James (Cardiff) holds £75 of "
        "current WIP under canonical 20425. The new path is exposing the real "
        "shape: one commercial firm, eight ERic canonical identities, five "
        "Salesforce Accounts, fragmented bridge.\n\n"
        "Required business decision: should Hugh James be represented as one "
        "parent firm, multiple branch accounts, or a parent-child hierarchy? "
        "Whatever the answer, it should be **made in Salesforce first**, then "
        "flow downstream via the hourly sync.\n\n"
    )

    # Critical design question
    lines.append(section_header("Design question to resolve before WO-14", 2))
    lines.append(
        "**Do we treat Salesforce branch/location Accounts as separate WIP "
        "companies, or do we need a parent-company grouping layer?**\n\n"
        "Two options:\n\n"
        "- **Option A (one-account-one-company):** keep the current model. "
        "Branch totals add up, but ERic users asking about the parent firm "
        "hit `not_mapped_to_wip`. Caveat surfaces it but the answer is "
        "fragmentary.\n"
        "- **Option B (introduce `wip_company_group` or "
        "`salesforce_parent_account_id`):** add a parent-firm layer so "
        "ERic can roll up branch WIP under the canonical parent. Cleaner "
        "for user queries; requires either (i) a Salesforce parent_account_id "
        "column flowing through the sync, or (ii) an ERic-side curated "
        "parent→child canonical mapping.\n\n"
        "Recommendation: do not design Option B until the operator review "
        "of this report tells us how many clusters are genuine branches vs "
        "SF duplicates vs false positives. The bucket totals above are the "
        "first input to that decision.\n\n"
    )

    # Per-cluster details
    lines.append(section_header("Top %d clusters by business impact" % min(TOP_N_HIGH_IMPACT, len(ranked)), 2))
    lines.append("Ranked by `score = (£WIP / 1000) + 5·active_cases + 2·webinar_attendees + 1·open_rejects`.\n\n")
    lines.append("| # | Cluster | Category | SF accts | Canonicals | Cities | £ WIP | Cases | Webinar | Rejects | Score |\n")
    lines.append("|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|\n")
    for i, c in enumerate(ranked[:TOP_N_HIGH_IMPACT], 1):
        lines.append(
            "| %d | %s | %s | %d | %d | %d | %s | %d | %d | %d | %.1f |\n" % (
                i, c.group_label, c.category,
                len(c.salesforce_account_ids),
                len(c.canonical_company_ids),
                len(c.distinct_cities),
                fmt_money(c.total_current_wip),
                c.active_case_count, c.webinar_attendees,
                c.import_reject_count, c.impact_score,
            )
        )
    lines.append("\n")

    lines.append(section_header("Files in this evidence package", 2))
    lines.append(
        "- `sf_canonical_alignment_report.md` — this narrative report\n"
        "- `sf_canonical_alignment_candidates.csv` — every cluster surfaced, full columns\n"
        "- `high_impact_duplicate_groups.csv` — top %d clusters by impact score\n"
        "- `generate_alignment_report.py` — the read-only script that produced these\n\n"
        % TOP_N_HIGH_IMPACT
    )

    lines.append(section_header("What WO-13 does NOT do", 2))
    lines.append(
        "- No automatic merges (Salesforce owns the merge — see [[feedback-salesforce-readonly]])\n"
        "- No `canonical_company` updates in ERic\n"
        "- No `entity_alias` writes\n"
        "- No `wip_company` mutations\n"
        "- No reject-queue bulk processing\n"
        "- No ERic behaviour changes\n\n"
    )

    with open(path, "w") as f:
        f.writelines(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    out_dir = Path(__file__).resolve().parent
    out_dir.mkdir(parents=True, exist_ok=True)

    sys.stderr.write("Connecting to nif_wip + nif_elt...\n")
    w = wip_conn()
    e = elt_conn()
    try:
        sys.stderr.write("Loading wip_company...\n")
        wip_companies = load_wip_companies(w)
        sys.stderr.write("Loading wip_company_duplicate_candidate...\n")
        dup_candidates = load_duplicate_candidates(w)
        sys.stderr.write("Loading WIP currency totals...\n")
        wip_currencies = load_wip_currencies(w)
        sys.stderr.write("Loading reject counts...\n")
        reject_counts = load_reject_counts_by_normalised(w)

        sys.stderr.write("Loading ERic canonical_company...\n")
        canonical_by_id = load_canonical_companies(e)
        sys.stderr.write("Loading sf_account_company_bridge...\n")
        sf_bridge = load_sf_bridge(e)
        sys.stderr.write("Loading webinar attendee counts...\n")
        webinar_attendees = load_webinar_attendees_by_canonical(e)
    finally:
        w.close()
        e.close()

    sys.stderr.write("Building clusters...\n")
    clusters: list[Cluster] = []
    clusters.extend(build_wip_duplicate_clusters(wip_companies, dup_candidates))
    clusters.extend(build_canonical_fragmentation_clusters(canonical_by_id, wip_companies))
    clusters.extend(build_unmapped_wip_clusters(wip_companies))

    sys.stderr.write("Classifying + enriching %d clusters...\n" % len(clusters))
    for c in clusters:
        classify(c)
        enrich(c, wip_currencies, webinar_attendees, reject_counts)

    totals = {
        "wip_active": len(wip_companies),
        "wip_unmapped": sum(1 for w in wip_companies if not w["canonical_company_id"]),
        "wip_dup_candidates": len(dup_candidates),
        "bridge_canonicals": len({r["canonical_company_id"] for r in sf_bridge.values() if r["canonical_company_id"]}),
        "open_rejects": sum(reject_counts.values()),
    }

    sys.stderr.write("Writing CSVs + report...\n")
    write_full_csv(clusters, out_dir / "sf_canonical_alignment_candidates.csv")
    write_high_impact_csv(clusters, out_dir / "high_impact_duplicate_groups.csv", TOP_N_HIGH_IMPACT)
    write_report(clusters, out_dir / "sf_canonical_alignment_report.md", totals)

    # Console summary
    by_cat = defaultdict(int)
    for c in clusters:
        by_cat[c.category] += 1
    print("=== WO-13 summary ===")
    print("  clusters: %d" % len(clusters))
    for cat, n in sorted(by_cat.items(), key=lambda kv: -kv[1]):
        print("    %s: %d" % (cat, n))
    print("  artifacts in: %s" % out_dir)


if __name__ == "__main__":
    main()
