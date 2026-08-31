#!/usr/bin/env python3
"""
Create the Account Health history columns in Airtable (idempotent, run anytime).

modules/06_account_health.py writes these every run, but AirtableClient SKIPS
any field that does not exist -- silently, with a warning -- so without this the
history is computed and then thrown away. Running it twice is harmless: existing
columns are left exactly as they are, including their data.

Also adds the columns that were already being written but had no home:
"Account Pipeline Stage", "Last Called At (Contacts)" and "Needs Re-assign" were
all appearing as `WARNING: skipping field ...` in the run logs.

    python scripts/setup_health_history_columns.py            # create
    python scripts/setup_health_history_columns.py --dry-run  # report only

Needs AIRTABLE_PAT (with schema.bases:write), AIRTABLE_BASE_ID and
AIRTABLE_COMPANY_BASE_ID.
"""
import argparse
import os
import sys

import requests

META = "https://api.airtable.com/v0/meta/bases"

# (name, airtable field definition)
HISTORY_COLUMNS = [
    ("Health Baseline",     {"type": "singleLineText"}),
    ("Previous Health",     {"type": "singleLineText"}),
    ("Health Last Changed", {"type": "singleLineText"}),
    ("Health Change Count", {"type": "number", "options": {"precision": 0}}),
]

# Written by account health already, but missing from one or both bases.
BACKFILL_COLUMNS = [
    ("Account Pipeline Stage",   {"type": "singleLineText"}),
    ("Last Called At (Contacts)", {"type": "singleLineText"}),
    ("Needs Re-assign",          {"type": "checkbox",
                                  "options": {"icon": "check", "color": "greenBright"}}),
]

ALL_COLUMNS = HISTORY_COLUMNS + BACKFILL_COLUMNS


def _tables(base_id: str, headers: dict) -> list:
    r = requests.get(f"{META}/{base_id}/tables", headers=headers, timeout=30)
    r.raise_for_status()
    return r.json().get("tables", [])


def ensure_columns(base_id: str, table_name: str, headers: dict,
                   dry_run: bool = False) -> tuple:
    """Add any missing column to one table. Returns (added, skipped, failed)."""
    table = next((t for t in _tables(base_id, headers) if t["name"] == table_name), None)
    if table is None:
        print(f"  ! table {table_name!r} not found in base {base_id} — skipping")
        return 0, 0, 1

    existing = {f["name"] for f in table.get("fields", [])}
    added = skipped = failed = 0

    for name, defn in ALL_COLUMNS:
        if name in existing:
            print(f"    = {name!r} already exists")
            skipped += 1
            continue
        if dry_run:
            print(f"    + {name!r} WOULD be created ({defn['type']})")
            added += 1
            continue
        resp = requests.post(
            f"{META}/{base_id}/tables/{table['id']}/fields",
            json={"name": name, **defn}, headers=headers, timeout=30,
        )
        if resp.status_code in (200, 201):
            print(f"    + created {name!r} ({defn['type']})")
            added += 1
        else:
            print(f"    ! create {name!r} FAILED {resp.status_code}: {resp.text[:200]}")
            failed += 1
    return added, skipped, failed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be created, change nothing")
    args = ap.parse_args()

    pat = os.environ.get("AIRTABLE_PAT")
    if not pat:
        print("ERROR: AIRTABLE_PAT is not set")
        return 2
    headers = {"Authorization": f"Bearer {pat}", "Content-Type": "application/json"}

    crm_base     = os.environ.get("AIRTABLE_BASE_ID")
    company_base = os.environ.get("AIRTABLE_COMPANY_BASE_ID") or crm_base
    if not crm_base:
        print("ERROR: AIRTABLE_BASE_ID is not set")
        return 2

    # (base_id, table_name) — mirrors _write_table() in 06_account_health.py
    targets = [(company_base, "Company List"), (crm_base, "Companies")]

    total_a = total_s = total_f = 0
    for base_id, table_name in targets:
        print(f"\n{table_name} (base {base_id[:8]}…):")
        a, s, f = ensure_columns(base_id, table_name, headers, args.dry_run)
        total_a, total_s, total_f = total_a + a, total_s + s, total_f + f

    verb = "would create" if args.dry_run else "created"
    print(f"\nDone — {verb} {total_a}, already present {total_s}, failed {total_f}")
    return 1 if total_f else 0


if __name__ == "__main__":
    sys.exit(main())
