# API Contract — Shared Between Agent A and Agent B

Both agents build against this contract. Do not change signatures without updating this file.

## 1. Tracking Pixel (owned by Agent A, consumed by Agent B's reporting)
```
GET https://track.<yourdomain>.workers.dev/pixel/{trackingId}.gif
```
- On hit: writes to Airtable Email Contacts table where `Tracking ID` = {trackingId}
  - Sets `Email Read` = true (if not already)
  - Increments `Open Count`
  - Sets `Last Open Time` = now
- Returns a 1x1 transparent gif, always 200 status (never error, even if trackingId not found — just log and return the gif)

## 2. Click Redirect (owned by Agent A)
```
GET https://track.<yourdomain>.workers.dev/click/{trackingId}?url={encodedDestination}
```
- On hit: sets Email Contacts.`Clicked` = true for that trackingId
- 302 redirects to the decoded destination URL

## 3. Inbox Health Lookup (owned by Agent B, consumed by Agent A's rotation logic)
Function signature (implemented in `/data/health-score.js`, imported by `/sender/rotation.js`):
```js
// returns { inboxEmail, healthScore, status, currentDailyCap, sentToday }
async function getInboxHealth(inboxEmail) { ... }

// returns array of inboxes with status === 'Active', sorted by lowest sentToday first
async function getAvailableInboxes() { ... }
```
Agent A's sender MUST call `getAvailableInboxes()` before every send batch — never hardcode inbox list.

## 4. Send Event Logging (owned by Agent A, consumed by Agent B's daily report)
After every successful send, Agent A writes to the Email Contacts row:
- `Email Sent` = true, `Sent Date` = now, `Assigned Inbox` = {inboxUsed}, `Tracking ID` = {generated uuid}

Agent B's daily report job reads these fields — do not rename them without updating schema.md.
