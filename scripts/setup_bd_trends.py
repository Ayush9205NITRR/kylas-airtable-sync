"""
One-shot / idempotent schema setup for the "BD Trends" Airtable table.

BD Trends holds one row per (grain x period x owner) — Day / Week / Month /
Quarter — aggregated from "BD Daily Stats" (see scripts/build_bd_trends.py).
It feeds an Airtable Interface dashboard, so unlike "BD Daily Stats" (90-day
retention) it needs to persist Week/Month/Quarter rows forever; only its Day
rows are pruned (see build_bd_trends.py).

Safe to re-run: creates the table if missing, otherwise adds any fields that
aren't there yet (existing fields/rows are left untouched).

Run:  python scripts/setup_bd_trends.py
Env:  AIRTABLE_PAT, AIRTABLE_BASE_ID
"""
import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()

PAT     = os.environ["AIRTABLE_PAT"]
BASE_ID = os.environ["AIRTABLE_BASE_ID"]
HEADERS = {"Authorization": f"Bearer {PAT}", "Content-Type": "application/json"}
META    = "https://api.airtable.com/v0/meta/bases"

TABLE_NAME = "BD Trends"

T  = "singleLineText"
N  = "number"
NP = {"precision": 0}
DATE_ISO = {"type": "date", "options": {"dateFormat": {"name": "iso"}}}

GRAINS = ["Day", "Week", "Month", "Quarter"]

# Same column names 05_bd_stats.py writes to BD Daily Stats — kept identical
# so build_bd_trends.py can copy a daily row's metrics straight across without
# re-mapping keys.
METRIC_FIELDS = ["Attempted", "Connected", "Discovery Calls", "MQL", "Activation", "SQL"]

BD_TRENDS_TABLE = {
    "name": TABLE_NAME,
    "description": (
        "One row per (grain x period x owner) rolled up from BD Daily Stats "
        "(Slot=full_day). Grain = Day/Week/Month/Quarter. Key = "
        "'<Grain>|<Period>|<Owner>'. Refreshed daily by "
        "scripts/build_bd_trends.py. Day rows older than 100 days are "
        "pruned; Week/Month/Quarter rows are kept forever. Feeds the BD "
        "Interface dashboard."
    ),
    "fields": [
        {"name": "Key", "type": T},   # primary field, upsert key = "<Grain>|<Period>|<Owner>"
        {"name": "Grain", "type": "singleSelect",
         "options": {"choices": [{"name": g} for g in GRAINS]}},
        {"name": "Period", "type": T},
        {"name": "Period Start", **DATE_ISO},
        {"name": "Owner", "type": T},
    ] + [{"name": f, "type": N, "options": NP} for f in METRIC_FIELDS],
}


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


def main():
    print("=== BD Trends Schema Setup ===\n")
    tables = get_tables(BASE_ID)

    print(f"[{TABLE_NAME}]")
    if TABLE_NAME in tables:
        print("    ~ Already exists — checking for missing fields")
        add_missing(BASE_ID, tables[TABLE_NAME],
                    [f for f in BD_TRENDS_TABLE["fields"] if f["name"] != "Key"])
    else:
        create_table(BASE_ID, BD_TRENDS_TABLE)

    print("\n=== Done ===")


if __name__ == "__main__":
    main()
