"""
One-shot schema setup. Run this before the first full sync.

Creates:
  - New columns in Company List (Company Database base)
  - Companies table in CRM Sales Pipeline base
  - New columns in Contacts (CRM base) + Company linked record field
  - Deals table in CRM base + Company linked record field
"""
import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()

PAT           = os.environ["AIRTABLE_PAT"]
COMPANY_BASE  = os.environ.get("AIRTABLE_COMPANY_BASE_ID") or os.environ["AIRTABLE_BASE_ID"]
CRM_BASE      = os.environ["AIRTABLE_BASE_ID"]
HEADERS       = {"Authorization": f"Bearer {PAT}", "Content-Type": "application/json"}
META          = "https://api.airtable.com/v0/meta/bases"

T  = "singleLineText"
ML = "multilineText"


def get_tables(base_id):
    r = requests.get(f"{META}/{base_id}/tables", headers=HEADERS, timeout=30)
    r.raise_for_status()
    return {t["name"]: t for t in r.json().get("tables", [])}


def field_names(table):
    return {f["name"] for f in table.get("fields", [])}


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
        print(f"    ! {name} FAILED {r.status_code}: {r.text[:150]}")


def add_missing(base_id, table, new_fields):
    existing = field_names(table)
    for f in new_fields:
        if f["name"] not in existing:
            add_field(base_id, table["id"], f)
        else:
            print(f"    ~ {f['name']} (already exists)")


def create_table(base_id, table_def):
    r = requests.post(f"{META}/{base_id}/tables", json=table_def,
                      headers=HEADERS, timeout=30)
    if r.status_code in (200, 201):
        result = r.json()
        print(f"    + Created: {table_def['name']}")
        return result
    else:
        print(f"    ! Failed {table_def['name']}: {r.status_code} {r.text[:200]}")
        return None


N  = "number"
CB = "checkbox"
NP = {"precision": 0}

# ── Company List (master database) ───────────────────────────────────────────
COMPANY_LIST_NEW = [
    {"name": "Batch",                         "type": T},
    {"name": "Pipeline Stage BD",             "type": T},
    {"name": "Source of Data",                "type": T},
    {"name": "Owner Email",                   "type": T},
    # Account health fields
    {"name": "Total POCs",                    "type": N, "options": NP},
    {"name": "YtBM POCs",                     "type": N, "options": NP},
    {"name": "Active POCs",                   "type": N, "options": NP},
    {"name": "MQL POCs",                      "type": N, "options": NP},
    {"name": "Hot POCs",                      "type": N, "options": NP},
    {"name": "Connected POCs",                "type": N, "options": NP},
    {"name": "Terminal POCs",                 "type": N, "options": NP},
    {"name": "NOI Count",                     "type": N, "options": NP},
    {"name": "Called Since Apr 19",           "type": N, "options": NP},
    {"name": "Last Called At (Contacts)",     "type": T},
    {"name": "Account Status",                "type": T},
    {"name": "Needs Re-assign",               "type": CB},
    {"name": "Claimed By",                    "type": T},
    {"name": "Status of Reachout",            "type": T},
]

# ── Companies table (CRM base — for linking) ─────────────────────────────────
COMPANIES_TABLE = {
    "name": "Companies",
    "fields": [
        {"name": "Company Name",              "type": T},
        {"name": "Kylas Company ID",          "type": T},
        {"name": "Industry",                  "type": T},
        {"name": "Owner",                     "type": T},
        {"name": "Owner Email",               "type": T},
        {"name": "Pipeline Stage BD",         "type": T},
        {"name": "Batch",                     "type": T},
        {"name": "Source of Data",            "type": T},
        {"name": "Updated At",                "type": T},
        # Account health fields
        {"name": "Total POCs",                "type": N, "options": NP},
        {"name": "YtBM POCs",                 "type": N, "options": NP},
        {"name": "Active POCs",               "type": N, "options": NP},
        {"name": "MQL POCs",                  "type": N, "options": NP},
        {"name": "Hot POCs",                  "type": N, "options": NP},
        {"name": "Connected POCs",            "type": N, "options": NP},
        {"name": "Terminal POCs",             "type": N, "options": NP},
        {"name": "NOI Count",                 "type": N, "options": NP},
        {"name": "Called Since Apr 19",       "type": N, "options": NP},
        {"name": "Last Called At (Contacts)", "type": T},
        {"name": "Account Status",            "type": T},
        {"name": "Needs Re-assign",           "type": CB},
        {"name": "Claimed By",                "type": T},
        {"name": "Status of Reachout",        "type": T},
    ],
}

# New fields to add to an EXISTING Companies CRM table
COMPANIES_CRM_NEW = [
    {"name": "Owner Email",                   "type": T},
    {"name": "Total POCs",                    "type": N, "options": NP},
    {"name": "YtBM POCs",                     "type": N, "options": NP},
    {"name": "Active POCs",                   "type": N, "options": NP},
    {"name": "MQL POCs",                      "type": N, "options": NP},
    {"name": "Hot POCs",                      "type": N, "options": NP},
    {"name": "Connected POCs",                "type": N, "options": NP},
    {"name": "Terminal POCs",                 "type": N, "options": NP},
    {"name": "NOI Count",                     "type": N, "options": NP},
    {"name": "Called Since Apr 19",           "type": N, "options": NP},
    {"name": "Last Called At (Contacts)",     "type": T},
    {"name": "Account Status",                "type": T},
    {"name": "Needs Re-assign",               "type": CB},
    {"name": "Claimed By",                    "type": T},
    {"name": "Status of Reachout",            "type": T},
]

# ── Contacts: new columns ─────────────────────────────────────────────────────
CONTACT_NEW = [
    {"name": "Designation",     "type": T},
    {"name": "Kylas Company Id","type": T},
    {"name": "LinkedIn",        "type": "url"},
    {"name": "City",            "type": T},
    {"name": "State",           "type": T},
    {"name": "Country",         "type": T},
    {"name": "Source",          "type": T},
    {"name": "Pipeline Stage",  "type": T},
    {"name": "Last Called At",  "type": T},
    {"name": "Next Call Date",  "type": T},
    {"name": "Remarks",         "type": ML},
    {"name": "Created At",      "type": T},
    {"name": "Updated At",      "type": T},
]

# ── Deals table ───────────────────────────────────────────────────────────────
DEALS_TABLE_BASE_FIELDS = [
    {"name": "Deal Name",             "type": T},
    {"name": "Kylas Deal Id",         "type": T},
    {"name": "Pipeline Stage",        "type": T},
    {"name": "Pipeline",              "type": T},
    {"name": "Contact Name",          "type": T},
    {"name": "Kylas Contact Id",      "type": T},
    {"name": "Company Name",          "type": T},
    {"name": "Kylas Company Id",      "type": T},
    {"name": "Deal Value",            "type": "number", "options": {"precision": 2}},
    {"name": "Actual Value",          "type": "number", "options": {"precision": 2}},
    {"name": "Owner",                 "type": T},
    {"name": "Source",                "type": T},
    {"name": "Forecast Type",         "type": T},
    {"name": "Expected Closure Date", "type": T},
    {"name": "Execution Date",        "type": T},
    {"name": "Location",              "type": T},
    {"name": "Pax Count",             "type": T},
    {"name": "Created At",            "type": T},
    {"name": "Updated At",            "type": T},
]


# ── Kylas Field Map (the mapping "UI" — edit rows in Airtable) ────────────────
# Each row maps one Airtable Company-List column → one Kylas field. Entity picks
# whether it lands on the company or on its associated contacts. Keys named
# cfXxx are pushed as Kylas custom fields.
FIELD_MAP_TABLE = {
    "name": "Kylas Field Map",
    "fields": [
        {"name": "Entity", "type": "singleSelect",
         "options": {"choices": [{"name": "Company"}, {"name": "Contact"}]}},
        {"name": "Airtable Column", "type": T},
        {"name": "Kylas Field",     "type": T},
        {"name": "Active",          "type": CB},
        {"name": "Notes",           "type": T},
    ],
}


def main():
    print("=== Airtable Schema Setup ===\n")

    # ── 1. Company Database base ──────────────────────────────────────────────
    print("1. Company Database base (Company List)")
    co_tables = get_tables(COMPANY_BASE)
    if "Company List" in co_tables:
        add_missing(COMPANY_BASE, co_tables["Company List"], COMPANY_LIST_NEW)
    else:
        print("    ! Company List table not found")

    # ── 1b. Kylas Field Map (mapping table for Airtable → Kylas field push) ───
    print("\n1b. Kylas Field Map (Company Database base)")
    if "Kylas Field Map" in co_tables:
        print("    ~ Already exists")
    else:
        create_table(COMPANY_BASE, FIELD_MAP_TABLE)

    # ── 2. CRM Sales Pipeline base ────────────────────────────────────────────
    print("\n2. CRM Sales Pipeline base")
    crm_tables = get_tables(CRM_BASE)

    # 2a. Companies table
    print("\n  [Companies]")
    if "Companies" in crm_tables:
        companies_id = crm_tables["Companies"]["id"]
        print("    ~ Already exists")
        add_missing(CRM_BASE, crm_tables["Companies"], COMPANIES_CRM_NEW)
    else:
        result = create_table(CRM_BASE, COMPANIES_TABLE)
        companies_id = result.get("id") if result else None

    # 2b. Contacts
    print("\n  [Contacts]")
    if "Contacts" in crm_tables:
        add_missing(CRM_BASE, crm_tables["Contacts"], CONTACT_NEW)
        if companies_id and "Company" not in field_names(crm_tables["Contacts"]):
            add_field(CRM_BASE, crm_tables["Contacts"]["id"], {
                "name": "Company",
                "type": "multipleRecordLinks",
                "options": {"linkedTableId": companies_id},
            })
        # Industry lookup — pulls Industry from linked Companies record
        contacts_fields  = {f["name"]: f for f in crm_tables["Contacts"].get("fields", [])}
        companies_fields = {f["name"]: f for f in crm_tables.get("Companies", {}).get("fields", [])}
        co_link_id       = contacts_fields.get("Company", {}).get("id")
        industry_fid     = companies_fields.get("Industry", {}).get("id")
        if co_link_id and industry_fid and "Industry" not in contacts_fields:
            add_field(CRM_BASE, crm_tables["Contacts"]["id"], {
                "name": "Industry",
                "type": "multipleLookupValues",
                "options": {"fieldIdInLinkedTable": industry_fid, "recordLinkFieldId": co_link_id},
            })
        elif "Industry" in contacts_fields:
            print("    ~ Industry (already exists)")
        else:
            print("    ~ Industry lookup: re-run after first sync to get field IDs")
    else:
        print("    ! Contacts table not found")

    # 2c. Deals
    print("\n  [Deals]")
    if "Deals" in crm_tables:
        print("    ~ Already exists")
        if companies_id and "Company" not in field_names(crm_tables["Deals"]):
            add_field(CRM_BASE, crm_tables["Deals"]["id"], {
                "name": "Company",
                "type": "multipleRecordLinks",
                "options": {"linkedTableId": companies_id},
            })
    else:
        fields = list(DEALS_TABLE_BASE_FIELDS)
        if companies_id:
            fields.append({
                "name": "Company",
                "type": "multipleRecordLinks",
                "options": {"linkedTableId": companies_id},
            })
        create_table(CRM_BASE, {"name": "Deals", "fields": fields})

    print("\n=== Done ===")


if __name__ == "__main__":
    main()
