"""
Create BD Dashboard tables in the CRM Airtable base (appBVetuD5Un2bgTe).

Tables created (idempotent — skips fields that already exist):
  BD Targets      — daily/weekly/monthly targets per owner per metric
  BD Activity Log — window-by-window daily achievement tracking
"""
import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()

PAT      = os.environ["AIRTABLE_PAT"]
CRM_BASE = os.environ["AIRTABLE_BASE_ID"]
HEADERS  = {"Authorization": f"Bearer {PAT}", "Content-Type": "application/json"}
META     = "https://api.airtable.com/v0/meta/bases"

T  = "singleLineText"
N  = "number"
ML = "multilineText"

METRICS = ["Attempted", "Connected", "MQL", "Discovery Call", "SQL", "Activation"]
WINDOWS = ["11:00 - 13:00", "15:00 - 18:00"]


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
        print(f"    + Created: {table_def['name']}")
        return r.json()
    print(f"    ! Failed {table_def['name']}: {r.status_code} {r.text[:200]}")
    return None


BD_TARGETS_TABLE = {
    "name": "BD Targets",
    "fields": [
        {
            "name": "Metric",
            "type": "singleSelect",
            "options": {"choices": [{"name": m} for m in METRICS]},
        },
        {"name": "Owner",         "type": T},
        {"name": "Daily Target",  "type": N, "options": {"precision": 0}},
        {
            "name": "Effective From",
            "type": "date",
            "options": {"dateFormat": {"name": "local", "format": "M/D/YYYY"}},
        },
    ],
}

# Formula fields are added after table creation (they reference other fields)
BD_TARGETS_FORMULA_FIELDS = [
    {
        "name": "Window 1 Target (11-1)",
        "type": "formula",
        "options": {"formula": "ROUND({Daily Target}/4, 0)"},
    },
    {
        "name": "Window 2 Target (3-6)",
        "type": "formula",
        "options": {"formula": "ROUND({Daily Target}/4, 0)"},
    },
    {
        "name": "Weekly Target",
        "type": "formula",
        "options": {"formula": "{Daily Target}*6"},
    },
    {
        "name": "Monthly Target",
        "type": "formula",
        "options": {"formula": "{Daily Target}*26"},
    },
]

BD_ACTIVITY_TABLE = {
    "name": "BD Activity Log",
    "fields": [
        {
            "name": "Date",
            "type": "date",
            "options": {"dateFormat": {"name": "local", "format": "M/D/YYYY"}},
        },
        {"name": "Owner",    "type": T},
        {
            "name": "Metric",
            "type": "singleSelect",
            "options": {"choices": [{"name": m} for m in METRICS]},
        },
        {
            "name": "Window",
            "type": "singleSelect",
            "options": {"choices": [{"name": w} for w in WINDOWS]},
        },
        {"name": "Achieved", "type": N, "options": {"precision": 0}},
        {"name": "Notes",    "type": ML},
    ],
}

# Automatically populated by 05_bd_stats.py on every sync run
BD_DAILY_STATS_TABLE = {
    "name": "BD Daily Stats",
    "fields": [
        {"name": "Stat Key",           "type": T},   # YYYY-MM-DD|slot|owner
        {"name": "Date",               "type": T},
        {"name": "Owner",              "type": T},
        {"name": "Slot",               "type": T},   # first_half or full_day
        {"name": "Attempted",          "type": N, "options": {"precision": 0}},
        {"name": "Connected",          "type": N, "options": {"precision": 0}},
        {"name": "Discovery Calls",    "type": N, "options": {"precision": 0}},
        {"name": "SQL",                "type": N, "options": {"precision": 0}},
        {"name": "MQL",                "type": N, "options": {"precision": 0}},
        {"name": "Activation",         "type": N, "options": {"precision": 0}},
        {"name": "W1 Attempted",       "type": N, "options": {"precision": 0}},
        {"name": "W1 Connected",       "type": N, "options": {"precision": 0}},
        {"name": "W1 Discovery Calls", "type": N, "options": {"precision": 0}},
        {"name": "W1 SQL",             "type": N, "options": {"precision": 0}},
        {"name": "W1 MQL",             "type": N, "options": {"precision": 0}},
        {"name": "W1 Activation",      "type": N, "options": {"precision": 0}},
    ],
}


def main():
    print("=== BD Dashboard Schema Setup ===\n")
    tables = get_tables(CRM_BASE)

    # ── BD Targets ────────────────────────────────────────────────────────────
    print("[BD Targets]")
    if "BD Targets" in tables:
        print("    ~ Already exists — checking formula fields")
        add_missing(CRM_BASE, tables["BD Targets"], BD_TARGETS_FORMULA_FIELDS)
    else:
        result = create_table(CRM_BASE, BD_TARGETS_TABLE)
        if result:
            print("    Adding formula fields...")
            for f in BD_TARGETS_FORMULA_FIELDS:
                add_field(CRM_BASE, result["id"], f)

    # ── BD Activity Log ───────────────────────────────────────────────────────
    print("\n[BD Activity Log]")
    if "BD Activity Log" in tables:
        print("    ~ Already exists")
    else:
        create_table(CRM_BASE, BD_ACTIVITY_TABLE)

    # ── BD Daily Stats (auto-populated by sync) ───────────────────────────────
    print("\n[BD Daily Stats]")
    if "BD Daily Stats" in tables:
        print("    ~ Already exists — checking for missing fields")
        add_missing(CRM_BASE, tables["BD Daily Stats"],
                    [f for f in BD_DAILY_STATS_TABLE["fields"] if f["name"] != "Stat Key"])
    else:
        create_table(CRM_BASE, BD_DAILY_STATS_TABLE)

    # ── Account Activity Log (auto-populated by sync) ─────────────────────────
    companies_id = tables["Companies"]["id"] if "Companies" in tables else None

    ACCOUNT_ACTIVITY_TABLE = {
        "name": "Account Activity Log",
        "fields": [
            {"name": "Stat Key",          "type": T},   # YYYY-MM-DD|kylas_company_id
            {"name": "Date",              "type": T},
            {"name": "Company Name",      "type": T},
            {"name": "Kylas Company Id",  "type": T},
            {"name": "BD Owners",         "type": T},   # comma-separated BD people who touched this company
            {"name": "POCs Tapped",       "type": N, "options": {"precision": 0}},
            {"name": "Attempted POCs",    "type": N, "options": {"precision": 0}},
            {"name": "Connected POCs",    "type": N, "options": {"precision": 0}},
            {"name": "DCB POCs",          "type": N, "options": {"precision": 0}},
            {"name": "SQL POCs",          "type": N, "options": {"precision": 0}},
            {"name": "MQL POCs",          "type": N, "options": {"precision": 0}},
            {"name": "Activation POCs",   "type": N, "options": {"precision": 0}},
        ],
    }

    print("\n[Account Activity Log]")
    if "Account Activity Log" in tables:
        print("    ~ Already exists — checking for missing fields")
        aal_fields = list(ACCOUNT_ACTIVITY_TABLE["fields"])
        # Add Company linked record if Companies table exists
        if companies_id and "Company" not in field_names(tables["Account Activity Log"]):
            aal_fields.append({
                "name": "Company",
                "type": "multipleRecordLinks",
                "options": {"linkedTableId": companies_id},
            })
        add_missing(CRM_BASE, tables["Account Activity Log"],
                    [f for f in aal_fields if f["name"] != "Stat Key"])
    else:
        # Create table then add Company linked record
        result = create_table(CRM_BASE, ACCOUNT_ACTIVITY_TABLE)
        if result and companies_id:
            add_field(CRM_BASE, result["id"], {
                "name": "Company",
                "type": "multipleRecordLinks",
                "options": {"linkedTableId": companies_id},
            })

    print("\n=== Done ===")
    print("""
How to use the BD Dashboard:
─────────────────────────────────────────────────────────
1. BD Targets table — fill one row per (Owner × Metric):
     Metric        : Attempted / Connected / MQL / Discovery Call / SQL
     Owner         : Team member name (match exactly)
     Daily Target  : Your daily goal (e.g. 20)
     Effective From: When this target starts
   Formula fields auto-calculate: Window 1/2 targets = Daily/4,
   Weekly = Daily×6, Monthly = Daily×26.

2. BD Activity Log — team fills daily (per window per metric):
     Date    : Today's date
     Owner   : Your name
     Metric  : Which KPI
     Window  : 11:00 - 13:00 or 15:00 - 18:00
     Achieved: How many you hit in that window

3. Airtable Interface Designer — create a "BD Dashboard" interface:
     - Grouped bar chart: BD Activity Log grouped by Metric
     - Summary numbers: SUM(Achieved) per Owner per day
     - Target comparison: link to BD Targets for the matching row

4. Accounts Dashboard — in Companies table:
     - Add a grouped view: Group by "Source of Data"
     - Count shows how many accounts per source
     - Click a group to drill into individual accounts
""")


if __name__ == "__main__":
    main()
