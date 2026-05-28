# WO-14A.2 — Operator runbook: company-group review

How to work through the WIP company-group review queue safely. This is
operator-facing; if you're not the operator triaging groups, you don't
need to read this.

## What's in the queue

The `nif_wip` database has a `wip_company_group` table seeded from the
WO-13 canonical-fragmentation report. Each row represents a candidate
group — a set of Salesforce Accounts that probably belong to one business
firm but are currently fragmented across multiple SF Accounts and
multiple ERic canonical IDs.

As of the WO-14A.1 seed run (2026-05-28):

| review_status   | Count | Visible to ERic? |
|---|---:|---|
| `PROPOSED`      | 277 | no — invisible until APPROVED |
| `NEEDS_REVIEW`  | 1   | no |
| `APPROVED`      | 1   | yes (via `v_eric_wip_company_group_*` views) |
| `REJECTED`      | 1   | no, and the name is sticky-rejected so a re-seed won't propose it again |

PROPOSED + NEEDS_REVIEW are your queue. APPROVED and REJECTED are done.

## The three decisions you can make

The MVP supports exactly three values in the `operator_decision` column
of the review CSV. The architect deferred `SPLIT_GROUP` and `MERGE_INTO`
until the blueprint is extended — don't use them; the apply script will
report them as unknown and skip.

| `operator_decision` | When to use | What it does |
|---|---|---|
| `APPROVE_GROUP` | The cluster is one real firm (with branches / aliases / minor name variants). You're ready for ERic to roll up WIP across these accounts. | Sets the group + all its members to `APPROVED`. Promotes the SF Account you named in `set_primary_sf_id` to `PRIMARY`; the others become `BRANCH`. Visible to ERic. |
| `REJECT_GROUP` | The cluster looks like a duplicate but isn't (false positive — different firms that share a name root). | Sets the group + members to `REJECTED`. Adds the normalised name to `wip_company_group_seed_reject` so a future seed run won't re-propose it. |
| `NEEDS_MORE_REVIEW` | You can't tell yet (need commercial team input, missing context). | Flips the group to `NEEDS_REVIEW`. Comes back to the queue. Doesn't touch members. |

If you leave `operator_decision` blank for a row, the apply script ignores
it. That's how you advance the queue in batches without committing to
every decision at once.

## Three columns to fill in

- **`operator_decision`** — one of the three values above
- **`set_primary_sf_id`** — required for `APPROVE_GROUP` when the group
  has more than one member. Must be one of the `member_sf_ids` listed
  in the same row. Picks the SF Account that represents the headquarters
  / parent practice.
- **`operator_note`** — free text. Captures your reasoning. Lives on
  the group row's `resolution_note` (and the sticky-reject row's
  `reason` for REJECTs). Future re-runs of the report can show this so
  you don't have to re-explain.

Everything else in the CSV is read-only context for your decision.

## How to pick the PRIMARY

For `APPROVE_GROUP`, you need to nominate one SF Account as PRIMARY.
The schema enforces "exactly one PRIMARY per APPROVED group" — there is
no way to have two.

In order of preference, pick:

1. **The SF Account flagged as the firm's HQ in Salesforce.** When we
   add `salesforce_parent_account_id` to the sync (WO-14B §6 P6), the
   SF Account that other branches point to via `ParentId` is the
   natural HQ.
2. **The SF Account with the most current WIP.** Look at the
   `group_current_wip` column — if one member's name corresponds to
   the largest case load, that's a defensible HQ.
3. **The SF Account whose name has no parenthetical / branch suffix.**
   "Hugh James" beats "Hugh James (London)" because the un-suffixed name
   is more likely the parent record in the firm's CRM.
4. **The SF Account in the most populous city.** Tie-breaker only.

The `suggested_primary_sf_id` column already proposes one based on #2.
You can use it as-is or override it. If you override, paste the SF
Account ID into `set_primary_sf_id`.

## When to APPROVE vs REJECT vs NEEDS_MORE_REVIEW

### APPROVE_GROUP
Use when **all** of these are true:
- The `member_account_names` clearly refer to the same firm
  (e.g. "Hugh James (Cardiff)", "Hugh James (London)", …)
- `distinct_account_types` is one value, or the variants are
  commercially consistent (e.g. all "Tier 1 Solicitor")
- The `seed_warning` column is empty, OR it's `wide_geographic_split`
  AND you can confirm the firm genuinely operates across that many
  cities (Hugh James does)
- You can identify which SF Account should be PRIMARY

### REJECT_GROUP
Use when **any** of these is true:
- The `member_account_names` look similar but refer to genuinely
  different firms (e.g. two unrelated "Smith Law" practices in different
  regions)
- `distinct_account_types` contains a mix that doesn't make commercial
  sense (e.g. one member is a solicitor, another is a council — the
  `mixed_account_types` warning will be set)
- You've checked Salesforce and can confirm no business relationship
  between the accounts

### NEEDS_MORE_REVIEW
Use when **any** of these is true:
- You need to ask the commercial team or check Salesforce manually
- The cluster looks plausible but you can't yet identify PRIMARY
- The `seed_warning` is `wide_geographic_split` (≥6 cities) and you're
  not sure whether it's one firm or several
- Member SF Account names overlap with a separate cluster that's also
  pending review — defer until you've done both

## Seed warnings to attend to

The `seed_warning` column flags clusters where automatic proposal is
less safe. Two warnings exist:

| `seed_warning` | Meaning | Default operator stance |
|---|---|---|
| `mixed_account_types` | Members include >1 distinct `account_type` (e.g. Tier 1 + Council). Seed defaulted `group_type` to `UNKNOWN`. | Lean toward `REJECT_GROUP` unless you can confirm the accounts are commercially related. |
| `wide_geographic_split` | Members span ≥6 distinct cities. Per WO-14B §7 rule 4 this is split-by-default territory. | Lean toward `NEEDS_MORE_REVIEW`. Only `APPROVE_GROUP` if you can confirm the firm operates across that many cities (e.g. national chains like Knights). |

## Worked examples

These three were applied during the WO-14A.1 smoke test. They are real
rows in the live `nif_wip` and exercise each decision path.

### Hugh James — APPROVE_GROUP

Cluster: `CC-FRAG-hugh-james`, 5 SF Accounts across 5 cities (Cardiff,
London, Plymouth, Southampton, Manchester), `seed_warning =
wide_geographic_split`, current WIP £75 (Cardiff).

Operator reasoning:
- All five member names are explicit branches of one firm
- Hugh James is a known multi-office UK practice — the wide geographic
  split is real
- London chosen as PRIMARY (commercial team confirmation)

Marked row:
```
operator_decision   = APPROVE_GROUP
set_primary_sf_id   = 0015800000HzXcoAAF
operator_note       = primary=London office per commercial team confirmation
```

Result after apply:
- `wip_company_group.id=68` → `APPROVED`, `primary_salesforce_account_id=0015800000HzXcoAAF`
- London member promoted to `PRIMARY`, Cardiff/Plymouth/Southampton/Manchester → `BRANCH`
- `v_eric_wip_company_group_summary` now returns Hugh James with
  `group_current_wip_value=£75` across 5 members

### Knights — NEEDS_MORE_REVIEW

Cluster: `CC-FRAG-knights`, 35 SF Accounts (in the WO-13 source data),
35 distinct cities, `seed_warning = wide_geographic_split`.

Operator reasoning:
- Knights is genuinely a national chain (35 cities is plausible)
- But the live PROPOSED group only has 7 members (see "Known limitation"
  below) — wait for WO-14A.3 to fold in the missing branches before
  APPROVING

Marked row:
```
operator_decision = NEEDS_MORE_REVIEW
operator_note     = defer until membership is complete
```

### Jones Whyte — REJECT_GROUP

Cluster: `CC-FRAG-jones-whyte`, member names suggest a single firm but
inspection (during the smoke test) treated this as a false-positive
example. In real review, the operator would only REJECT if SF inspection
confirmed no business relationship.

Marked row:
```
operator_decision = REJECT_GROUP
operator_note     = inspection found two unrelated practices sharing a name root
```

Result:
- Group → `REJECTED`
- `wip_company_group_seed_reject` row added for `jones whyte`
- Future seed runs skip this name (verified live: re-seed showed `1
  sticky_rejected`)

## Apply runbook

The full workflow:

### Step 1 — Export the review pack

If you don't already have a fresh CSV:

```bash
cd /home/WIP
python -m scripts.export_groups_for_review \
       --output /tmp/wip_groups_for_review.csv \
       --top 50 \
       --include-needs-review
```

`--top 50` keeps the highest-impact 50 groups (ranked by impact_score
from WO-13). Drop `--top` to get the full PROPOSED queue.

### Step 2 — Mark decisions

Open the CSV in Excel or a text editor. For each row, fill in the three
operator columns (decision, set_primary_sf_id, note). Leave rows blank
to defer them.

### Step 3 — Dry-run

Before committing, preview what will happen:

```bash
python -m scripts.apply_company_group_decisions \
       --input /tmp/wip_groups_for_review.csv \
       --dry-run
```

The output reports `rows seen / groups APPROVED / REJECTED /
NEEDS_REVIEW / skipped / failed`. Any `unknown decisions` or `errors`
listed must be fixed in the CSV before applying.

### Step 4 — Apply

```bash
python -m scripts.apply_company_group_decisions \
       --input /tmp/wip_groups_for_review.csv \
       --apply
```

Each row is one transaction; a failure on row 5 does **not** roll back
rows 1-4. The script exits non-zero if any row failed so automation
notices.

### Step 5 — Verify

```bash
python3 -c "
import pymysql, os
c = pymysql.connect(host='localhost', port=3336, user='wip_read',
                    password=os.environ['NIF_WIP_DB_PASS'],
                    database='nif_wip',
                    cursorclass=pymysql.cursors.DictCursor).cursor()
c.execute('SELECT group_name, member_count, primary_member_count, '
          'group_current_wip_value FROM v_eric_wip_company_group_summary '
          'ORDER BY group_current_wip_value DESC')
for r in c.fetchall(): print(r)
"
```

Every APPROVED group should appear, with `primary_member_count=1` and
the rolled-up `group_current_wip_value`.

### Step 6 — Re-run as needed

The apply script is **idempotent**:
- Re-applying a CSV where rows already match their target state is a
  no-op (counts as `rows skipped`).
- The seed proposer (`generate_company_group_review_pack`) also skips:
  - clusters whose members are already in an APPROVED group
  - clusters whose normalised name is in `wip_company_group_seed_reject`
  - clusters whose normalised name has an open PROPOSED/NEEDS_REVIEW

So you can re-run safely after operator iterations.

## Known limitation — under-membered PROPOSED groups

The WO-14A.1 seed proposer parses Salesforce IDs out of the WO-13
candidates CSV. The original WO-13 CSV truncated long ID lists at 8
entries (with a `... (+N more)` suffix), so groups for high-fragmentation
firms ended up with fewer members than the underlying cluster.

Example: **Knights** has 35 SF Accounts in the WO-13 source data but
only 7 in the live `wip_company_group_member` table.

Status:
- The truncation has been fixed in WO-13's generator (
  `ops/evidence/WO-13/generate_alignment_report.py`, `fmt_id_list`).
  Re-running the WO-13 script writes the full ID lists.
- The 280 already-PROPOSED groups in `nif_wip` are unchanged — they
  retain the smaller membership from the original seed.
- A follow-up WO (call it **WO-14A.3 — fold-in pass for existing
  PROPOSED groups**) will compare each PROPOSED group's current
  membership to the regenerated WO-13 cluster and INSERT missing
  members as `PROPOSED` rows under the same group.

Operator implications:
- For groups whose `member_count` looks low vs the number of SF Accounts
  you know the firm has (e.g. Knights), prefer `NEEDS_MORE_REVIEW`
  until WO-14A.3 has run. APPROVING under-membered groups won't break
  anything (the schema accepts it), but the ERic group-level roll-up
  will under-report until the missing branches are added later.
- Groups with `member_count ≤ 8` (which is most of the queue) are
  unaffected — they fit within the original truncation limit.

## What WO-14A.2 does NOT do

- No ERic cutover. ERic still answers at the per-Salesforce-Account
  level (WO-12 behaviour). The group-level read-model is in place
  (`v_eric_wip_company_group_*` views) but ERic's `firm_summary` /
  `top_firms` / `webinar_firms_without_wip` do not yet consume them.
  That's WO-14A.4 (queued; depends on enough APPROVED groups to make
  the cutover meaningful — current count: 1).
- No automatic group merges. Every APPROVED group required an operator
  decision row in the apply CSV.
- No GUI. CSV roundtrip is MVP. A GUI is a later WO.
- No `SPLIT_GROUP` / `MERGE_INTO` operator decisions yet. Both require
  blueprint extension first.

## Files in this evidence package

- `operator_runbook.md` — this document
- `groups_for_review_top50.csv` — top 50 PROPOSED + NEEDS_REVIEW groups
  ranked by impact_score (1 of the 50 is the existing NEEDS_REVIEW
  Knights row from the WO-14A.1 smoke test; the other 49 are PROPOSED)

## Related

- WO-13 — Salesforce duplicate / canonical alignment review
  (`ops/evidence/WO-13/sf_canonical_alignment_report.md`)
- WO-14B — `wip_company_group` blueprint
  (`ops/evidence/WO-14B/wip_company_group_blueprint.md`)
- WO-14A.1 — schema + seed + apply scripts (this repo's main branch)
- WO-14A.3 — fold-in pass for under-membered PROPOSED groups (queued)
- WO-14A.4 — ERic group_default cutover (queued; depends on operator
  approving a meaningful first batch)
