"""
One-shot schema setup for the Cold Call Analysis System.

Creates the `Calls` table (and adds any missing fields if it already exists) in
the cold-call Airtable base. Idempotent — safe to re-run.

Base id: COLD_CALL_AIRTABLE_BASE_ID (falls back to AIRTABLE_BASE_ID).
Token:   AIRTABLE_PAT (falls back to AIRTABLE_TOKEN).
"""
import os
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cold_call import config

PAT = config.AIRTABLE_TOKEN
BASE = config.AIRTABLE_BASE_ID
TABLE = config.TABLE_NAME
HEADERS = {"Authorization": f"Bearer {PAT}", "Content-Type": "application/json"}
META = "https://api.airtable.com/v0/meta/bases"

T = "singleLineText"
ML = "multilineText"
N = "number"
NP = {"precision": 0}
DATE = {"type": "date", "options": {"dateFormat": {"name": "iso"}}}
DATETIME = {"type": "dateTime", "options": {
    "dateFormat": {"name": "iso"},
    "timeFormat": {"name": "24hour"},
    "timeZone": "Asia/Kolkata",
}}


def _sel(*choices):
    return {"type": "singleSelect", "options": {"choices": [{"name": c} for c in choices]}}


# Non-formula fields. bd_name is first => Airtable makes it the primary field.
# The total_score formula is added afterwards so it can reference these by name.
FIELDS = [
    {"name": "bd_name", "type": T},
    {"name": "call_date", **DATE},
    {"name": "audio_filename", "type": T},
    {"name": "duration_seconds", "type": N, "options": NP},
    {"name": "transcript", "type": ML},
    {"name": "hook_score", "type": N, "options": NP},
    {"name": "hook_feedback", "type": ML},
    {"name": "hook_better_line", "type": ML},
    {"name": "objection_score", "type": N, "options": NP},
    {"name": "objections_list", "type": ML},
    {"name": "objection_feedback", "type": ML},
    {"name": "pitch_score", "type": N, "options": NP},
    {"name": "pitch_feedback", "type": ML},
    {"name": "pitch_better_version", "type": ML},
    {"name": "discovery_score", "type": N, "options": NP},
    {"name": "discovery_outcome", **_sel("booked", "agreed", "followup_promised", "no_next_step")},
    {"name": "discovery_feedback", "type": ML},
    {"name": "top_miss", "type": ML},
    {"name": "call_language", **_sel("hindi", "english", "hinglish")},
    {"name": "status", **_sel("processed", "too_short", "error")},
    {"name": "processed_at", **DATETIME},
]

FORMULA_FIELD = {
    "name": "total_score",
    "type": "formula",
    "options": {"formula": "{hook_score}+{objection_score}+{pitch_score}+{discovery_score}"},
}


def get_tables(base_id):
    r = requests.get(f"{META}/{base_id}/tables", headers=HEADERS, timeout=30)
    r.raise_for_status()
    return {t["name"]: t for t in r.json().get("tables", [])}


def field_names(table):
    return {f["name"] for f in table.get("fields", [])}


def add_field(base_id, table_id, field, formula=False):
    time.sleep(0.3)
    r = requests.post(f"{META}/{base_id}/tables/{table_id}/fields",
                      json=field, headers=HEADERS, timeout=30)
    name = field["name"]
    if r.status_code in (200, 201):
        print(f"    + {name}")
    elif r.status_code == 422 and formula:
        print(f"    ! Could not auto-create formula field '{name}'.")
        print("      Add it manually in Airtable as a Formula field:")
        print(f"        {field['options']['formula']}")
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
        print(f"    + Created table: {table_def['name']}")
        return r.json()
    print(f"    ! Failed to create {table_def['name']}: {r.status_code} {r.text[:200]}")
    return None


def main():
    if not PAT or not BASE:
        print("ERROR: set AIRTABLE_PAT and COLD_CALL_AIRTABLE_BASE_ID (or AIRTABLE_BASE_ID)")
        sys.exit(1)

    print(f"=== Cold Call schema setup → base {BASE}, table '{TABLE}' ===")
    tables = get_tables(BASE)

    if TABLE in tables:
        print(f"  ~ '{TABLE}' exists — adding any missing fields")
        add_missing(BASE, tables[TABLE], FIELDS)
        if "total_score" not in field_names(tables[TABLE]):
            add_field(BASE, tables[TABLE]["id"], FORMULA_FIELD, formula=True)
        else:
            print("    ~ total_score (already exists)")
    else:
        result = create_table(BASE, {"name": TABLE, "fields": FIELDS})
        if result:
            add_field(BASE, result["id"], FORMULA_FIELD, formula=True)

    print("=== Done ===")


if __name__ == "__main__":
    main()
