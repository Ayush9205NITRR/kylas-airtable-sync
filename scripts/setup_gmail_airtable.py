"""
One-shot schema setup for the Gmail thread scraper. Idempotent — safe to re-run,
and it only ever *adds*: existing tables, fields and data are left alone.

Sets up two tables in the scraper's Airtable base:

  * the thread table (GMAIL_TABLE_NAME — a table id or name) with the scraped
    columns plus a `Files` attachment field for downloadable attachments
  * `Search Terms` — the control panel. Add a row, type a name, tick Active,
    and the next run searches for it. No code change needed.

Base id: GMAIL_AIRTABLE_BASE_ID (falls back to AIRTABLE_BASE_ID).
Token:   AIRTABLE_PAT (falls back to AIRTABLE_TOKEN), needs schema write scope.

    python scripts/setup_gmail_airtable.py
"""
import os
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gmail_scraper import config

PAT = config.AIRTABLE_TOKEN
BASE = config.AIRTABLE_BASE_ID
TABLE = config.TABLE_NAME
TERMS_TABLE = config.SEARCH_TERMS_TABLE
HEADERS = {"Authorization": f"Bearer {PAT}", "Content-Type": "application/json"}
META = "https://api.airtable.com/v0/meta/bases"

T = "singleLineText"
ML = "multilineText"
N = "number"
NP = {"precision": 0}
DATETIME = {"type": "dateTime", "options": {
    "dateFormat": {"name": "iso"},
    "timeFormat": {"name": "24hour"},
    "timeZone": "Asia/Kolkata",
}}

# Thread ID first => Airtable makes it the primary field, which is what the
# upsert (fieldsToMergeOn) keys on.
FIELDS = [
    {"name": config.KEY_FIELD, "type": T},
    {"name": "Category", "type": T},
    {"name": "All Categories", "type": T},
    {"name": "Subject", "type": T},
    {"name": "Sender Email", "type": "email"},
    {"name": "Sender Name", "type": T},
    {"name": "To Emails", "type": ML},
    {"name": "CC Emails", "type": ML},
    {"name": "First Email Date", **DATETIME},
    {"name": "Last Email Date", **DATETIME},
    {"name": "Attachments", "type": ML},
    {"name": "Attachment Count", "type": N, "options": NP},
    {"name": "Message Count", "type": N, "options": NP},
    {"name": config.ATTACHMENT_FIELD, "type": "multipleAttachments"},
    {"name": "Snippet", "type": ML},
    {"name": "Gmail Link", "type": "url"},
    {"name": "Mailbox", "type": T},
    {"name": "Synced At", **DATETIME},
]

# The control panel: search terms live here so they can be edited in Airtable.
SEARCH_TERMS_FIELDS = [
    {"name": "Search Term", "type": T,
     "description": "A name or phrase to look for. Plain text is matched as an "
                    "exact phrase anywhere in the mailbox; full Gmail syntax "
                    "(from:, has:attachment, OR ...) also works."},
    {"name": "Active", "type": "checkbox",
     "options": {"icon": "check", "color": "greenBright"}},
    {"name": "Category", "type": T,
     "description": "Optional label for matches. Defaults to the search term."},
    {"name": "Scope", "type": T,
     "description": "Optional. Overrides 'in:anywhere' for this row."},
    {"name": "Notes", "type": ML},
]


def get_tables(base_id):
    r = requests.get(f"{META}/{base_id}/tables", headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json().get("tables", [])


def find_table(tables, wanted):
    """Match on table id or name — GMAIL_TABLE_NAME may be either."""
    for t in tables:
        if wanted in (t.get("id"), t.get("name")):
            return t
    return None


def field_names(table):
    return {f["name"] for f in table.get("fields", [])}


def add_field(base_id, table_id, field):
    time.sleep(0.3)
    r = requests.post(f"{META}/{base_id}/tables/{table_id}/fields",
                      json=field, headers=HEADERS, timeout=30)
    name = field["name"]
    if r.status_code in (200, 201):
        print(f"    + {name}")
    elif r.status_code == 422:
        print(f"    ~ {name} (already exists)")
    else:
        print(f"    ! {name} FAILED {r.status_code}: {r.text[:150]}")


def ensure_table(tables, wanted, fields, create_name):
    """Add any missing fields to `wanted`, creating the table if it's absent."""
    table = find_table(tables, wanted)
    if table:
        print(f"  ~ '{table['name']}' ({table['id']}) exists — adding missing fields")
        existing = field_names(table)
        for f in fields:
            if f["name"] in existing:
                print(f"    ~ {f['name']} (already exists)")
            else:
                add_field(BASE, table["id"], f)
        return True

    if wanted.startswith("tbl"):
        print(f"  ! Table id {wanted} not found in base {BASE}. Check "
              f"GMAIL_TABLE_NAME and that the PAT can see this base.")
        return False

    r = requests.post(f"{META}/{BASE}/tables",
                      json={"name": create_name, "fields": fields},
                      headers=HEADERS, timeout=30)
    if r.status_code in (200, 201):
        print(f"    + Created table: {create_name} ({r.json().get('id')})")
        return True
    print(f"    ! Failed to create {create_name}: {r.status_code} {r.text[:200]}")
    return False


def seed_search_terms():
    """Put one example row in Search Terms, but only if the table is empty."""
    url = f"https://api.airtable.com/v0/{BASE}/{requests.utils.quote(TERMS_TABLE)}"
    r = requests.get(url, headers=HEADERS, params={"maxRecords": 1}, timeout=30)
    if r.status_code >= 400 or r.json().get("records"):
        return
    r = requests.post(url, headers=HEADERS, timeout=30, json={"records": [
        {"fields": {"Search Term": "Offsite DMC", "Active": True,
                    "Notes": "Example row — edit or delete. Type any name here; "
                             "the scraper searches it as an exact phrase."}},
    ]})
    if r.status_code in (200, 201):
        print("    + Seeded an example search term ('Offsite DMC')")


def main():
    if not PAT or not BASE:
        print("ERROR: set AIRTABLE_PAT and GMAIL_AIRTABLE_BASE_ID (or AIRTABLE_BASE_ID)")
        sys.exit(1)

    print(f"=== Gmail scraper schema setup → base {BASE} ===")
    try:
        tables = get_tables(BASE)
    except requests.HTTPError as exc:
        print(f"  ! Cannot read base {BASE}: {exc}")
        print("    The PAT needs schema.bases:read + schema.bases:write and "
              "access to this base.")
        sys.exit(1)

    print(f"-- thread table: {TABLE}")
    ok = ensure_table(tables, TABLE, FIELDS, create_name="Email Threads")

    print(f"-- search terms table: {TERMS_TABLE}")
    if ensure_table(tables, TERMS_TABLE, SEARCH_TERMS_FIELDS,
                    create_name=TERMS_TABLE):
        seed_search_terms()

    print("=== Done ===" if ok else "=== Done (with warnings) ===")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
