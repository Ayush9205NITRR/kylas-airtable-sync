# Kylas -> Airtable Sync

Automated sync from Kylas CRM to Airtable, running twice daily via GitHub Actions.

> **Also in this repo:** the **Cold Call Analysis System** (`cold_call/`) — a
> separate daily pipeline that transcribes BD call recordings from Google Drive,
> scores them with Gemini, stores results in Airtable, and emails each BD a
> coaching summary. See [`cold_call/README.md`](cold_call/README.md). It runs
> independently of the Kylas sync (own deps in `cold_call/requirements.txt`, own
> Airtable base via `COLD_CALL_AIRTABLE_BASE_ID`).

## Setup

### 1. Install
```bash
pip install -r requirements.txt
cp .env.example .env   # fill in your keys
```

### 2. Airtable tables to create

**Base: Kylas CRM - Sales Pipeline**
- `Contacts` - fields: Kylas Contact Id (Text), First Name, Last Name, Email, Phone, Company Id, Designation, Created At (Text), Updated At (Text)
- `Deals` - fields: Kylas Deal Id (Text), Deal Name, Deal Value (Number), Currency, Pipeline, Stage, Contact Id, Company Id, Expected Close Date, Created At (Text), Updated At (Text)
- `Sync Log` - fields: Run ID (Text), Module (Single select), Status (Single select: running/success/failed), Created (Number), Updated (Number), Failed (Number), Error (Long text), Started At (Date+time), Finished At (Date+time)

**Base: Company Database**
- `Company List` - fields: Kylas Company Id (Text), Company Name, Industry, Website, Phone, Email, City, State, Country, Description, Created At (Text), Updated At (Text)

> IMPORTANT: `Created At` and `Updated At` must be Text fields (not Date), so the exact Kylas timestamp string is preserved for change-detection.

### 3. GitHub Secrets

Settings -> Secrets -> Actions:

| Secret | Value |
|--------|-------|
| `KYLAS_API_KEY` | Kylas API key (uuid:tenantId) |
| `AIRTABLE_PAT` | Airtable Personal Access Token |
| `AIRTABLE_BASE_ID` | Base ID for Kylas CRM - Sales Pipeline |
| `AIRTABLE_COMPANY_BASE_ID` | Base ID for Company Database |
| `RESEND_API_KEY` | Resend API key |
| `RESEND_FROM_EMAIL` | Verified sender (or `onboarding@resend.dev` for testing) |
| `ALERT_EMAIL` | Where to send sync reports |

### 4. Test each module
```bash
python modules/01_company_sync.py --test
python modules/02_contact_sync.py --test --id=5381741
python modules/03_deal_sync.py --test
python modules/04_email_alert.py --test
```

### 5. Full dry-run
```bash
python run_sync.py --dry-run
```

### 6. Trigger manually on GitHub
Actions tab -> select workflow -> Run workflow

## Schedule
| Workflow | Cron (UTC) | IST | Slot |
|----------|------------|-----|------|
| sync_1_30pm.yml | `0 8 * * 1-6` | 1:30 PM Mon-Sat | first_half |
| sync_6_00pm.yml | `30 12 * * 1-6` | 6:00 PM Mon-Sat | full_day |

## Upsert logic
```
Record exists in Airtable (by Kylas ID)?
  YES -> Updated At changed? YES -> UPDATE / NO -> SKIP
  NO  -> CREATE
```
