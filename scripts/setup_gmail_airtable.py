"""
One-shot schema setup for the Gmail thread scraper.

Creates the `Email Threads` table (or adds any missing fields if it already
exists) in the scraper's Airtable base. Idempotent — safe to re-run.

Base id: GMAIL_AIRTABLE_BASE_ID (falls back to AIRTABLE_BASE_ID).
Token:   AIRTABLE_PAT (falls back to AIRTABLE_TOKEN).

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
    {"name": "Snippet", "type": ML},
    {"name": "Gmail Link", "type": "url"},
    {"name": "Mailbox", "type": T},
    {"name": "Synced At", **DATETIME},
]


def get_tables(base_id):
    r = requests.get(f"{META}/{base_id}/tables", headers=HEADERS, timeout=30)
    r.raise_for_status()
    return {t["name"]: t for t in r.json().get("tables", [])}


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


def main():
    if not PAT or not BASE:
        print("ERROR: set AIRTABLE_PAT and GMAIL_AIRTABLE_BASE_ID (or AIRTABLE_BASE_ID)")
        sys.exit(1)

    print(f"=== Gmail scraper schema setup → base {BASE}, table '{TABLE}' ===")
    tables = get_tables(BASE)

    if TABLE in tables:
        print(f"  ~ '{TABLE}' exists — adding any missing fields")
        existing = field_names(tables[TABLE])
        for f in FIELDS:
            if f["name"] in existing:
                print(f"    ~ {f['name']} (already exists)")
            else:
                add_field(BASE, tables[TABLE]["id"], f)
    else:
        r = requests.post(f"{META}/{BASE}/tables",
                          json={"name": TABLE, "fields": FIELDS},
                          headers=HEADERS, timeout=30)
        if r.status_code in (200, 201):
            print(f"    + Created table: {TABLE}")
        else:
            print(f"    ! Failed to create {TABLE}: {r.status_code} {r.text[:200]}")
            sys.exit(1)

    print("=== Done ===")


if __name__ == "__main__":
    main()
