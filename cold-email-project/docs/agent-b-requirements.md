# Agent B — Requirements: Data Layer + Reporting

**Read first:** `/docs/schema.md` and `/docs/api-contract.md` — these are your source of truth. Do not invent field names.

**You own these folders — do not touch anything outside them:**
```
/data
/reports
/.github/workflows/poll-replies.yml
/.github/workflows/daily-report.yml
```

## What to build

### 1. `/data/airtable-setup.js`
- One-time script: creates all 5 tables in Airtable exactly per `/docs/schema.md`:
  - **People** (identity layer)
  - **Email Contacts** (operational layer — linked to People)
  - **Email Templates**
  - **Inboxes**
  - **Daily Report**
- Sets up the `Emails` link field on People ↔ `Person` link field on Email Contacts (two-way link)
- Idempotent — safe to run again without duplicating tables

### 2. `/data/primary-email-enforcer.js`
- New requirement: whenever an Email Contact's `Is Primary` is set to true, automatically set `Is Primary = false` on every OTHER Email Contact linked to the same Person
- Whenever an Email Contact's `Status` changes to `Bounced` and it was `Is Primary = true`, automatically promote the next `Active` Email Contact linked to that Person to `Is Primary = true` (if one exists)
- This can be an Airtable Automation (preferred, no server needed) or a script triggered via webhook — your choice, document which in the PR

### 3. `/data/reply-poller.js`
- Runs periodically (via `poll-replies.yml`), checks each of the 3 inboxes via Gmail API for new messages
- Matches incoming message sender email against Email Contacts.Email (not People — match at the email level)
- On match: sets `Replied` = true, `Reply Text` = message snippet, `Reply Date` = now on that Email Contacts row
- Detects bounce-notification emails (from mailer-daemon / postmaster) and extracts the original recipient to set `Status` = `Bounced` on that Email Contacts row (this in turn should trigger primary-email-enforcer.js logic)

### 4. `/data/health-score.js`
- Implements `getInboxHealth(inboxEmail)` and `getAvailableInboxes()` exactly per `/docs/api-contract.md` section 3
- Computes Health Score using the formula in schema.md: `100 - (BounceRate*10) - (SpamRate*20) + (OpenRate*0.5)`
- Bounce rate and open rate are computed from Email Contacts rows linked to that inbox
- Updates Inboxes.`Status` to `Paused` automatically if Health Score < 60, back to `Active` if it recovers above 75
- **This is the function Agent A's rotation.js imports — keep the function signature stable.**

### 5. `/reports/daily-report-generator.js`
- Runs once daily (end of day, via `daily-report.yml`), after send-daily.yml has run
- Aggregates the day's Email Contacts data into one new Daily Report row: Total Sent, Total Opened, Open Rate, Total Clicked, Total Replied, Total Bounced
- Breaks down by inbox and by segment (segment comes from the linked Person record, use a lookup)
- Writes an `Alerts` string if any inbox's Health Score dropped below 60 that day
- Optional: also output a simple markdown/HTML summary file to `/reports/output/{date}.md` for easy reading

### 6. `/.github/workflows/poll-replies.yml` and `/.github/workflows/daily-report.yml`
- Cron jobs for the above scripts

## Explicitly NOT your responsibility (Agent A owns these)
- Actually sending emails
- Tracking pixel / click redirect implementation
- Inbox rotation selection logic (you only PROVIDE the health data, Agent A consumes it)

## Definition of done
- `airtable-setup.js` creates all 5 tables correctly from a blank base, with the People ↔ Email Contacts link working both ways
- Adding a second Email Contact for the same Person and marking it primary correctly un-marks the first one
- `getAvailableInboxes()` returns correct data when tested against sample Inboxes rows
- Running `daily-report-generator.js` against a day with sample Email Contacts data produces a correct Daily Report row
