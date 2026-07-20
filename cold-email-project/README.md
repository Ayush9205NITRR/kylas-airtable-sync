# Cold Email Automation

Automated cold outreach system: sends templated emails across 3 warmed-up Gmail inboxes, tracks opens/clicks via a Cloudflare Worker pixel and click redirect, detects replies/bounces, and rotates sending based on per-inbox health scores. Airtable is the data layer.

## Project layout

```
docs/     - Shared source-of-truth docs (schema, API contract, per-agent requirements)
sender/   - Agent A: Gmail sending, inbox rotation, warm-up curve
tracking/ - Agent A: Cloudflare Workers for pixel tracking and click redirects
data/     - Agent B: Airtable setup, primary-email enforcement, reply polling, health scoring
reports/  - Agent B: Daily report generation
```

## Start here

- [`docs/schema.md`](docs/schema.md) — Airtable table/field definitions. Source of truth for all field names.
- [`docs/api-contract.md`](docs/api-contract.md) — Interfaces shared between the sending side and the data/reporting side (tracking pixel, click redirect, inbox health lookup, send event logging).
- [`docs/agent-a-requirements.md`](docs/agent-a-requirements.md) — Sending + tracking infrastructure requirements.
- [`docs/agent-b-requirements.md`](docs/agent-b-requirements.md) — Data layer + reporting requirements.

## Ownership split

- **Agent A** owns `/sender`, `/tracking`, and `/.github/workflows/send-daily.yml`.
- **Agent B** owns `/data`, `/reports`, `/.github/workflows/poll-replies.yml`, and `/.github/workflows/daily-report.yml`.

Neither side may rename or remove a shared field/function signature without updating `docs/schema.md` or `docs/api-contract.md` accordingly.
