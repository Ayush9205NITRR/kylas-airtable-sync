"""
One-shot script: creates missing columns in Company List + Contacts tables,
and creates the Deals table if it doesn't exist.
Run this once before the first full sync.
"""
import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()

PAT          = os.environ["AIRTABLE_PAT"]
COMPANY_BASE = os.environ.get("AIRTABLE_COMPANY_BASE_ID") or os.environ["AIRTABLE_BASE_ID"]
CONTACTS_BASE = os.environ["AIRTABLE_BASE_ID"]
HEADERS      = {"Authorization": f"Bearer {PAT}", "Content-Type": "application/json"}
META         = "https://api.airtable.com/v0/meta/bases"

T = "singleLineText"
ML = "multilineText"
N2 = {"type": "number", "options": {"precision": 2}}


def get_tables(base_id):
    r = requests.get(f"{META}/{base_id}/tables", headers=HEADERS, timeout=30)
    r.raise_for_status()
    return {t["name"]: t for t in r.json().get("tables", [])}


def add_field(base_id, table_id, field):
    time.sleep(0.3)
    r = requests.post(
        f"{META}/{base_id}/tables/{table_id}/fields",
        json=field, headers=HEADERS, timeout=30,
    )
    name = field["name"]
    if r.status_code in (200, 201):
        print(f"    + {name}")
    elif r.status_code == 422:
        print(f"    ~ {name} (already exists)")
    else:
        print(f"    ! {name} FAILED {r.status_code}: {r.text[:120]}")


def add_missing(base_id, table, new_fields):
    existing = {f["name"] for f in table.get("fields", [])}
    for f in new_fields:
        if f["name"] not in existing:
            add_field(base_id, table["id"], f)
        else:
            print(f"    ~ {f['name']} (already exists)")


def create_table(base_id, table_def):
    r = requests.post(f"{META}/{base_id}/tables", json=table_def, headers=HEADERS, timeout=30)
    if r.status_code in (200, 201):
        print(f"  + Created table: {table_def['name']}")
        return r.json()
    else:
        print(f"  ! Failed {table_def['name']}: {r.status_code} {r.text[:200]}")
        return None


# ── Company List ────────────────────────────────────────────────────────────
COMPANY_NEW = [
    {"name": "Batch",             "type": T},
    {"name": "Pipeline Stage BD", "type": T},
    {"name": "Source of Data",    "type": T},
]

# ── Contacts ─────────────────────────────────────────────────────────────────
CONTACT_NEW = [
    {"name": "Designation",     "type": T},
    {"name": "Kylas Company Id","type": T},
    {"name": "LinkedIn",        "type": "url"},
    {"name": "City",            "type": T},
    {"name": "State",           "type": T},
    {"name": "Country",         "type": T},
    {"name": "Source",          "type": T},
    {"name": "Pipeline Stage",  "type": T},
    {"name": "Remarks",         "type": ML},
    {"name": "Created At",      "type": T},
    {"name": "Updated At",      "type": T},
]

# ── Deals table (create fresh) ───────────────────────────────────────────────
DEALS_TABLE = {
    "name": "Deals",
    "fields": [
        {"name": "Deal Name",             "type": T},
        {"name": "Kylas Deal Id",         "type": T},
        {"name": "Pipeline Stage",        "type": T},
        {"name": "Pipeline",              "type": T},
        {"name": "Contact Name",          "type": T},
        {"name": "Kylas Contact Id",      "type": T},
        {"name": "Company Name",          "type": T},
        {"name": "Kylas Company Id",      "type": T},
        N2 | {"name": "Deal Value"},
        N2 | {"name": "Actual Value"},
        {"name": "Owner",                 "type": T},
        {"name": "Source",                "type": T},
        {"name": "Forecast Type",         "type": T},
        {"name": "Expected Closure Date", "type": T},
        {"name": "Execution Date",        "type": T},
        {"name": "Location",              "type": T},
        {"name": "Pax Count",             "type": T},
        {"name": "Created At",            "type": T},
        {"name": "Updated At",            "type": T},
    ],
}


def main():
    print("=== Airtable Schema Setup ===\n")

    # Company List
    print("1. Company List base")
    co_tables = get_tables(COMPANY_BASE)
    if "Company List" in co_tables:
        add_missing(COMPANY_BASE, co_tables["Company List"], COMPANY_NEW)
    else:
        print("   ! Company List table not found")

    # Contacts
    print("\n2. Contacts table")
    ct_tables = get_tables(CONTACTS_BASE)
    if "Contacts" in ct_tables:
        add_missing(CONTACTS_BASE, ct_tables["Contacts"], CONTACT_NEW)
    else:
        print("   ! Contacts table not found")

    # Deals
    print("\n3. Deals table")
    if "Deals" in ct_tables:
        print("   ~ Already exists — skipping")
    else:
        create_table(CONTACTS_BASE, DEALS_TABLE)

    print("\n=== Done ===")


if __name__ == "__main__":
    main()
