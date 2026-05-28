"""WIP-side firm-name normalisation.

Mirrors the logic of ERic's `src/resolver/normaliser.normalise_company` and
the suffix-strip in `scripts/generate_alias_review_pack._normalise`, but
re-implemented locally so WIP doesn't import from /home/ELT.

Used by:
- Salesforce Account sync — to compute `wip_company.account_name_normalised`
  and `wip_company.duplicate_group_key`.
- Duplicate detector — to group SF Account names into candidate clusters.

Examples:
    "Hugh James Solicitors"  → "hugh james"
    "Knights plc"            → "knights"
    "Knights Solicitors LLP" → "knights"
    "Charles Newton & Co"    → "charles newton"
"""

from __future__ import annotations

import re

_SUFFIX_PATTERN = re.compile(
    r"\b("
    r"solicitors?|sra|llp|llc|ltd|limited|plc|"
    r"co|company|and co|and company|"
    r"group|law|legal|the law firm|law firm|"
    r"partners?|practice|chambers|associates"
    r")\b",
    re.IGNORECASE,
)
_PUNCT_PATTERN = re.compile(r"[^\w\s]+")
_WS_PATTERN = re.compile(r"\s+")


def normalise_company_name(name: str | None) -> str:
    """Lowercase, strip punctuation, strip firm-type suffixes, collapse spaces.

    Order matters: punctuation FIRST so '& Co' becomes ' Co' and matches the
    word-bounded 'co' suffix; then suffix strip; then whitespace collapse.
    """
    if not name:
        return ""
    s = name.lower()
    s = _PUNCT_PATTERN.sub(" ", s)
    s = _SUFFIX_PATTERN.sub(" ", s)
    s = _WS_PATTERN.sub(" ", s).strip()
    return s


def duplicate_group_key(name: str | None) -> str:
    """The key used to detect Salesforce-Account duplicates.

    MVP: same as `normalise_company_name`. Can grow to incorporate postcode
    or industry later if name-alone proves too coarse — see WO-9B blueprint
    section 1a.
    """
    return normalise_company_name(name)
