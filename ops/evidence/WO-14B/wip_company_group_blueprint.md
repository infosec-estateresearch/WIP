# WO-14B — `wip_company_group` blueprint

Design-only. No DDL applied. No migrations. No data writes. Operator
review required before any WO-14B follow-up turns this into code.

Author context: WO-13 surfaced 1,007 canonical-fragmentation clusters
(vs. 27 true Salesforce duplicates and 17 false positives). That ratio —
roughly 22:1 in favour of fragmentation — is what makes this a structural
design decision rather than a cleanup task. See
`ops/evidence/WO-13/sf_canonical_alignment_report.md` for the inputs.

---

## 1. Decision: is `wip_company_group` required?

**Yes — recommended.**

The current one-Salesforce-Account-equals-one-WIP-company model is
technically clean but commercially fragmentary: a firm like Hugh James
appears in WIP as 5 separate companies (Cardiff/London/Plymouth/
Southampton/Manchester) under 5 separate per-city canonical IDs. The
ERic canonical the user reaches for ("Hugh James Solicitors", id 13030)
is not in the WIP bridge at all. WO-12's `firm_summary` correctly
reports `not_mapped_to_wip`, but the answer is fragmentary — the firm
*does* have WIP, just under sibling identities.

WO-13's headline numbers show this is not a one-off. The same pattern
holds for Knights (35 SF Accounts / 38 canonicals / 35 cities), WBW
(14/19/15), Wards (13/17/14), Tollers (12/13/9), and ~1000 others.
Adding a grouping layer is the only way ERic can answer "Hugh James"
without either (a) silently picking one branch or (b) refusing the
question on a technicality.

The grouping layer is **additive, not replacement**: Salesforce Account
remains the legal/CRM identity; `wip_company` remains the SF mirror;
`canonical_company` remains ERic's identity nucleus. `wip_company_group`
sits *above* `wip_company` and provides the business roll-up.

### What the grouping layer is NOT

- Not a Salesforce-side change (we have no write authority on SF — see
  the standing read-only rule).
- Not a `canonical_company` mutation. Operators do not "merge" canonicals
  inside ERic.
- Not automatic. The architect rule from the alias work holds:
  `detect → report → review → approve → apply`. No silent merges.
- Not a replacement for `wip_company_duplicate_candidate` — that table
  catches the 27 true SF duplicates (same name, same location) where
  the fix belongs in Salesforce. The group layer is for the 1,007
  fragmentation clusters where the fix is *not* a Salesforce merge.

---

## 2. Proposed table model

Two tables in `nif_wip`:

### `wip_company_group`

| Column | Type | Notes |
|---|---|---|
| `id` | BIGINT PK | Internal group identity — ERic and WIP code key off this, never the name |
| `group_name` | VARCHAR(255) NOT NULL | Human-facing display name (e.g. "Hugh James") |
| `group_name_normalised` | VARCHAR(255) NOT NULL | `normalise_company_name(group_name)`; UNIQUE within `review_status='APPROVED'` |
| `primary_salesforce_account_id` | VARCHAR(18) NULL | The "headquarters" Salesforce Account. Operator-set. Used when ERic needs to surface one canonical SF identity for the group |
| `primary_canonical_company_id` | BIGINT NULL | The ERic canonical the group's name maps to (e.g. 13030 "Hugh James Solicitors" for the Hugh James group) — drives ERic's firm-name → group resolution |
| `group_type` | ENUM | See controlled vocabulary below |
| `review_status` | ENUM | See controlled vocabulary below |
| `source_cluster_id` | VARCHAR(64) NULL | The WO-13 `cluster_id` that seeded this group, for audit trail |
| `proposed_by` | VARCHAR(255) NULL | `'wo-13-seed'` for system-proposed; operator handle for manual additions |
| `reviewed_by` | VARCHAR(255) NULL | Operator who APPROVED/REJECTED |
| `reviewed_at` | DATETIME(6) NULL |  |
| `resolution_note` | TEXT NULL | Free-text reason for status (especially for REJECTED) |
| `created_at` | DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) |  |
| `updated_at` | DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) |  |

Indexes:
- `UNIQUE (group_name_normalised, review_status)` partial / functional where supported, otherwise enforced at application level — prevents two APPROVED groups with the same normalised name
- `INDEX (primary_canonical_company_id)` — drives ERic lookup
- `INDEX (primary_salesforce_account_id)` — drives WIP lookup
- `INDEX (review_status, group_type)` — drives operator queue queries

### `wip_company_group_member`

| Column | Type | Notes |
|---|---|---|
| `id` | BIGINT PK |  |
| `group_id` | BIGINT NOT NULL | FK → `wip_company_group.id` ON DELETE RESTRICT (never lose history silently) |
| `wip_company_id` | BIGINT NOT NULL | FK → `wip_company.id` ON DELETE RESTRICT |
| `salesforce_account_id` | VARCHAR(18) NOT NULL | Denormalised copy from `wip_company` so member lists survive a (soft-)deleted wip_company |
| `canonical_company_id` | BIGINT NULL | Denormalised; may be NULL where the SF Account isn't yet bridged |
| `member_role` | ENUM | See controlled vocabulary below |
| `confidence_source` | ENUM | How the membership was proposed: `WO-13-CLUSTER`, `SF_PARENT_ACCOUNT`, `OPERATOR_MANUAL` |
| `review_status` | ENUM | Independent of the group's status — a member can be PROPOSED while the group is APPROVED (operator adding new branches) |
| `proposed_by` | VARCHAR(255) NULL |  |
| `reviewed_by` | VARCHAR(255) NULL |  |
| `reviewed_at` | DATETIME(6) NULL |  |
| `created_at` | DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) |  |
| `updated_at` | DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) |  |

Indexes / constraints:
- `UNIQUE (group_id, wip_company_id)` — a `wip_company` appears at most once per group
- `UNIQUE (wip_company_id)` filtered to `review_status='APPROVED'` — **a wip_company belongs to at most one APPROVED group across the whole table**. Enforced by partial unique index on databases that support it; otherwise by a small trigger/check or by application-layer guard. This is the critical structural rule.
- `UNIQUE (group_id, member_role)` filtered to `member_role='PRIMARY' AND review_status='APPROVED'` — at most one PRIMARY per APPROVED group
- `INDEX (canonical_company_id)` — drives ERic's group-by-canonical lookup

### Controlled vocabularies

`group_type` — what kind of grouping this is:

| Value | Meaning |
|---|---|
| `FIRM_GROUP` | One legal firm operating under one trading name across multiple offices (Hugh James). Default for branch/per-city fragmentation. |
| `BRAND_GROUP` | Multiple distinct legal entities operating under a shared brand or referral network |
| `BRANCH_GROUP` | Parent + branches with a formal Salesforce ParentId hierarchy (when we add `salesforce_parent_account_id` to wip_company) |
| `NETWORK_GROUP` | Affiliated firms (e.g. a panel) that should be grouped for some reports but not for billing/case attribution |
| `UNKNOWN` | Operator can't yet tell — placeholder during review |

`member_role` — how a member sits in the group:

| Value | Meaning |
|---|---|
| `PRIMARY` | The headquarters / parent SF Account. Exactly one per APPROVED group. |
| `BRANCH` | A geographic branch of the same firm |
| `OFFICE` | A non-trading office (registered address etc.) |
| `ALIAS` | The same firm represented under an alternate name (typo, rebrand, legacy) |
| `RELATED_ACCOUNT` | Linked but not part of the same legal firm (e.g. a related TPS provider). Visible in operator review but excluded from WIP roll-up by default. |
| `UNKNOWN` | Placeholder during review |

`review_status` — operator workflow state:

| Value | Meaning |
|---|---|
| `PROPOSED` | System-proposed (typically by the WO-13 seed). Not visible in any ERic roll-up. |
| `NEEDS_REVIEW` | Operator has touched it but not finalised |
| `APPROVED` | Operator has confirmed. Visible in ERic group-level answers. |
| `REJECTED` | Operator confirmed this is NOT a group (false positive). Stays in the table for audit so the seed doesn't re-propose it next run. |

---

## 3. Relationship to Salesforce Account and `canonical_company`

```
                Salesforce Account            (legal/CRM identity — read-only)
                        │
                        ▼  hourly sync
                 wip_company                  (one row per active SF Account)
                        │
                        │  membership (0..n)
                        ▼
   ┌──────  wip_company_group_member  ──────┐
   │                                          │
   │  group_id ─────► wip_company_group       │
   │                                          │
   └──── canonical_company_id (denorm) ───────┘
                        │
                        │  primary_canonical_company_id
                        ▼
                canonical_company              (ERic's identity nucleus)
```

Key invariants:

- **Salesforce Account is unchanged.** `wip_company_group` is a downstream
  WIP-side concept. Salesforce neither knows nor cares about it.
- **`canonical_company` is unchanged.** ERic does not gain a new
  "parent canonical" notion. Instead, ERic asks: "for canonical X, which
  APPROVED group, if any, contains a member with `canonical_company_id = X`?"
- **A group can span multiple canonicals.** Hugh James → 8 canonicals
  (13030, 18724, 19311, 20425, 21545, 21547, 21549, 23743). All eight
  are valid `canonical_company_id` values on individual member rows; the
  group's `primary_canonical_company_id` is just the one ERic prefers
  for "what does this group resolve to" (13030 = "Hugh James Solicitors").
- **A canonical belongs to at most one APPROVED group.** Enforced by the
  partial unique index on `(canonical_company_id)` where
  `review_status='APPROVED'`. This is the rule that lets ERic answer
  "show me X" deterministically: there is never ambiguity about which
  group a canonical rolls up to.
- **Salesforce `Account.ParentId`** is *not* the source of truth for
  groups — it is a seed signal. If we add `salesforce_parent_account_id`
  to `wip_company` (recommended as a co-requisite, see §6), the WO-13
  seed pass can propose `BRANCH_GROUP` candidates pre-clustered by
  Salesforce's own hierarchy. Operator still has to APPROVE.

---

## 4. Operator approval workflow

Mirrors the alias-review pattern that's already been validated in WO-7..8:
`detect → report → review → approve → apply`. No automatic state changes.

### 4.1 Detect (system, repeatable)

A `propose_groups.py` script runs against the WO-13 cluster set and
emits one PROPOSED group + members per cluster that:

- has `category = CANONICAL_FRAGMENTATION`
- has `impact_score ≥ THRESHOLD` (initial cut: ≥ 20 — covers the top
  ~200 clusters out of 1,007; can be lowered as operator capacity grows)
- has no APPROVED group already containing any of its `wip_company_id`s
  (idempotency — re-running the seed does not create duplicates)

The script writes PROPOSED rows under `proposed_by = 'wo-13-seed'`.
It does NOT touch APPROVED or REJECTED rows.

### 4.2 Report (operator-facing artifact)

A daily refresh emits `ops/evidence/WO-14A/groups_for_review.csv`
(one row per PROPOSED or NEEDS_REVIEW group) with columns:

```
group_id, group_name, group_type, member_count,
member_wip_ids, member_sf_ids, member_account_names,
canonical_ids, distinct_cities, total_current_wip,
active_case_count, webinar_attendees, import_reject_count,
impact_score, suggested_primary_sf_id, suggested_primary_canonical_id,
operator_decision, operator_note, set_primary_sf_id
```

The right-three columns are blank — operators fill them in.
`operator_decision` accepts: `APPROVE`, `REJECT`, `NEEDS_REVIEW`,
`SPLIT`, `MERGE_INTO=<group_id>`.

### 4.3 Review (operator)

Operator opens the CSV in Excel or the eventual WIP review GUI, marks
decisions, saves. Reject reasons go in `operator_note`.

### 4.4 Apply (script, idempotent)

`apply_group_decisions.py` reads the marked CSV and:

- `APPROVE` → set group `review_status=APPROVED`, set
  `primary_salesforce_account_id` from `set_primary_sf_id` (or the
  suggested one if blank), set all member `review_status=APPROVED`
- `REJECT` → set group `review_status=REJECTED`. Members stay attached
  for audit but become invisible to ERic.
- `NEEDS_REVIEW` → flag for follow-up; no member-status change
- `SPLIT` → operator-provided split rule; create N PROPOSED groups
  from the members, leave the original in REJECTED status with a note
- `MERGE_INTO=<existing_group_id>` → re-parent members to the target
  group, REJECT the source

All writes inside a single transaction per operator decision so a CSV
parse failure halfway through doesn't leave half-applied groups.

### 4.5 Audit & re-propose

A re-run of `propose_groups.py` after an operator pass:

- Skips clusters where every member is now in an APPROVED group ✓
- Skips clusters where every member is in a REJECTED group ✓
- Proposes a fresh PROPOSED group when *new* `wip_company` rows enter
  a previously-reviewed cluster (e.g. Salesforce adds a sixth Hugh James
  branch — the seed proposes adding it to the existing APPROVED group
  rather than creating a new one, by matching on `group_name_normalised`)

---

## 5. ERic reporting behaviour: account-level vs group-level

ERic gains one new resolution step and three answer modes.

### 5.1 New resolution step in firm lookup

```
firm_name typed by user
  │
  ▼
resolve_canonical_company_id()    (existing — WO-12)
  │
  ▼
canonical_id
  │
  ▼
NEW: resolve_to_group()
  │
  ├── canonical is PRIMARY/BRANCH/OFFICE/ALIAS member of an APPROVED group
  │     → group_id known. Default answer at group level.
  │
  └── canonical is not in any APPROVED group
        → no group. Answer at account level (current WO-12 behaviour).
```

### 5.2 Three answer modes for `firm_summary`

| Mode | When | Output |
|---|---|---|
| `account_only` | canonical is not in a group | unchanged WO-12 output (one wip block, one set of WIP fields) |
| `group_default` | canonical is in an APPROVED group; user said "Hugh James" (i.e. didn't pin a branch) | group_name as header; total_current_wip = SUM across APPROVED PRIMARY/BRANCH/OFFICE/ALIAS members; member-level breakdown table underneath |
| `account_pinned` | user explicitly named a branch ("Hugh James Cardiff") | account-only output PLUS a one-line note "This is part of the Hugh James group (5 SF Accounts). Want the group view?" — operator can request the roll-up |

The default for an unqualified firm name is `group_default`. This
matches the architect's example: *"Hugh James appears to be represented
by multiple Salesforce branch/location accounts. I can show the
group-level view or the individual Salesforce account view."*

### 5.3 Three answer modes for `top_firms(metric=total_wip)`

| Mode | Selection | Output |
|---|---|---|
| `groups_first` (DEFAULT) | for each group: SUM(member.current_wip); for each wip_company NOT in any APPROVED group: its own current_wip. Combined, ranked. | One row per group or singleton. `data_source: wip_sql_group_view` |
| `accounts_only` | every wip_company stands alone (current WO-12 behaviour) | `data_source: wip_sql` |
| `accounts_within_group` | drill-down for a chosen group | per-member rows under one group header |

`groups_first` is recommended as the new default because (a) it matches
what an operator means when they say "top firms", and (b) it removes the
fragmentation noise that currently splits a firm's WIP across rows.

### 5.4 `webinar_firms_without_wip` change

When testing whether a firm has WIP, lift the test to group level: a
firm's webinar attendees are excluded from the "not in WIP" list if ANY
member of its group has current WIP. This means Hugh James (currently
on the list at #6 with 51 attendees) drops off — Cardiff's £75 of WIP
counts for the group.

### 5.5 Coverage caveat changes

The existing WO-12 coverage caveat continues unchanged. Add one
group-specific addendum surfaced when an answer is in `group_default`
mode and the group has any members in PROPOSED or NEEDS_REVIEW state:

> *Note: this group's membership is currently under operator review.
> N member account(s) are pending approval and are not included in the
> rolled-up figures above.*

### 5.6 Concerns / open questions

- **What does a "group rename" look like in ERic?** Group display
  changes when operators set a new `group_name`. ERic always reads
  `group_name` at query time, so changes propagate immediately.
- **What about cross-group reporting (e.g. "top firms by group + branch")?**
  Not needed for v1. Operator can request the drill-down explicitly.
- **What happens when SF deletes a `wip_company` that is a PRIMARY?**
  The soft-delete flag (`is_active_in_salesforce=0`) demotes it. The
  group's member row stays for audit. ERic group total skips inactive
  members. Operator must promote another member to PRIMARY (UI or CSV).

---

## 6. Migration path from WO-13 clusters into proposed groups

### 6.1 Phasing

| Phase | Authority needed | Output |
|---|---|---|
| P0 — table model (this WO) | review-only | this blueprint |
| P1 — schema migration | separate WO-14B-MIGRATE auth | DDL for `wip_company_group` + `wip_company_group_member`; grants for `wip_app` (DML) and `wip_read` (SELECT) |
| P2 — seed proposal script | bundled with P1 | `scripts/propose_groups.py` reads WO-13 candidates CSV and INSERTs PROPOSED rows. Idempotent. Re-run on every WIP sync run if desired. |
| P3 — operator workflow scripts | WO-14A | `scripts/export_groups_for_review.py` + `scripts/apply_group_decisions.py`. Both wrap the same DML logic so the GUI (when built) can reuse. |
| P4 — ERic read-model | separate WO under ERic | new views `v_eric_wip_group_summary`, `v_eric_wip_group_with_wip`; ERic `wip.py` learns `resolve_to_group()`; firm_summary / top_firms switch to `group_default` |
| P5 — operator review of top-200 clusters | operator capacity, not code | APPROVED Hugh James, Knights, etc. Visible in ERic. |
| P6 — co-requisite: Salesforce ParentId | one-time add of `wip_company.salesforce_parent_account_id` + SF sync change | enables `BRANCH_GROUP` seeding from SF's own hierarchy |

### 6.2 Seed translation from WO-13 candidates

For each row in `sf_canonical_alignment_candidates.csv` where
`category = CANONICAL_FRAGMENTATION` and `impact_score >= 20`:

```
wip_company_group:
  group_name              = WO-13 group_label (Title-cased)
  group_name_normalised   = WO-13 group_label
  primary_salesforce_account_id = NULL          (operator picks)
  primary_canonical_company_id  = pick the canonical_company_id
        that has the lowest id AND a non-fragmented name (i.e. no
        parentheses, no city suffix). Falls back to lowest id overall.
  group_type              = FIRM_GROUP                 (default)
  review_status           = PROPOSED
  source_cluster_id       = WO-13 cluster_id
  proposed_by             = 'wo-13-seed'

wip_company_group_member (one per WIP id in the cluster):
  group_id                = the just-created group
  wip_company_id          = from cluster
  salesforce_account_id   = denormalised
  canonical_company_id    = denormalised
  member_role             = BRANCH               (default for non-PRIMARY)
  confidence_source       = WO-13-CLUSTER
  review_status           = PROPOSED
```

### 6.3 Operator decisions on Hugh James (worked example)

What the operator's CSV row would look like after a review pass:

| Field | Value |
|---|---|
| group_id | 1 |
| group_name | Hugh James |
| group_type | FIRM_GROUP |
| member_count | 5 |
| member_sf_ids | 0015800000E2xdbAAB, 0015800000HzXcoAAF, 001Tv00000PnXxQIAV, 001Tv00000Pne4NIAR, 001Tv00000Pnj8rIAB |
| member_account_names | Hugh James (Cardiff), Hugh James (London), Hugh James (Plymouth), Hugh James (Southampton), Hugh James (Manchester) |
| canonical_ids | 13030, 18724, 19311, 20425, 21545, 21547, 21549, 23743 |
| distinct_cities | Cardiff, London, Manchester, Plymouth, Southampton |
| total_current_wip | £75 |
| webinar_attendees | 53 |
| import_reject_count | 640 |
| impact_score | 751.1 |
| suggested_primary_sf_id | 0015800000E2xdbAAB (Cardiff — only account with current WIP) |
| suggested_primary_canonical_id | 13030 (the canonical ERic resolves "Hugh James" to) |
| **operator_decision** | **APPROVE** |
| **set_primary_sf_id** | 0015800000HzXcoAAF (London — operator chooses London as the HQ) |
| **operator_note** | "Single FIRM_GROUP; PRIMARY = London office per commercial team confirmation 2026-05-28. Need to check whether 18724 (Solicirors typo) and 19311 (Potter Rees Dolan) should stay attached as ALIAS or REJECTED separately." |

Post-apply state:
- 1 APPROVED group named "Hugh James"
- 5 APPROVED member rows; London promoted to PRIMARY, others BRANCH
- ERic firm_summary for "Hugh James" returns group_default mode:
  total current WIP £75 across 5 SF Accounts, with per-branch table

The remaining canonical IDs (18724 typo, 19311 Potter Rees Dolan) sit
in `NEEDS_REVIEW` until the operator does a second pass — either folding
them in as ALIAS or rejecting them out.

---

## 7. Risks and rules for not merging false positives

The single biggest failure mode is **over-grouping**: two legitimately
separate firms with similar names get rolled into one group, and the
ERic answer becomes wrong instead of fragmentary.

Rules:

1. **No automatic APPROVAL.** Even the cleanest WO-13 cluster requires
   operator sign-off. PROPOSED groups are invisible to ERic.
2. **`canonical_company_id` uniqueness across APPROVED groups** (partial
   unique index): a canonical belongs to *at most one* APPROVED group.
   The schema rejects an operator mistake that would attach the same
   canonical to two groups.
3. **Different `account_type` flag.** When the seed builds a PROPOSED
   group, if the cluster's `distinct_account_types > 1` (e.g. one
   member is "Tier 1 Solicitor" and another is "Council"), mark the
   group `group_type = UNKNOWN` and add a flag column
   `seed_warning = "mixed_account_types"`. The operator review CSV
   surfaces that warning prominently.
4. **City-divergence rule.** A cluster with members across ≥ 3 distinct
   cities is allowed as a candidate (Hugh James shape), but ≥ 6 cities
   should be split-by-default — operator must explicitly opt back into
   a single group. This catches the "Smith Law" pattern where many
   unrelated regional firms share a generic name.
5. **REJECTED is sticky.** A REJECTED group's `group_name_normalised`
   is added to a `rejected_group_seeds` table; the seed proposer skips
   any cluster whose key is in that set. Operator can un-reject manually
   if needed.
6. **No mass-apply CLI flag.** `apply_group_decisions.py` requires the
   marked CSV; there is no `--approve-all` shortcut. Approval is
   per-row.
7. **Audit on every state change.** Each `review_status` transition
   writes to `wip_audit_log` (existing WO-10A table) with the operator
   handle, the prior state, and the resolution note.
8. **REJECTED ≠ deleted.** A REJECTED group's rows remain queryable for
   investigation; only the unique-index guards (APPROVED-only) ignore
   them.

---

## 8. Recommended next WO

**WO-14A — operator review workflow.**

The blueprint above is sufficient to start implementation. The
sequencing for WO-14A:

1. **WO-14A.1 — schema:** apply the migrations for `wip_company_group`
   and `wip_company_group_member`; add the partial unique indexes; add
   `rejected_group_seeds` for the sticky-reject rule.
2. **WO-14A.2 — seed:** ship `propose_groups.py`. Run it once. Operator
   gets the first `groups_for_review.csv` with ~200 PROPOSED groups
   (those with WO-13 impact_score ≥ 20).
3. **WO-14A.3 — apply:** ship `apply_group_decisions.py`. Operator
   marks decisions, script applies. Iterate until top-50 are APPROVED.
4. **WO-14A.4 — ERic read-model:** add `v_eric_wip_group_summary` and
   `v_eric_wip_group_with_wip` views, grant `wip_read` SELECT, teach
   ERic `wip.py` to use them. Update `firm_summary`, `top_firms`, and
   `webinar_firms_without_wip` to default to `group_default` mode.

**Not WO-14C (reject queue triage) yet.** The 6,985 open rejects in the
fragmentation clusters are largely symptoms of the missing group layer —
once Hugh James is an APPROVED group, `import_mastercopy_wip` can match
its 640 rejected rows on group_name_normalised, not just account name.
Triage cost drops by an order of magnitude after WO-14A.

**Not WO-14D (caveat refinement) yet.** The caveats need to change *with*
the answer-mode change in §5.5. Bundle that change into WO-14A.4 rather
than doing it as a separate WO.

---

## Out of scope for this blueprint

- DDL is not provided. WO-14A will translate §2 into migrations.
- ERic-side code changes are not provided. WO-14A will translate §5 into
  service-layer edits.
- A GUI is not designed. The CSV roundtrip in §4 is the minimum viable
  workflow; a GUI is a later WO.
- Salesforce parent_account_id sync is named as a co-requisite (§6 P6)
  but not specified in detail — that depends on whether the SF
  integration user has `Account.ParentId` read access; needs a separate
  spike.

---

## Closure check against WO-14B conditions

| Condition | Where addressed |
|---|---|
| 1. Clear decision on whether `wip_company_group` is required | §1 — yes, recommended |
| 2. Proposed table model | §2 |
| 3. Relationship to Salesforce Account and `canonical_company` | §3 |
| 4. Operator approval workflow | §4 |
| 5. ERic reporting behaviour: account-level vs group-level | §5 |
| 6. Migration path from WO-13 clusters into proposed groups | §6 |
| 7. Risks and rules for not merging false positives | §7 |
| 8. Recommended next WO | §8 — WO-14A (operator workflow on top of the table model) |
