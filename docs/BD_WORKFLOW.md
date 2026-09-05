# BD Metrics & Reporting — single source of truth

How BD activity gets from Kylas into Airtable and into the team's inbox: what
is measured, how each number is defined, what runs when, and what changed when.

If a number in a report looks wrong, start at [Definitions](#3-definitions--how-each-number-is-calculated),
then [Guarantees](#6-guarantees) — most surprises are one of the two rules there
working as intended.

**Keep this file current.** Every change to the workflow adds an entry to
[Change log](#8-change-log): what changed, why, how the logic now works, and the
impact on existing numbers. That section is the history; git blame on this file
is the audit trail.

---

## 1. The shape of it

Kylas stores only a contact's *current* pipeline stage — no history, no change
timestamps. So "how much did the team do on Tuesday?" cannot be answered by
reading Kylas. Everything below exists to answer it anyway: by remembering
yesterday's stages and comparing.

```
Kylas (contacts + their current stage)
   │  read once a day, compared against the previous snapshot
   ▼
state/contact_stage.json          the snapshot, kept in git
   │  whatever differs is a stage MOVE
   ▼
BD Stage Changes                  the evidence: one row per contact per day it moved
   │                              Previous Stage → Current Stage, with a direction
   ▼
BD Metrics Daily                  ◀── THE BASE TABLE
                                  per associate, per day, per metric,
                                  at Contact grain and Company grain
   │
   ├── Contact Matrix             W1..W6 per month, Contact metrics
   ├── Company Matrix             W1..W6 per month, Company metrics
   ├── Daily digest               one team email, computed fresh
   ├── Weekly digest              one team email, rolled up FROM the base table
   └── Monthly digest             one team email, rolled up FROM the base table
```

Two rules make this hang together:

1. **Only the base table is authoritative.** Everything below it is derived by
   re-bucketing, never by re-counting. A view therefore cannot disagree with the
   base table, and two views cannot disagree with each other.
2. **A closed period is a record, not a projection.** Once a day, week or month
   is over, its stored numbers are frozen and never rewritten.

---

## 2. What runs when

All times UTC; IST is +5:30. Scheduling is split between GitHub's own cron and
cron-job.org — GitHub's cron drifts late (observed ~2h), which was firing a
second run and a second email to the whole team, so the jobs that must land on
time are triggered externally via `workflow_dispatch` instead.

| Time (UTC) | IST | Workflow | Does |
|---|---|---|---|
| `30 1` Mon–Fri | 07:00 | `sync_team` | Refresh the roster from Kylas |
| `08:00` *(cron-job.org)* | 13:30 | `sync_1_30pm` | Midday sync |
| `0 13` Mon–Sat | 18:30 | `call_invites` | Calendar blocks |
| `13:00` *(cron-job.org)* | 18:30 | `sync_6_00pm` | EOD sync + rollup |
| **`30 12` daily** | **18:00** | **`bd_stage_changes`** | **Detect stage moves → BD Stage Changes** |
| `30 13` Mon–Fri | 19:00 | `daily_account_status` | Account Health → Kylas |
| **`30 13` daily** | **19:00** | **`bd_metrics_long`** | **Build BD Metrics Daily + send daily digest** |
| `0 14` Mon–Sat | 19:30 | `bd_trends` | Trends rollup |
| **`0 14` daily** | **19:30** | **`bd_matrix_views`** | **Contact + Company Matrix** |
| `30 2` Mon | 08:00 | `account_health_weekly` | Account Health digest |
| Sat `03:30` *(cron-job.org)* | 09:00 | `weekly_report` | Weekly team digest |
| 1st `03:30` *(cron-job.org)* | 09:00 | `monthly_report` | Monthly team digest |

**Order matters for the three in bold.** `bd_stage_changes` (18:00) writes the
evidence, `bd_metrics_long` (19:00) derives the base table from it, and
`bd_matrix_views` (19:30) rolls the base table into the matrices. Run them out
of order and the later ones read stale or empty input — each warns loudly rather
than silently reporting zeros.

---

## 3. Definitions — how each number is calculated

### Stage changes

`bd_stage_changes.py` reads every contact's current stage, compares it against
`state/contact_stage.json` from the previous run, and records a row for each
contact whose stage differs. It then saves the new stages back to the snapshot,
so **today's current stage becomes tomorrow's previous stage** — the comparison
rolls forward on its own:

```
Day 1:  LinkedIn Outreach Initiated → CNC 1      (logged; snapshot now says CNC 1)
Day 2:  CNC 1                       → Activation (logged from CNC 1, not from LinkedIn)
```

Two things this deliberately is **not**:

- **Not a call count.** A call that leaves the stage alone is invisible; a bulk
  edit that moves the stage counts even though nobody called. It measures
  *pipeline progression*, which is the thing the stage list can actually
  evidence. Real call volume lives in Kylas `/call-logs`.
- **Not per-call resolution.** Two moves between runs collapse into one row
  showing the net start → end, because the stage is only read once a day.

**First sighting.** A contact seen for the first time is normally a baseline,
not a move — otherwise the first run would report ~37k "changes" that are just
the initial read. The exception: a contact that first appears *already in
progress* (bulk-imported straight in at, say, Activation) is logged as a move
from nothing → its stage, because work demonstrably happened on it. A contact
imported at the bottom stage stays a baseline: created, not worked. This is
suppressed entirely on a bootstrap run (empty snapshot), where it would
otherwise fire for ~20k contacts at once.

### Call Attempted / Call Connected

Both are derived from the **move**, not from where a contact currently sits.
A contact counts on the day its stage moved, and only on that day.

| Metric | Rule |
|---|---|
| **Call Attempted** | The contact moved to any real stage — anything except the bottom of the funnel |
| **Call Connected** | Attempted, **and** the stage moved to is not a "Could Not Connect" stage |

So a move to `CNC (Could Not Connect) - 1` is **attempted but not connected** —
tried, couldn't reach them. A move to `Follow-up (1)` is both. A move to the
bottom stage is neither: landing back there, whether by a genuine regression or
by Kylas renaming the option, is a stage change but never evidence of a call.

The bottom stage is resolved by **rank position**, not by matching its name —
see [Gotchas](#7-gotchas-things-that-have-actually-bitten-us).

The remaining Contact metrics (Meeting Booked, Meeting Done, SQL, MQL) are
scored the same way, from the stage moved to, using the definitions in
`bd_monthly_matrix.py` so the daily view and the monthly matrix cannot drift.

### Last Call Date

A contact's last call date is the date of its most recent **detected stage
move**, and nothing else. Not its creation, not an unrelated edit.

There is a **cutover date** (`stage_history.CALL_DATE_CUTOVER`, currently
`2026-09-05`):

- **Before the cutover** — the old fallback still applies (`cfLastCalledAt`, or
  the `max(createdAt, updatedAt, cfLastCalledAt)` composite), so every figure
  already measured stays exactly as measured.
- **From the cutover onward** — the fallback is ignored entirely. A contact
  counts as called on a day only if its stage actually moved.

The old composite could be bumped by our own automation editing an unrelated
field — an owner reassignment made a contact look called — which is what made it
untrustworthy. The deliberate consequence: forward-looking counts start near
zero and fill in as real moves accumulate. An unmoved contact was never evidence
of a call; it only looked like one.

**Company-level last call date** is then the latest across that company's
contacts. It is not measured separately.

---

## 4. The tables

### BD Stage Changes — the evidence

One row per contact per day it moved.

`Date · Contact · Company · Company Id · BD Associate · Previous Stage · Current Stage · Direction`

Direction is Forward / Backward, judged by `config/account_pipeline_order.json`
(rank 1 = best), the same order the Account Pipeline Stage rollup uses. Retained
400 days.

### BD Metrics Daily — the base table

One row per associate per day per metric, in long form so metric *names* can go
on a chart axis.

`Date · Week · Month · BD Associate · BD Email · Metric Group · Metric · Value`

`Metric Group` is `Contact` or `Company` — never mix them in one chart, one
counts people and the other counts accounts.

| Group | Metrics |
|---|---|
| Contact | Call Attempted, Call Connected, Meeting Booked, Meeting Done, SQL, MQL |
| Company | Companies Worked, Companies Reached, Requirements Stated, Handoff Calls Held, SQLs Accepted, SQLs Rejected |

Company metrics count each account once per day at its **best** stage that day.

### Contact Matrix / Company Matrix — the week views

One row per associate per month per metric, weeks across the columns:

```
BD Associate   Month     Metric           W1   W2   W3   W4   W5   Month Total
Anjali Athya   2026-09   Call Attempted   12   18   15    0    6            51
```

**W1 is the calendar week containing the 1st** — not "days 1–7". Weeks cut on
the same Monday boundary as the ISO week used everywhere else, so "W3" here and
the weekly digest's week always cover the same days.

Months run to **five** weeks far more often than four, and to six when a 31-day
month starts on a Sunday, so the table carries W1–W6. Unused weeks are left
blank rather than written as `0`: "no such week" and "a week nobody worked" are
different statements, and only the second is a zero.

---

## 5. Reporting

**One email per period, to the whole team** — not one per person.

| Digest | When | Window |
|---|---|---|
| Daily | 19:00 UTC daily | Mixed: today's activity, plus SQL month-to-date and Handoff week-to-date |
| Weekly | Sat 03:30 UTC | Every column covers the ISO week |
| Monthly | 1st, 03:30 UTC | Every column covers the calendar month |

All three are the same code with a different window, all sorted by **SQL,
highest first**, and all carry a **TEAM TOTAL** row.

The daily digest keeps its mixed windows on purpose: its SQL and Handoff columns
are running period totals, so a quiet day still shows where each rep stands for
the month. A test pins that shape — changing the report the team reads every
evening should be a decision, not a side effect.

The weekly and monthly digests **read the stored base table** rather than
recomputing (`--email-only`). Closed days in it are frozen, so reading them back
reports exactly the figures that were measured; recomputing could quietly
disagree with numbers already sent.

### Retired, not deleted

These still exist and are runnable by hand, but are off every schedule:

- `modules/04_email_alert.py` — the per-person 1:30pm / 6:30pm emails
- `modules/06_periodic_report.py` — the per-person weekly / monthly loop (16 emails each)
- `modules/07_hot_pipeline.py` — Hot Pipeline digest
- `_send_poc_emails()` in `06_account_health.py` — per-POC exhaust emails

---

## 6. Guarantees

**Closed periods are frozen.** Once a day, week or month is over, its stored
row is never rewritten. Re-deriving a closed period silently shrinks it —
contacts get reassigned, roster membership changes, definitions evolve — so
history would decay every time a job ran. Freezing is what makes a number you
read last month still true this month.

Freezing is applied at every grain: days in BD Metrics Daily and BD Stage Change
Daily, months in the two matrices.

**A failed derivation writes nothing rather than something wrong.** If the stage
ranking config fails to load, the Account Pipeline Stage column is left
untouched rather than blanked; if the base table is empty, the roll-ups warn
loudly instead of publishing zeros.

---

## 7. Gotchas — things that have actually bitten us

**A picklist rename looks like activity.** Kylas relabelled option `2862826`
from "Yet to Be Mined" to "LinkedIn Outreach Initiated". Every contact sitting
at the bottom of the funnel appeared to move — 12,638 of them in one run, all
of which would have registered as calls. Guard: `is_call` decides separately
whether *landing on* a stage counts as a call, so a rename records the change
but never the call. Code resolves the bottom stage by **rank position**
(`order.unmined_label`), never by matching a literal string, so the next rename
needs no code change.

**Kylas returns bare integers where you expect objects.** On search results
`company` is an int and the pipeline stage is a bare option id; the nested
object only appears on detail reads. Both shapes must be handled — this silently
blanked the Company column in the change log until it was fixed.

**Stage names are spelled inconsistently.** En-dash vs hyphen, spacing around
separators, and British "Organisation" vs American "Organization". Everything
compares through `account_pipeline._norm()`. Never match a stage name raw.

**`continue-on-error` does not survive a job timeout.** It forgives a *step*
failure; a *job* timeout is unconditional and kills everything. A step that can
run long needs its own `timeout-minutes` **below** the job's, or the whole run
is marked cancelled even after the important work succeeded.

**The base table is a hard dependency, not a nicety.** `bd_metrics_long` reads
BD Stage Changes; `bd_matrix_views` reads BD Metrics Daily. If an upstream job
did not run, the downstream one has nothing to derive from and says so.

---

## 8. Change log

Newest first. Every entry: **what** changed, **why**, **how** it works now, and
the **impact** on existing numbers.

### 2026-09-05 — Contact Matrix and Company Matrix

- **What** — Two new derived views, `Contact Matrix` and `Company Matrix`, rolled
  up from BD Metrics Daily into week-of-month columns W1–W6 with a month total.
  New workflow `bd_matrix_views` at 14:00 UTC daily.
- **Why** — Week-wise and month-wise history per associate, without overwriting
  previous periods.
- **How** — Nothing is measured; the base table is read back and re-bucketed by
  week of month. W1 is the calendar week containing the 1st, cut on the same
  Monday boundary as the ISO week, so matrix weeks and digest weeks always agree.
  Once a month is over its rows are frozen and the next month starts new rows.
- **Impact** — Additive. No existing number changes.

### 2026-09-05 — BD Metrics Daily became the base table

- **What** — The weekly and monthly digests now read the stored BD Metrics Daily
  rows instead of recomputing from the change log.
- **Why** — Closed days in the base table are frozen. Reading them back reports
  exactly what was measured; recomputing could quietly disagree with numbers
  already emailed out.
- **How** — `read_metrics_daily()` reconstructs the same shape `build_long()`
  produces, so a digest cannot tell whether its rows were computed or read back.
- **Impact** — Weekly/monthly figures become stable once written. Daily is
  unchanged: it still computes fresh, because today is not frozen yet.

### 2026-09-05 — One team digest per period

- **What** — Weekly and monthly reports consolidated from one email per BD
  associate (~16 each) into a single team email each, matching the daily digest.
  Added a TEAM TOTAL row to all three.
- **Why** — Email volume: roughly 32 individual reports a week, none of which
  showed anyone the team picture.
- **How** — `PERIODS` maps daily/weekly/monthly to a window, a column set and a
  sort column; the three digests are the same code over different windows. All
  sort by SQL descending.
- **Impact** — Per-person weekly/monthly emails stop. The numbers themselves are
  unchanged; they are now aggregated into one table instead of split across 16.

### 2026-09-05 — Attempted/Connected derived from stage changes

- **What** — The six Contact metrics now come from a contact's stage *moving*,
  scored on the stage it moved to, rather than from where the contact currently
  sits.
- **Why** — Reading current stage counted a contact on every day its stage
  happened to be read, so one move inflated every subsequent day, and a day with
  no activity still reported numbers.
- **How** — `build_long()` reads the BD Stage Changes log instead of Kylas.
  Attempted = moved to any real stage; Connected = attempted and not to a CNC
  stage. Definitions still imported from `bd_monthly_matrix.py`, so only the
  *input* changed, not the rules.
- **Impact** — **Every Contact metric changes.** Daily numbers drop to what
  actually moved that day. Also removed a ~33k-contact Kylas fetch from the
  daily run. Added `Company Id` to the change log, which had only stored a
  company *name* — blank on every search result, so company roll-ups had nothing
  to group by.

### 2026-09-05 — Last Call Date cutover

- **What** — From `2026-09-05`, last call date comes only from detected stage
  moves. Before that date the old fallback still applies.
- **Why** — The old composite (`createdAt` / `updatedAt` / `cfLastCalledAt`)
  could be bumped by our own automation editing an unrelated field, so it was
  not evidence of a call.
- **How** — `effective_call_date()` honours the fallback only for dates before
  `CALL_DATE_CUTOVER`. A contact first seen already in progress now counts as a
  move; one first seen at the bottom stage does not. Suppressed on bootstrap runs.
- **Impact** — Numbers before the cutover are untouched. From the cutover,
  counts start near zero and build as real moves accumulate.
