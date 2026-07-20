# Agent A — Requirements: Sending + Tracking Infrastructure

**Read first:** `/docs/schema.md` and `/docs/api-contract.md` — these are your source of truth. Do not invent field names.

**You own these folders — do not touch anything outside them:**
```
/sender
/tracking
/.github/workflows/send-daily.yml
```

## What to build

### 1. `/sender/gmail-send.js`
- Authenticates via Gmail API OAuth for each of the 3 inboxes (credentials from GitHub Secrets, one per inbox)
- Pulls today's queued leads from Airtable (Email Contacts where `Status` = Active, `Is Primary` = true, and `Email Sent` = false, and linked Person's `Batch` matches today's active batch)
- For each Email Contact row: calls `getAvailableInboxes()` (see api-contract.md) to pick a sending inbox — respects `Current Daily Cap`
- Renders the assigned Email Template (merge fields from linked Person's lookup fields (Name, Company, Designation) into Subject/Body Variant)
- Generates a unique `Tracking ID`, embeds tracking pixel `<img src="https://track.../pixel/{id}.gif">` in body, wraps all links through `/click/{id}?url=...`
- Sends via Gmail API
- Writes back to the Email Contacts row per "Send Event Logging" in api-contract.md
- Respects per-inbox daily cap — stop sending from an inbox once its cap is hit for the day, move to next available inbox

### 2. `/sender/rotation.js`
- Implements inbox selection logic: always picks the inbox with the lowest `Emails Sent Today` among `Active` status inboxes
- Never selects a `Paused` inbox
- Logs a warning (write to a `system-log` field or console, Agent B can wire this into alerts later) if no inbox is available

### 3. `/sender/warmup-curve.js`
- Exports the warm-up curve constant from schema.md (Week 1: 15/day ... Week 5+: 55/day)
- Computes `Current Daily Cap` for a given inbox based on `Days Since Warmup Start`

### 4. `/tracking/pixel-worker.js` (Cloudflare Worker)
- Implements the pixel endpoint exactly as defined in api-contract.md section 1
- Must respond fast (<200ms) and never throw a visible error to the client

### 5. `/tracking/click-worker.js` (Cloudflare Worker)
- Implements click redirect exactly as defined in api-contract.md section 2

### 6. `/.github/workflows/send-daily.yml`
- Cron job, runs once daily (choose a sensible time, e.g. 9 AM IST)
- Runs `/sender/gmail-send.js`

## Explicitly NOT your responsibility (Agent B owns these)
- Reply/bounce detection
- Health score calculation logic (you only CONSUME `getAvailableInboxes()`, you don't implement it)
- Daily report generation
- Airtable base/table creation itself (assume tables from schema.md already exist)

## Definition of done
- Running `/sender/gmail-send.js` manually sends a real test email to a test Email Contact, pixel fires correctly on open, click redirect works, and the Email Contacts row updates correctly.
- Confirm only the `Is Primary = true` Email Contact for a Person is targeted — never send to two emails of the same Person in one run.
