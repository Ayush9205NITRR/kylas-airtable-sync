# Gmail Threads → Airtable

Searches one Gmail mailbox with keyword queries (e.g. `Offsite DMC in:anywhere`),
then writes **one Airtable row per email thread** with:

| Airtable field | Where it comes from |
|---|---|
| `Thread ID` | Gmail thread id — the dedup key, so re-runs update instead of duplicating |
| `Category` | The category whose keywords matched (first match wins, config order = priority) |
| `All Categories` | Every category that matched, comma-separated |
| `Subject` | Subject of the **first** message, with `Re:`/`Fwd:` stripped |
| `Sender Email` / `Sender Name` | `From` of the first message — who started the thread |
| `To Emails` | Union of `To` across the whole thread |
| `CC Emails` | Union of `Cc` across the whole thread (someone looped in on reply 4 still counts) |
| `First Email Date` | Earliest message, IST |
| `Last Email Date` | Latest message, IST |
| `Attachments` | Attachment filenames across the thread, comma-separated |
| `Files` | The actual attachment files, uploaded so they're downloadable from the record |
| `Attachment Count` / `Message Count` | Counts |
| `Snippet` | Gmail's preview of the opening message |
| `Gmail Link` | Direct link back to the thread |
| `Mailbox` / `Synced At` | Which account was scraped, and when |

Dates use Gmail's `internalDate` (its own receive timestamp), so a mail client
that writes a malformed `Date:` header can't skew the timeline.

---

## What you need

### 1. Google Cloud — Gmail API access

The Gmail API is the only supported route; there is no way to "scrape" Gmail
from the web UI reliably, and IMAP can't give you thread grouping cleanly.

1. [console.cloud.google.com](https://console.cloud.google.com) → create (or pick) a project
2. **APIs & Services → Library → enable "Gmail API"**
3. **OAuth consent screen** → *External* (or *Internal* if enout.in is a Workspace)
   → add the mailbox address under **Test users** if you leave the app unpublished
4. **Credentials → Create credentials → OAuth client ID → Desktop app** → download the JSON

Then, once, on your own laptop:

```bash
pip install -r gmail_scraper/requirements.txt
python -m gmail_scraper.auth_setup --client-secret ~/Downloads/client_secret.json
```

A browser opens, you approve read-only access, and it prints three values:
`GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN`. Those go into
`.env` and into GitHub Secrets. The refresh token is what lets the GitHub
Action run unattended — no browser in CI.

> On a headless box, add `--manual`: it prints a URL, you approve in any
> browser, and paste the (failed) `localhost` redirect URL back.

> **Workspace alternative:** if enout.in is Google Workspace and you'd rather
> not do the OAuth dance, a Super Admin can grant the existing service account
> (`GOOGLE_SERVICE_ACCOUNT_JSON`, already used by `cold_call/`) domain-wide
> delegation for `https://www.googleapis.com/auth/gmail.readonly`. Then just set
> `GMAIL_USER=ayush@enout.in` and skip the OAuth vars entirely. This needs admin
> console access; the OAuth route doesn't.

The scope is `gmail.readonly` — the scraper cannot send, modify, or delete mail.

### 2. Airtable

- An **Airtable PAT** with `data.records:read`, `data.records:write`,
  `schema.bases:read` and `schema.bases:write` on the target base
  (`AIRTABLE_PAT` — the repo already uses this).
- The base defaults to `appNjXRYNAQ2Nuiah` and the thread table to
  `tblyXn5UDBAxTbWYZ`; override with `GMAIL_AIRTABLE_BASE_ID` /
  `GMAIL_TABLE_NAME` (a table id or a name both work).

Create the tables and fields:

```bash
python scripts/setup_gmail_airtable.py
```

This adds the scraped columns plus the `Files` attachment field to the thread
table, and creates the `Search Terms` control table. It only ever **adds** —
existing tables, fields and rows are untouched. Idempotent, so re-run it any
time. Or run the **Gmail Scraper - Airtable Setup** workflow from the Actions
tab.

### 3. Environment variables

```bash
GMAIL_USER=ayush@enout.in          # mailbox to scrape
GMAIL_CLIENT_ID=...                # from auth_setup
GMAIL_CLIENT_SECRET=...
GMAIL_REFRESH_TOKEN=...
AIRTABLE_PAT=pat...
# optional — these have working defaults
GMAIL_AIRTABLE_BASE_ID=appNjXRYNAQ2Nuiah
GMAIL_TABLE_NAME=tblyXn5UDBAxTbWYZ
GMAIL_SEARCH_TERMS_TABLE=Search Terms
GMAIL_DEFAULT_SCOPE=in:anywhere    # scope appended to a bare name
GMAIL_LOOKBACK_DAYS=180            # default window when --since is omitted
GMAIL_MAX_THREADS=500              # per-term safety cap
GMAIL_ATTACHMENT_FIELD=Files
GMAIL_MAX_ATTACHMENT_MB=5          # Airtable's own per-file upload limit
GMAIL_MAX_ATTACHMENTS_PER_THREAD=10
```

Same names as GitHub Secrets for the scheduled workflow.

---

## Schema design — and how uniqueness works

### The decision that drives everything: one row per *thread*, not per message

A "Proposal" conversation is 8 back-and-forth mails. Store one row per message
and you get 8 near-identical rows, and columns like *First Email Date* and *Last
Email Date* stop meaning anything — every row would have the same value in both.
So the grain is the **conversation**, and the columns you asked for follow
naturally:

| Column | Grain | Taken from |
|---|---|---|
| `Subject`, `Sender Email` | the **first** message | who opened the thread |
| `First Email Date` | earliest message | when it started |
| `Last Email Date` | latest message | when it last moved |
| `CC Emails`, `To Emails` | **union across all messages** | everyone involved |
| `Attachments`, `Files` | union across all messages | everything exchanged |

That's why CC is a union: someone looped in on reply 4 is a participant of the
conversation, even though they're absent from message 1.

### The uniqueness key

`Thread ID` (Gmail's `thread.id`) is the primary field and the upsert key. It's
stable — the same conversation keeps its id forever, and new replies land in the
same thread rather than creating a new one. Two things dedupe against it:

**Within a run** — a thread matching both `proposal` *and* `cost sheet` is
collected once, not twice, and lists both in `All Categories`. The local script
prints this so you can see it:

```
[dedup] 14 hit(s) -> 11 unique thread(s) (3 matched more than one keyword)
```

**Across runs** — the write is an Airtable *upsert* keyed on `Thread ID`, so
re-running never adds a second copy. It refreshes the row instead: new
`Last Email Date`, higher `Message Count`, any new CCs. You'll see it in the
output:

```
[airtable] created 3 new row(s), updated 8 existing row(s)
```

`updated 8` is the proof — those 8 were already there and were not duplicated.

### Why not dedupe on subject?

Because half your inbox says "Proposal" and "Re: Proposal", from different
vendors, about different deals. Subject is display text, not identity. Sender +
subject is better but still collides (same DMC, two proposals) and breaks the
moment someone edits the subject line mid-thread.

There's also a `First Message ID` column holding the RFC 822 `Message-ID` of the
opening mail. That one is globally unique and identical in **every** mailbox
that received the mail, whereas a Gmail thread id is per-account. You don't need
it today; you will the day you scrape a second mailbox and need to tell "same
conversation" from "different account, coincidental id".

### The two tables

**Thread table** (`tblyXn5UDBAxTbWYZ`) — one row per conversation, fields as
listed at the top of this README. `Thread ID` must be the primary field, since
that's what the upsert merges on.

**`Search Terms`** — the control panel, described below.

Both are created by `python scripts/setup_gmail_airtable.py`, which only ever
*adds*: existing tables, fields and rows are never modified or deleted.

## Search terms — you control these, in Airtable

The **`Search Terms`** table is the control panel. Add a row, type a name, tick
`Active`. That's it — the next run searches for it. No code change, no deploy.

| Search Term | Active | Category | Scope | Notes |
|---|---|---|---|---|
| `Offsite DMC` | ✅ | | | matched as an exact phrase |
| `Acme Travels Pvt Ltd` | ✅ | `Vendors` | | filed under "Vendors" |
| `Goa DMC` | ✅ | | `in:inbox` | skips Spam/Trash for this row only |
| `Old campaign` | ⬜ | | | ignored while unticked |

Only `Search Term` is required. `Category` defaults to the term itself; `Scope`
defaults to `in:anywhere`.

**Plain names are all you need.** `Acme Travels` becomes `"Acme Travels"
in:anywhere` — quoted, so it matches the whole phrase rather than any email
containing "acme" or "travels". If you *do* type Gmail syntax
(`from:sales@dmc.com has:attachment`, `"Offsite" OR "Outing"`, `subject:quote
-label:spam`), it's detected and passed through untouched — same box, no second
field to learn.

Unticking `Active` stops a term without deleting it, so the rows it already
produced stay put.

**Priority:** rows are read top to bottom. A thread matching both `Offsite DMC`
and `Offsite` is filed under whichever is higher, with both listed in
`All Categories`.

`in:anywhere` includes Spam and Trash — Gmail's behaviour, and what your
original query asked for. Set `Scope` on a row, or `GMAIL_DEFAULT_SCOPE`
globally, to change that.

[`config/email_categories.json`](../config/email_categories.json) is the
fallback, used only when the Airtable table is unreachable or has no active
rows, so a fresh checkout still runs.

## Running it locally

One command does everything — auth check, schema, then the scrape:

```bash
pip install -r gmail_scraper/requirements.txt
cp .env.example .env          # fill in the Gmail + Airtable values
python run_gmail_local.py
```

It runs three steps and **stops at the first failure**, so you always know what
broke:

```
STEP 1/3  credentials, granted scopes, and the three Gmail calls the scraper makes
STEP 2/3  create/verify the Airtable tables and fields (only ever adds)
STEP 3/3  search "proposal" and "cost sheet", show the first 5, ask before writing
```

Nothing is written until you confirm. Useful variants:

```bash
python run_gmail_local.py --check-only                     # just step 1
python run_gmail_local.py --dry-run                        # look, never write
python run_gmail_local.py --keywords "proposal,cost sheet,quotation"
python run_gmail_local.py --limit 20 --since 1y --yes
python run_gmail_local.py --use-airtable-terms             # terms from Airtable
```

### The underlying module

`run_gmail_local.py` is a wrapper; the pipeline is usable directly:

```bash
# test run: first 5 threads only, nothing written
python -m gmail_scraper.pipeline --limit 5 --dry-run

# same 5, written to Airtable with attachments
python -m gmail_scraper.pipeline --limit 5

# the full set, last 180 days
python -m gmail_scraper.pipeline

# one term from the table, all time
python -m gmail_scraper.pipeline --category "Offsite DMC" --since all

# a name that isn't in the table yet
python -m gmail_scraper.pipeline --term "Acme Travels" --since 1y

# raw Gmail query, verbatim
python -m gmail_scraper.pipeline --query 'from:sales@dmc.com has:attachment'

# skip file uploads (filenames still recorded), or dump to JSON
python -m gmail_scraper.pipeline --no-attachments
python -m gmail_scraper.pipeline --dry-run --json /tmp/threads.json
```

Check what terms would run, without touching Gmail:

```bash
python -m gmail_scraper.search_terms
```

Check auth on its own:

```bash
python -m gmail_scraper.gmail_client    # prints the mailbox + a per-category thread count
```

Scheduled run: `.github/workflows/gmail_scrape.yml`, 7:00 AM IST daily, plus
manual dispatch with the same options as flags.

## Attachments

Real files land in the `Files` column, downloadable straight from the record.
The scraper pulls the bytes from Gmail and pushes them through Airtable's
content-upload API — no public URL or intermediate hosting needed.

Two caps, both adjustable:

- **5 MB per file** (`GMAIL_MAX_ATTACHMENT_MB`) — Airtable's own upload limit,
  so raising it past 5 won't help. Oversized files are skipped, but their names
  still appear in the `Attachments` text column and `Gmail Link` reaches the
  original mail.
- **10 files per thread** (`GMAIL_MAX_ATTACHMENTS_PER_THREAD`) — stops one
  40-attachment thread stalling a run.

Re-runs only upload filenames the record doesn't already have, so nothing
duplicates. `--no-attachments` records names only.

## Notes & limits

- Gmail's date operators (`newer_than:`, `after:`) are **day-granular**, so
  `--since` can't be finer than a day.
- Re-running is cheap and safe: the upsert keys on `Thread ID`, so an active
  thread just gets its `Last Email Date`, `Message Count` and CC list refreshed.
- API quota is 250 units/user/second; a `threads.get` costs 10. The scraper
  backs off automatically on 429/5xx.

## Tests

```bash
python -m unittest tests.test_gmail_scraper -v
```

Parsing, subject cleanup, CC union, attachment walking and query building are
all covered with hand-built Gmail payloads — no network needed.
