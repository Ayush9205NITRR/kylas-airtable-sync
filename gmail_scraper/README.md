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
- A **base id** for where the threads should live: `GMAIL_AIRTABLE_BASE_ID`.
  Use a dedicated base so scraped mail never mixes with the Kylas CRM data.
  (If unset it falls back to `AIRTABLE_BASE_ID`.)

Create the table and its fields:

```bash
python scripts/setup_gmail_airtable.py
```

Idempotent — re-run it any time you add a field. Or run the **Gmail Scraper -
Airtable Setup** workflow from the Actions tab.

### 3. Environment variables

```bash
GMAIL_USER=ayush@enout.in          # mailbox to scrape
GMAIL_CLIENT_ID=...                # from auth_setup
GMAIL_CLIENT_SECRET=...
GMAIL_REFRESH_TOKEN=...
AIRTABLE_PAT=pat...
GMAIL_AIRTABLE_BASE_ID=app...
# optional
GMAIL_LOOKBACK_DAYS=180            # default window when --since is omitted
GMAIL_MAX_THREADS=500              # per-category safety cap
GMAIL_TABLE_NAME=Email Threads
```

Same names as GitHub Secrets for the scheduled workflow.

---

## Categories

Keywords live in [`config/email_categories.json`](../config/email_categories.json)
so you can add categories without touching code:

```json
{
  "default_scope": "in:anywhere",
  "categories": [
    { "name": "Offsite DMC", "keywords": ["Offsite DMC"] },
    { "name": "Offsite",     "keywords": ["offsite", "team outing"] },
    { "name": "Raw query",   "query": "from:dmc@example.com has:attachment in:anywhere" }
  ]
}
```

- `keywords` are OR'd and quoted, then suffixed with `default_scope` →
  `("offsite" OR "team outing") in:anywhere`
- `query` (if given) is passed to Gmail verbatim — full search syntax works:
  `from:`, `to:`, `has:attachment`, `label:`, `subject:`, `-exclude`
- **Order = priority.** A thread matching both "Offsite DMC" and "Offsite" is
  filed under `Offsite DMC` (listed first) and lists both in `All Categories`.

`in:anywhere` includes Spam and Trash — that's Gmail's behaviour, and it's what
your example query asked for. Drop it from `default_scope` to search only the
normal mailbox.

## Running it

```bash
# everything in the config, last 180 days, written to Airtable
python -m gmail_scraper.pipeline

# see what would land, without writing
python -m gmail_scraper.pipeline --dry-run --limit 20

# one category, all time
python -m gmail_scraper.pipeline --category "Offsite DMC" --since all

# a one-off query that isn't in the config
python -m gmail_scraper.pipeline --query 'Offsite DMC in:anywhere' \
    --category-name "Offsite DMC" --since 1y

# dump to JSON as well
python -m gmail_scraper.pipeline --dry-run --json /tmp/threads.json
```

Check auth on its own:

```bash
python -m gmail_scraper.gmail_client    # prints the mailbox + a per-category thread count
```

Scheduled run: `.github/workflows/gmail_scrape.yml`, 7:00 AM IST daily, plus
manual dispatch with the same options as flags.

## Notes & limits

- **Attachments are stored as filenames**, not files. Uploading the actual
  binaries into an Airtable attachment field means hosting each file at a public
  URL first — worth doing only if you actually need the files in Airtable.
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
