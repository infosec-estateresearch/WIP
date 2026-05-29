# WO-14A.6 — Estate Research demo walkthrough

A short script for showing the WIP operator GUI to the client. Use the
running pages, not slides. Stay in the GUI. Keep the technical layer
invisible.

**Audience:** Estate Research operational lead (and anyone they bring).
**Length:** 15–20 minutes including questions.
**URL the demoer opens:** `http://192.168.55.10:8300/`
**No login** (internal LAN/VPN only — flag this if asked).

---

## The one sentence to anchor everything

> *We are not guessing or merging firms automatically. The system is
> surfacing review work safely so Salesforce and WIP stay aligned.*

Say it once on the dashboard. Say it again on the duplicates page. Say
it a third time if the client starts asking about automatic decisions.

---

## Pre-flight — 5 minutes before the call

Run these locally and check the output. If any of them is unexpected,
**delay the demo** and investigate.

```bash
# 1. Service reachable
curl -sS http://192.168.55.10:8300/healthz                # expect: ok

# 2. Live queue is populated (not empty, not all approved)
python3 - <<'PY'
import pymysql
c = pymysql.connect(host="localhost", port=3336, user="wip_app",
                    password="QuQZJyCSLK3Fys62sCX1gttH",
                    database="nif_wip", cursorclass=pymysql.cursors.DictCursor).cursor()
for q, label in [
    ("SELECT review_status, COUNT(*) n FROM wip_company_group GROUP BY review_status", "groups"),
    ("SELECT COUNT(*) n FROM wip_company_duplicate_candidate WHERE status IN ('NEW','NEEDS_REVIEW')", "open duplicates"),
    ("SELECT COUNT(*) n FROM wip_import_reject WHERE resolution_status IN ('NEW','NEEDS_REVIEW')", "open rejects"),
]:
    c.execute(q); print(label, list(c.fetchall()))
PY

# 3. Hugh James (the confirmed example) still exists
curl -sS http://192.168.55.10:8300/data-review/groups/68 | grep -q "Confirmed" \
    && echo "Hugh James confirmed example: OK" \
    || echo "Hugh James example: MISSING — pick another confirmed group"
```

Expected state for the demo:
- groups: ~277 Pending review · 1 Needs review · 1 Confirmed · 1 Rejected
- open duplicates: ~45
- open rejects: ~25,000 (the import-coverage story)
- Hugh James confirmed example: OK

---

## The five-step walkthrough

Each step has: page, what to point at, what to say, what to *not* say.

### Step 1 — Dashboard

**Open:** `http://192.168.55.10:8300/`

**Point at:** the one-line intro ("Today's review work: about N items
waiting"), then the three card rows in order — *Data the system holds*,
*Things to review*, *Latest activity*.

**Say:**
> *"This is where Estate Research's working data lives. Three things to
> notice: top row is what the system holds, middle row is what needs
> your team's eyes, bottom row is the health of the automatic feeds.
> The middle row is where the work is."*

Then anchor the message:
> *"We are not guessing or merging firms automatically. The system is
> surfacing review work safely so Salesforce and WIP stay aligned."*

**Do not say:**
- "FastAPI", "MariaDB", "wip_company", port numbers, or any field name
- "We could automate that" — even hypothetically
- "ERic" — different product surface; out of scope for this demo

**Tip:** if the client asks "is this real data", confirm yes — pulled
live from Salesforce and the Mastercopy import. Say *"these are the
real numbers — we're not showing a sandbox."*

---

### Step 2 — Firm Groups (the pending queue)

**Open:** `http://192.168.55.10:8300/data-review/groups`
(That's the "Awaiting review" view — the default.)

**Point at:** the top three rows. Knights, WBW, Porter Dodson — high
Priority numbers, many Salesforce accounts each, "Many locations" tag.

**Say:**
> *"These are firms where Salesforce has more than one account for
> what looks like the same business. The system is asking your team:
> 'is this one firm or several?'. We're not deciding for you. We're
> surfacing it ranked by impact — biggest first."*

Click into Knights or any high-priority row briefly to show the table
of member accounts. **Do not** click a button yet — that's Step 3.

**Do not say:**
- "Canonical fragmentation" — say "Salesforce showing several accounts
  for one firm" instead
- "We have 277 of these" — true but spammy; the client's eye is already
  on the count from the dashboard

**Tip:** if the client asks where these came from, the honest answer is
*"the system compared Salesforce account names and flagged clusters.
Operators decide each one."*

---

### Step 3 — A confirmed group (Hugh James)

**Open:** `http://192.168.55.10:8300/data-review/groups/68`

**Point at:** the green "Confirmed" badge, the four metric cards
(Current WIP / Active cases / Webinar attendees / Unmatched Mastercopy
rows), then the member table with the **Primary (HQ)** row at the top.

**Say:**
> *"This is what a confirmed group looks like. Hugh James — five
> Salesforce accounts (Cardiff, London, Plymouth, Southampton,
> Manchester), one firm. London is set as the headquarters. Reports
> now roll up to the firm, not the branch. The decision is on record —
> who confirmed it, when, with what note."*

Scroll to the note: *"primary=London office per commercial team
confirmation"* (or whatever the live note is).

**Do not say:**
- "wip_company_id 7222"
- "canonical_company_id 20425"
- "We applied this via APPROVE_GROUP in the apply script"

**Tip:** this is the moment to land the message a second time:
*"Note we did not merge anything in Salesforce. The Salesforce accounts
are unchanged. What we changed is how reports group them."*

---

### Step 4 — Possible Duplicate Accounts

**Open:** `http://192.168.55.10:8300/data-review/duplicates`

**Point at:** the intro paragraph and a couple of small clusters
(russell, glaisyers).

**Say:**
> *"Different problem here. This page lists Salesforce accounts that
> look like duplicates of each other — same firm name, two or three
> separate records. The merge actually happens in Salesforce — we
> don't touch it. This page just records what your team decided so
> the next sync skips it."*

Click into one to show the three buttons: *Not a duplicate* / *Mark
resolved in Salesforce* / *Save note*. **Do not click submit.**

**Do not say:**
- "We have an algorithm that auto-merges" — we don't, and we never will
- "Salesforce duplicate management" feature names — out of scope

**Tip:** the page literally says *"The actual merge happens in
Salesforce"* in bold — point at it. The wording is intentional.

---

### Step 5 — Unmatched Mastercopy Rows

**Open:** `http://192.168.55.10:8300/data-review/rejects`

**Point at:** the row count at the top, the filter dropdown (reject
reason), and one or two example rows.

**Say:**
> *"This is the most operationally important page. When the
> Mastercopy workbook gets imported, every row gets one of four
> outcomes: imported, updated, unchanged, or rejected. These are the
> rejected ones — we did not import them, because we couldn't safely
> identify the firm or the case reference. About 25,000 rows live
> here right now; that number drops every time the team fixes the
> underlying problem in Salesforce."*

Click into one (any "No Company Match" row). Show: original Mastercopy
row, what the system tried, the two action buttons.

**Say (closing):**
> *"The pattern is the same: fix or add the firm in Salesforce first,
> then the next sync brings it in, and the next import lands the row.
> There's no button here that creates a Salesforce account from the
> Mastercopy. That's deliberate. Salesforce stays the system of
> record."*

**Do not say:**
- "We could automate Salesforce account creation" — explicitly out of scope
- "The reject queue is huge" — it's a feature of the design, not a problem

---

## Closing — 2 minutes

Return to the dashboard. Say:

> *"That's the loop. Salesforce stays the source of truth. The
> Mastercopy keeps being your monthly working document. WIP lives in
> between, never guessing, always surfacing the questions for the
> team to answer. Each decision is on record."*

Then offer the next move:

> *"Two things we could do next: (a) walk a small batch of firm groups
> through together so your team gets comfortable with the buttons; or
> (b) leave the queue with you for a week and see what you push
> through. Either works. What would suit you?"*

---

## Anticipated client questions + how to answer

| Question | Honest answer |
|---|---|
| Can it tell us which is the right Salesforce account to keep? | No — we surface the cluster; the merge decision is yours and happens in Salesforce. |
| Why are there 25,000 unmatched rows? | Mostly because the Mastercopy has firm names that aren't yet in Salesforce, or rows that are summary/totals lines. The number drops as you add firms to Salesforce. |
| Can I edit a Salesforce account from here? | No. That would split source-of-truth across two systems. The flow stays: Salesforce first, sync brings it in. |
| Can I see who confirmed which group? | Yes — every confirmed/rejected group shows the user and timestamp. (Show Hugh James's note.) |
| Can the system auto-confirm obvious ones? | Technically yes, deliberately no. Confirmation is a decision, not a calculation. The whole value is that it's on record against a person. |
| What about logging in? | This deployment is on the internal network without login. If we expose it more broadly we'll add a login layer first. |
| Is this final? | No — this is the operator review surface. The next step (only after you've used it) is rolling group-level reporting into ERic and starting to design the WIP case editing surface. |

---

## Don't do during the demo

- Don't open a terminal. Don't open the database. Don't open the repo.
- Don't show the dropdown items that still say "not yet built"
  (Companies / Live Cases / Imports / Reports / Admin / Firm Aliases).
  If the client clicks one, just say *"that page is queued — we
  prioritised the review surface first because that's where your team
  comes in."*
- Don't promise dates for the queued pages. The order will follow how
  the client uses the review surface.
- Don't click "Confirm group" or "Reject group" on a live cluster
  during the demo unless you've discussed it with the client and they
  agree to make a real decision. The buttons commit.

---

## After the demo

1. Capture any concrete decisions in the GUI itself (operator notes on
   the groups discussed). The trail goes on the record automatically.
2. If the client asked for a feature, file it as a follow-up WO — do
   not commit to it on the call.
3. If the client wants the GUI in front of more people, raise auth
   first; do not widen the network reach without it.

---

## Related

- WO-14A.4 — the GUI itself (`src/wip_web/`)
- WO-14A.5 — the wording pass this demo relies on
- WO-14A.2 — the original CSV operator runbook (kept for reference,
  superseded by the GUI for client-facing use)
- WO-14B — the blueprint behind the group model
- WO-13 — the canonical-fragmentation evidence that motivated the
  whole `wip_company_group` layer
