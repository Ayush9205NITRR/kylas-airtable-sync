"""
One-time migration: copy each company's "Offsite Timeline" custom field
value into "Offsite Timeline (BD - New)" in Kylas.

Both fields must already exist on the Company entity in Kylas — this script
only copies values, it does not create fields. Field keys are resolved at
runtime by display name (via GET /entities/company/fields), so no internal
key is hardcoded.

Writes go through a full GET /companies/{id} + PUT, changing ONLY the
destination custom field. Every other field on the record — including
ownedBy/owner — is left exactly as Kylas returned it.

Usage:
    python scripts/migrate_offsite_timeline.py --dry-run   # preview only
    python scripts/migrate_offsite_timeline.py             # apply
    python scripts/migrate_offsite_timeline.py --id 12345  # single company
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

from utils.kylas_client import KylasClient

SRC_LABEL = "Offsite Timeline"
DST_LABEL = "Offsite Timeline (BD - New)"


def _resolve_field_keys(kylas: KylasClient, debug: bool = False) -> dict:
    """Map company field display name (lowercased) -> customFieldValues key."""
    mapping = {}
    for fld in kylas.list_entity_fields("company"):
        # Kylas may use different keys depending on endpoint version
        label = (fld.get("displayName") or fld.get("display_name") or
                 fld.get("label") or fld.get("fieldLabel") or "").strip()
        key   = (fld.get("name") or fld.get("fieldName") or
                 fld.get("id") or fld.get("fieldId") or "").strip()
        if debug:
            print(f"    field keys={list(fld.keys())} label={label!r} key={key!r}")
        if label and key:
            mapping[label.lower()] = key
    return mapping


def main():
    parser = argparse.ArgumentParser(
        description="Copy 'Offsite Timeline' -> 'Offsite Timeline (BD - New)' for all Kylas companies."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Print planned changes, write nothing")
    parser.add_argument("--id", type=int, dest="company_id",
                        help="Migrate a single company by Kylas ID")
    parser.add_argument("--list-fields", action="store_true",
                        help="Print all company field keys from Kylas and exit")
    args = parser.parse_args()

    kylas = KylasClient()

    print("Resolving custom field keys from Kylas...")
    field_map = _resolve_field_keys(kylas, debug=args.list_fields)

    if args.list_fields:
        print(f"\nAll {len(field_map)} company fields found:")
        for label, key in sorted(field_map.items()):
            print(f"  {label!r:50} -> {key}")
        sys.exit(0)

    src_key = field_map.get(SRC_LABEL.lower())
    dst_key = field_map.get(DST_LABEL.lower())

    print(f"  {SRC_LABEL!r:35} -> {src_key or '(NOT FOUND)'}")
    print(f"  {DST_LABEL!r:35} -> {dst_key or '(NOT FOUND)'}\n")

    if not src_key or not dst_key:
        print("ERROR: could not resolve one or both field keys.")
        print("Run with --list-fields to see all available field labels.")
        sys.exit(1)

    if args.company_id:
        companies = [kylas.get_company(args.company_id)]
        print(f"Fetched company {args.company_id}")
    else:
        print("Fetching all companies from Kylas...")
        companies = kylas.get_companies()
        print(f"Fetched {len(companies)} companies")

    migrated = 0
    skipped_empty = 0
    skipped_match = 0
    failed = 0

    for co in companies:
        cid = co.get("id")
        name = co.get("name") or f"Company {cid}"
        cf = co.get("customFieldValues") or {}
        src_val = cf.get(src_key)
        if src_val in (None, ""):
            skipped_empty += 1
            continue
        dst_val = cf.get(dst_key)
        if dst_val == src_val:
            skipped_match += 1
            continue

        print(f"  {name} (ID {cid}): {dst_val!r} -> {src_val!r}")
        if args.dry_run:
            continue

        ok = kylas.update_company_custom_field(cid, dst_key, src_val)
        if ok:
            migrated += 1
        else:
            failed += 1

    label = "[DRY RUN] " if args.dry_run else ""
    print(f"\n{label}Migrated: {migrated} | Already matching: {skipped_match} | "
          f"No source value: {skipped_empty} | Failed: {failed}")


if __name__ == "__main__":
    main()
