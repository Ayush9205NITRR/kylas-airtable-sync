"""
One-shot Airtable schema setup for the Client Repository table.

Creates `Client Repository` in the Company Database base (or adds any missing
columns if it already exists). Run once before the first
`python modules/09_client_repo.py` run:

    python scripts/setup_client_repo.py            # create/patch
    python scripts/setup_client_repo.py --dry-run  # show what it would do

Single-select options for Venture Class / Funding Stage / Macro Industry are
read from config/venture_taxonomy.json, so the dropdowns always match the
taxonomy the classifier actually uses. Re-run this after you add a bucket to
the taxonomy — Airtable rejects writes of select values it does not know.
"""
import argparse
import json
import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import venture_classifier as vc

TABLE_NAME = "Client Repository"
META = "https://api.airtable.com/v0/meta/bases"

T  = "singleLineText"
ML = "multilineText"
N  = "number"
CB = "checkbox"
SS = "singleSelect"


def _headers():
    return {"Authorization": f"Bearer {os.environ['AIRTABLE_PAT']}",
            "Content-Type": "application/json"}


def _base_id():
    return os.environ.get("AIRTABLE_COMPANY_BASE_ID") or os.environ["AIRTABLE_BASE_ID"]


def _select(choices):
    return {"options": {"choices": [{"name": c} for c in choices]}}


def field_defs() -> list:
    """Column definitions, with select options sourced from the taxonomy."""
    tax = vc.load_taxonomy()

    venture_classes = list(tax["venture_classes"]["order"]) + [tax["venture_classes"]["fallback"]]
    stages = list(tax["funding_stages"]["order"]) + [vc.UNCLASSIFIED_STAGE]
    industries = list(tax["macro_industries"]["order"]) + [tax["macro_industries"]["fallback"]]

    return [
        {"name": "Kylas Company Id",  "type": T},
        {"name": "Client Name",       "type": T},
        {"name": "Venture Class",     "type": SS, **_select(venture_classes)},
        {"name": "Funded Venture",    "type": CB, "options": {"icon": "check", "color": "greenBright"}},
        {"name": "Funding Stage",     "type": SS, **_select(stages)},
        {"name": "Round Size (USD)",  "type": N,  "options": {"precision": 0}},
        {"name": "Round Size",        "type": T},
        {"name": "Investors",         "type": T},
        {"name": "Funding Date",      "type": T},
        {"name": "Employee Count",    "type": N,  "options": {"precision": 0}},
        {"name": "Macro Industry",    "type": SS, **_select(industries)},
        {"name": "Industry (Kylas)",  "type": T},
        {"name": "Account Status",    "type": T},
        {"name": "Owner",             "type": T},
        {"name": "Batch",             "type": T},
        {"name": "Source of Data",    "type": T},
        {"name": "Client Basis",      "type": T},
        {"name": "Venture Confidence", "type": SS, **_select(["High", "Medium", "Low", "None"])},
        {"name": "Venture Basis",     "type": T},
        {"name": "Industry Basis",    "type": T},
        {"name": "Notes",             "type": ML},
        {"name": "Classified At",     "type": T},
    ]


def get_tables(base_id):
    r = requests.get(f"{META}/{base_id}/tables", headers=_headers(), timeout=30)
    r.raise_for_status()
    return {t["name"]: t for t in r.json().get("tables", [])}


def add_field(base_id, table_id, field):
    time.sleep(0.3)
    r = requests.post(f"{META}/{base_id}/tables/{table_id}/fields",
                      json=field, headers=_headers(), timeout=30)
    name = field["name"]
    if r.status_code in (200, 201):
        print(f"    + {name}")
    elif r.status_code == 422:
        print(f"    ~ {name} (already exists)")
    else:
        print(f"    ! {name} FAILED {r.status_code}: {r.text[:150]}")


def main():
    parser = argparse.ArgumentParser(description="Create/patch the Client Repository table")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    fields = field_defs()

    if args.dry_run:
        print(f"[DRY RUN] {TABLE_NAME} — {len(fields)} column(s):")
        for f in fields:
            choices = [c["name"] for c in f.get("options", {}).get("choices", [])]
            suffix = f"  ({len(choices)} options: {', '.join(choices[:4])}…)" if choices else ""
            print(f"  - {f['name']:<22} {f['type']}{suffix}")
        return

    # Imported here (not at module scope) so `field_defs()` and `--dry-run` stay
    # usable from tests without python-dotenv or any credentials present.
    from dotenv import load_dotenv
    load_dotenv()

    base_id = _base_id()
    tables = get_tables(base_id)

    if TABLE_NAME in tables:
        table = tables[TABLE_NAME]
        existing = {f["name"] for f in table.get("fields", [])}
        print(f"[setup] {TABLE_NAME} exists — adding missing columns")
        for f in fields:
            if f["name"] in existing:
                print(f"    ~ {f['name']} (already exists)")
            else:
                add_field(base_id, table["id"], f)
        return

    # Airtable uses the first field as the primary key; it cannot be a select
    # or checkbox, so Kylas Company Id leads.
    body = {"name": TABLE_NAME,
            "description": "Enout clients, split by venture class and macro industry. "
                           "Written by modules/09_client_repo.py.",
            "fields": fields}
    r = requests.post(f"{META}/{base_id}/tables", json=body, headers=_headers(), timeout=30)
    if r.status_code in (200, 201):
        print(f"[setup] + Created {TABLE_NAME} with {len(fields)} column(s)")
    else:
        print(f"[setup] ! Failed {r.status_code}: {r.text[:300]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
