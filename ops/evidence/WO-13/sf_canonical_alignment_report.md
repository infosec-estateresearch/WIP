# WO-13 — Salesforce duplicate / canonical alignment review

Read-only review of where the Salesforce ↔ WIP ↔ ERic canonical identity space is fragmented, duplicated, or misaligned. **No mutations.** Operator review only.

Generated: 2026-05-28 11:56:13 UTC

## Headline numbers

| Signal | Count |
|---|---|
| Active SF Accounts in WIP | 7375 |
| Active SF Accounts with no canonical bridge | 486 |
| WIP-detected duplicate-group candidates (NEW/NEEDS_REVIEW) | 45 |
| ERic canonical_companies in the bridge | 6951 |
| Clusters surfaced by this report | 1532 |
| Open import-reject rows (NEW/NEEDS_REVIEW) | 11872 |

## Bucket totals

| Category | Clusters | Σ current WIP | Σ webinar attendees | Σ import rejects |
|---|---:|---:|---:|---:|
| NEEDS_SALESFORCE_MERGE | 27 | £0.00 | 25 | 31 |
| CANONICAL_FRAGMENTATION | 1007 | £266,301.55 | 5376 | 6985 |
| FALSE_POSITIVE_DUPLICATE | 17 | £0.00 | 31 | 59 |
| UNMAPPED_SF_ACCOUNT | 481 | £0.00 | 0 | 43 |

## Hugh James — seed case

WO-12 surfaced this pattern. ERic users asking about "Hugh James" hit ERic canonical_company_id **13030** ("Hugh James Solicitors"). The WIP application has five sibling Salesforce Accounts under per-city canonical IDs, none of which is 13030:

| wip_company | SF Account ID | SF Account name | canonical_id (WIP) | Current WIP |
|---:|---|---|---:|---:|
| 2512 | 0015800000E2xdbAAB | Hugh James (Cardiff)     | 20425 | £75.00 |
| 3193 | 0015800000HzXcoAAF | Hugh James (London)      | 23743 | £0.00  |
| 7222 | 001Tv00000PnXxQIAV | Hugh James (Plymouth)    | 21545 | £0.00  |
| 7223 | 001Tv00000Pne4NIAR | Hugh James (Southampton) | 21547 | £0.00  |
| 7224 | 001Tv00000Pnj8rIAB | Hugh James (Manchester)  | 21549 | £0.00  |

ERic also has additional Hugh James canonicals not in WIP at all:

- **13030** — `Hugh James Solicitors` (the one ERic resolves "Hugh James" to; 51 webinar attendees on record)
- **18724** — `Hugh James Solicirors` (typo variant)
- **19311** — `Hugh James (Potter Rees Dolan)` (sub-firm/branding variant)

Effect: `firm_summary` for "Hugh James" correctly reports `wip_status = not_mapped_to_wip`, because canonical 13030 is not in the WIP-side bridge — even though Hugh James (Cardiff) holds £75 of current WIP under canonical 20425. The new path is exposing the real shape: one commercial firm, eight ERic canonical identities, five Salesforce Accounts, fragmented bridge.

Required business decision: should Hugh James be represented as one parent firm, multiple branch accounts, or a parent-child hierarchy? Whatever the answer, it should be **made in Salesforce first**, then flow downstream via the hourly sync.

## Design question to resolve before WO-14

**Do we treat Salesforce branch/location Accounts as separate WIP companies, or do we need a parent-company grouping layer?**

Two options:

- **Option A (one-account-one-company):** keep the current model. Branch totals add up, but ERic users asking about the parent firm hit `not_mapped_to_wip`. Caveat surfaces it but the answer is fragmentary.
- **Option B (introduce `wip_company_group` or `salesforce_parent_account_id`):** add a parent-firm layer so ERic can roll up branch WIP under the canonical parent. Cleaner for user queries; requires either (i) a Salesforce parent_account_id column flowing through the sync, or (ii) an ERic-side curated parent→child canonical mapping.

Recommendation: do not design Option B until the operator review of this report tells us how many clusters are genuine branches vs SF duplicates vs false positives. The bucket totals above are the first input to that decision.

## Top 50 clusters by business impact

Ranked by `score = (£WIP / 1000) + 5·active_cases + 2·webinar_attendees + 1·open_rejects`.

| # | Cluster | Category | SF accts | Canonicals | Cities | £ WIP | Cases | Webinar | Rejects | Score |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | hugh james | CANONICAL_FRAGMENTATION | 5 | 8 | 6 | £75.00 | 1 | 53 | 640 | 751.1 |
| 2 | jones whyte | CANONICAL_FRAGMENTATION | 2 | 5 | 2 | £169,310.30 | 26 | 40 | 9 | 388.3 |
| 3 | knights | CANONICAL_FRAGMENTATION | 35 | 38 | 35 | £0.00 | 0 | 130 | 91 | 351.0 |
| 4 | wbw | CANONICAL_FRAGMENTATION | 14 | 19 | 15 | £0.00 | 0 | 56 | 227 | 339.0 |
| 5 | porter dodson | CANONICAL_FRAGMENTATION | 5 | 7 | 6 | £0.00 | 0 | 48 | 107 | 203.0 |
| 6 | taylor emmet | CANONICAL_FRAGMENTATION | 4 | 6 | 4 | £0.00 | 0 | 37 | 85 | 159.0 |
| 7 | pinney talfourd | CANONICAL_FRAGMENTATION | 5 | 7 | 5 | £0.00 | 0 | 24 | 108 | 156.0 |
| 8 | birketts | CANONICAL_FRAGMENTATION | 6 | 7 | 6 | £0.00 | 0 | 62 | 28 | 152.0 |
| 9 | wards | CANONICAL_FRAGMENTATION | 13 | 17 | 14 | £0.00 | 0 | 36 | 77 | 149.0 |
| 10 | wolferstans | CANONICAL_FRAGMENTATION | 2 | 3 | 2 | £0.00 | 0 | 37 | 67 | 141.0 |
| 11 | blackadders | CANONICAL_FRAGMENTATION | 6 | 7 | 6 | £0.00 | 0 | 20 | 99 | 139.0 |
| 12 | buckles | CANONICAL_FRAGMENTATION | 6 | 8 | 6 | £0.00 | 0 | 50 | 24 | 124.0 |
| 13 | irwin mitchell | CANONICAL_FRAGMENTATION | 13 | 15 | 14 | £0.00 | 0 | 59 | 6 | 124.0 |
| 14 | blanchards bailey | CANONICAL_FRAGMENTATION | 4 | 5 | 4 | £0.00 | 0 | 26 | 69 | 121.0 |
| 15 | tees | CANONICAL_FRAGMENTATION | 6 | 12 | 6 | £0.00 | 0 | 46 | 29 | 121.0 |
| 16 | ashtons | CANONICAL_FRAGMENTATION | 6 | 7 | 6 | £0.00 | 0 | 53 | 13 | 119.0 |
| 17 | palmers | CANONICAL_FRAGMENTATION | 6 | 7 | 4 | £35,655.00 | 5 | 25 | 0 | 110.7 |
| 18 | clarke willmott | CANONICAL_FRAGMENTATION | 3 | 4 | 3 | £0.00 | 0 | 23 | 51 | 97.0 |
| 19 | chattertons | CANONICAL_FRAGMENTATION | 8 | 9 | 8 | £0.00 | 0 | 32 | 32 | 96.0 |
| 20 | foys | CANONICAL_FRAGMENTATION | 8 | 9 | 8 | £0.00 | 0 | 7 | 80 | 94.0 |
| 21 | brethertons | CANONICAL_FRAGMENTATION | 4 | 5 | 4 | £0.00 | 0 | 21 | 51 | 93.0 |
| 22 | tollers | CANONICAL_FRAGMENTATION | 12 | 13 | 9 | £0.00 | 0 | 17 | 59 | 93.0 |
| 23 | weightmans | CANONICAL_FRAGMENTATION | 8 | 9 | 8 | £0.00 | 0 | 33 | 26 | 92.0 |
| 24 | nelsons | CANONICAL_FRAGMENTATION | 7 | 11 | 5 | £24,721.25 | 6 | 18 | 1 | 91.7 |
| 25 | sas daniels | CANONICAL_FRAGMENTATION | 4 | 5 | 4 | £0.00 | 0 | 10 | 70 | 90.0 |
| 26 | ellis jones | CANONICAL_FRAGMENTATION | 6 | 8 | 6 | £0.00 | 0 | 31 | 26 | 88.0 |
| 27 | star | CANONICAL_FRAGMENTATION | 19 | 22 | 20 | £0.00 | 0 | 19 | 49 | 87.0 |
| 28 | mills reeve | CANONICAL_FRAGMENTATION | 6 | 7 | 6 | £0.00 | 0 | 41 | 4 | 86.0 |
| 29 | talbots | CANONICAL_FRAGMENTATION | 13 | 13 | 13 | £0.00 | 0 | 12 | 62 | 86.0 |
| 30 | zedra | CANONICAL_FRAGMENTATION | 4 | 8 | 5 | £0.00 | 0 | 8 | 69 | 85.0 |
| 31 | russell russell | CANONICAL_FRAGMENTATION | 5 | 6 | 5 | £0.00 | 0 | 7 | 70 | 84.0 |
| 32 | thomson snell passmore | CANONICAL_FRAGMENTATION | 2 | 3 | 2 | £0.00 | 0 | 32 | 19 | 83.0 |
| 33 | hcb | CANONICAL_FRAGMENTATION | 21 | 26 | 23 | £0.00 | 0 | 20 | 41 | 81.0 |
| 34 | lindsays | CANONICAL_FRAGMENTATION | 6 | 8 | 5 | £0.00 | 0 | 33 | 12 | 78.0 |
| 35 | swayne johnson | CANONICAL_FRAGMENTATION | 6 | 8 | 6 | £0.00 | 0 | 28 | 22 | 78.0 |
| 36 | stephens scown | CANONICAL_FRAGMENTATION | 2 | 4 | 3 | £0.00 | 0 | 13 | 48 | 74.0 |
| 37 | banner jones | CANONICAL_FRAGMENTATION | 2 | 3 | 2 | £0.00 | 0 | 27 | 20 | 74.0 |
| 38 | penningtons manches cooper | CANONICAL_FRAGMENTATION | 5 | 6 | 5 | £0.00 | 0 | 34 | 5 | 73.0 |
| 39 | switalskis | CANONICAL_FRAGMENTATION | 12 | 13 | 12 | £0.00 | 0 | 19 | 35 | 73.0 |
| 40 | lodders | CANONICAL_FRAGMENTATION | 3 | 4 | 3 | £0.00 | 0 | 27 | 18 | 72.0 |
| 41 | everys | CANONICAL_FRAGMENTATION | 6 | 8 | 6 | £0.00 | 0 | 29 | 14 | 72.0 |
| 42 | ashfords | CANONICAL_FRAGMENTATION | 4 | 5 | 4 | £0.00 | 0 | 32 | 7 | 71.0 |
| 43 | shakespeare martineau | CANONICAL_FRAGMENTATION | 10 | 10 | 11 | £0.00 | 0 | 26 | 19 | 71.0 |
| 44 | hcr | CANONICAL_FRAGMENTATION | 11 | 15 | 11 | £0.00 | 0 | 32 | 7 | 71.0 |
| 45 | thrings | CANONICAL_FRAGMENTATION | 6 | 9 | 6 | £0.00 | 0 | 20 | 30 | 70.0 |
| 46 | private client | CANONICAL_FRAGMENTATION | 2 | 4 | 0 | £815.00 | 11 | 6 | 0 | 67.8 |
| 47 | whitehead monckton | CANONICAL_FRAGMENTATION | 3 | 4 | 3 | £0.00 | 0 | 24 | 19 | 67.0 |
| 48 | lawson west | CANONICAL_FRAGMENTATION | 2 | 3 | 2 | £0.00 | 0 | 14 | 39 | 67.0 |
| 49 | smith partnership | CANONICAL_FRAGMENTATION | 5 | 6 | 5 | £0.00 | 0 | 20 | 27 | 67.0 |
| 50 | wollens | CANONICAL_FRAGMENTATION | 4 | 5 | 4 | £0.00 | 0 | 18 | 30 | 66.0 |

## Files in this evidence package

- `sf_canonical_alignment_report.md` — this narrative report
- `sf_canonical_alignment_candidates.csv` — every cluster surfaced, full columns
- `high_impact_duplicate_groups.csv` — top 50 clusters by impact score
- `generate_alignment_report.py` — the read-only script that produced these

## What WO-13 does NOT do

- No automatic merges (Salesforce owns the merge — see [[feedback-salesforce-readonly]])
- No `canonical_company` updates in ERic
- No `entity_alias` writes
- No `wip_company` mutations
- No reject-queue bulk processing
- No ERic behaviour changes

