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


def get_records(base_id, table_id):
    recs, offset = [], None
    while True:
        params = {"offset": offset} if offset else {}
        r = requests.get(f"https://api.airtable.com/v0/{base_id}/{table_id}",
                         headers=HEADERS, params=params, timeout=30)
        if r.status_code != 200:
            break
        data = r.json()
        recs.extend(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            break
    return recs


def ensure_rows(base_id, table_id, rows, key_field):
    """Add only rows whose key_field value is missing. Never overwrites user edits."""
    existing = {str(rec["fields"].get(key_field, "")).strip().lower()
                for rec in get_records(base_id, table_id)}
    for row in rows:
        kv = str(row.get(key_field, "")).strip().lower()
        if kv in existing:
            print(f"    ~ {row.get(key_field)} (row exists)")
            continue
        rr = requests.post(f"https://api.airtable.com/v0/{base_id}/{table_id}",
                           json={"fields": row}, headers=HEADERS, timeout=10)
        if rr.status_code in (200, 201):
            print(f"    + {row.get(key_field)} (row added)")
        else:
            print(f"    ! {row.get(key_field)} FAILED {rr.status_code}: {rr.text[:120]}")
        time.sleep(0.2)


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
        "options": {"formula": "ROUND({Daily Target}*5.5, 0)"},
    },
    {
        "name": "Monthly Target",
        "type": "formula",
        "options": {"formula": "ROUND({Daily Target}*22, 0)"},
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
        {"name": "Date Parsed", "type": "formula", "options": {"formula": "DATETIME_PARSE({Date})"}},
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
            {"name": "Date Parsed", "type": "formula", "options": {"formula": "DATETIME_PARSE({Date})"}},
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

    # ── BD Config (target management without touching code) ───────────────────
    BD_CONFIG_TABLE = {
        "name": "BD Config",
        "fields": [
            {"name": "Key",         "type": T},   # e.g. daily_attempted
            {"name": "Value",       "type": N, "options": {"precision": 0}},
            {"name": "Description", "type": T},
        ],
    }
    BD_CONFIG_ROWS = [
        {"Key": "daily_attempted",   "Value": 100, "Description": "Daily attempted calls per person"},
        {"Key": "daily_connected",   "Value":  35, "Description": "Daily connected calls per person"},
        {"Key": "daily_dcb",         "Value":   0, "Description": "Daily discovery calls target (0 = no daily target)"},
        {"Key": "daily_sql",         "Value":   0, "Description": "Daily SQL target (0 = no daily target)"},
        {"Key": "monthly_fixed_dcb", "Value":  10, "Description": "Monthly Discovery Calls goal"},
        {"Key": "monthly_fixed_sql", "Value":   6, "Description": "Monthly SQL goal"},
    ]
    print("\n[BD Config]")
    if "BD Config" in tables:
        cfg_id = tables["BD Config"]["id"]
        print("    ~ Already exists — ensuring default rows present")
    else:
        result = create_table(CRM_BASE, BD_CONFIG_TABLE)
        cfg_id = result["id"] if result else None
    if cfg_id:
        ensure_rows(CRM_BASE, cfg_id, BD_CONFIG_ROWS, "Key")

    # ── BD Members (email address management from Airtable) ───────────────────
    BD_MEMBERS_TABLE = {
        "name": "BD Members",
        "fields": [
            {"name": "Name",   "type": T},
            {"name": "Email",  "type": "email"},
            {
                "name": "Group",
                "type": "singleSelect",
                "options": {"choices": [{"name": "BD"}, {"name": "Revenue"}]},
            },
            {
                "name": "Active",
                "type": "checkbox",
                "options": {"icon": "check", "color": "greenBright"},
            },
        ],
    }
    BD_MEMBERS_ROWS = [
        {"Name": "Rubal",   "Email": "rubal@enout.in",          "Group": "BD", "Active": True},
        {"Name": "Bhaumik", "Email": "bhaumik@enout.in",        "Group": "BD", "Active": True},
        {"Name": "Shreya",  "Email": "shreya.bodwal@enout.in",  "Group": "BD", "Active": True},
        {"Name": "Mayra",   "Email": "mayra@enout.in",          "Group": "BD", "Active": True},
        {"Name": "Devansh", "Email": "devansh.shukla@enout.in", "Group": "BD", "Active": True},
        {"Name": "Tanay",   "Email": "tanay.kumar@enout.in",    "Group": "BD", "Active": True},
    ]
    print("\n[BD Members]")
    if "BD Members" in tables:
        mem_id = tables["BD Members"]["id"]
        print("    ~ Already exists — ensuring member rows present")
    else:
        result = create_table(CRM_BASE, BD_MEMBERS_TABLE)
        mem_id = result["id"] if result else None
    if mem_id:
        ensure_rows(CRM_BASE, mem_id, BD_MEMBERS_ROWS, "Name")

    # ── Email Templates (catalog of every automated email) ────────────────────
    EMAIL_TEMPLATES_TABLE = {
        "name": "Email Templates",
        "fields": [
            {"name": "Template",        "type": T},
            {"name": "When",            "type": T},
            {"name": "Recipients",      "type": T},
            {"name": "Subject Format",  "type": T},
            {"name": "Description",     "type": ML},
            {
                "name": "Active",
                "type": "checkbox",
                "options": {"icon": "check", "color": "greenBright"},
            },
        ],
    }
    EMAIL_TEMPLATES_ROWS = [
        {"Template": "BD — 11 AM Window", "When": "1:30 PM IST (Mon-Sat)",
         "Recipients": "Each BD member (cc Ayush, Vedant)",
         "Subject Format": "BD | {Name} | {Month Day} | 11 AM Window",
         "Description": "Morning window numbers (11 AM-1 PM): Attempted / Connected / Discovery Calls / SQL vs Daily target + per-window target. Monthly DCB/SQL goal reminder."},
        {"Template": "BD — EOD", "When": "6:30 PM IST (Mon-Sat)",
         "Recipients": "Each BD member (cc Ayush, Vedant)",
         "Subject Format": "BD | {Name} | {Month Day} | EOD",
         "Description": "End-of-day: W1 (11-1) + W2 (3-6) + Total vs Daily target. Monthly DCB/SQL goal reminder."},
        {"Template": "BD — Weekly Report", "When": "Saturday 9 AM IST",
         "Recipients": "Each BD member (cc Ayush, Vedant)",
         "Subject Format": "BD Weekly | {dd Mon - dd Mon YYYY}",
         "Description": "Mon-Fri totals vs weekly target (daily x 5.5) with % achieved."},
        {"Template": "BD — Monthly Report", "When": "1st of month 9 AM IST",
         "Recipients": "Each BD member (cc Ayush, Vedant)",
         "Subject Format": "BD Monthly | {Month YYYY}",
         "Description": "Previous month totals vs monthly target (daily x 22) with % achieved."},
        {"Template": "Hot Pipeline Digest", "When": "6:30 PM IST daily (EOD sync)",
         "Recipients": "team.json hot_pipeline_to (Ayush, Vedant)",
         "Subject Format": "Hot Pipeline | {Month Day}",
         "Description": "Companies with a contact in Activation / Discovery Call Booked / MQL / SQL. Columns: Company | Source | Industry."},
        {"Template": "Deal Rotting Alert", "When": "9 AM IST daily (Mon-Sat)",
         "Recipients": "team.json deal_rot.recipients (Vipul, Akash, Keshav) + deal owner (cc)",
         "Subject Format": "Deal Rotting Alert | {Month Day} | {N} deal(s)",
         "Description": "Open deals with no stage change AND no new comment for 2+ days. Columns: Deal Name | Owner | Pipeline Stage | Idle (days) | Last Comment."},
    ]
    for row in EMAIL_TEMPLATES_ROWS:
        row["Active"] = True

    print("\n[Email Templates]")
    if "Email Templates" in tables:
        et_id = tables["Email Templates"]["id"]
        print("    ~ Already exists — ensuring template rows present")
    else:
        result = create_table(CRM_BASE, EMAIL_TEMPLATES_TABLE)
        et_id = result["id"] if result else None
    if et_id:
        ensure_rows(CRM_BASE, et_id, EMAIL_TEMPLATES_ROWS, "Template")

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
