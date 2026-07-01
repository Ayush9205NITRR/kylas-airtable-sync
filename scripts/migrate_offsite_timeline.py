"""
One-time migration: copy each company's "Offsite Timeline" custom field
value into "Offsite Timeline (BD - New)" in Kylas.

The source field (cfOffsiteTimeline) stores a single-select dict like
  {'name': 'Jul - Sep', 'id': 257201}
The destination field (cfOffsiteTimelineBdNew) uses different option IDs,
discovered by scanning existing records. The mapping is hardcoded below.

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

SRC_KEY = "cfOffsiteTimeline"       # "Offsite Timeline"            — has the data
DST_KEY = "cfOffsiteTimelineBdNew"  # "Offsite Timeline (BD - New)" — needs to be filled

# Option IDs for the destination field, discovered from existing records.
# These differ from the source field's IDs even for the same quarter names.
DST_OPTIONS = {
    'Jan - Mar': 258137,
    'Apr - Jun': 258138,
    'Jul - Sep': 258139,
    'Oct - Dec': 258140,
}


def _scan_cf_keys(companies: list) -> set:
    """Collect every customFieldValues key seen across all company records."""
    keys = set()
    for co in companies:
        keys.update((co.get("customFieldValues") or {}).keys())
    return keys


def _src_name(src_val) -> str:
    """Extract the quarter name from a source field value (dict or string)."""
    if isinstance(src_val, dict):
        return src_val.get('name', '')
    return str(src_val) if src_val else ''


def _dst_matches(dst_val, name: str) -> bool:
    """True if the destination field already contains the given quarter name."""
    if isinstance(dst_val, dict):
        return dst_val.get('name') == name
    if isinstance(dst_val, list):
        return any(isinstance(v, dict) and v.get('name') == name for v in dst_val)
    return False


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

    if args.company_id:
        companies = [kylas.get_company(args.company_id)]
        print(f"Fetched company {args.company_id}")
    else:
        print("Fetching all companies from Kylas...")
        companies = kylas.get_companies()
        print(f"Fetched {len(companies)} companies")

    if args.list_fields:
        all_cf_keys = _scan_cf_keys(companies)
        print(f"\nAll custom field keys found across {len(companies)} companies:")
        for k in sorted(all_cf_keys):
            print(f"  {k}")
        sys.exit(0)

    print(f"\n  Source : {SRC_KEY}")
    print(f"  Dest   : {DST_KEY}\n")

    migrated = 0
    skipped_empty = 0
    skipped_match = 0
    skipped_unknown = 0
    failed = 0

    for co in companies:
        cid = co.get("id")
        name = co.get("name") or f"Company {cid}"
        cf = co.get("customFieldValues") or {}

        src_val = cf.get(SRC_KEY)
        if not src_val:
            skipped_empty += 1
            continue

        src_name = _src_name(src_val)
        if not src_name:
            skipped_empty += 1
            continue

        dst_option_id = DST_OPTIONS.get(src_name)
        if dst_option_id is None:
            print(f"  WARNING: {name} (ID {cid}): unrecognised value {src_name!r}, skipping")
            skipped_unknown += 1
            continue

        dst_val = cf.get(DST_KEY)
        if _dst_matches(dst_val, src_name):
            skipped_match += 1
            continue

        new_val = {'name': src_name, 'id': dst_option_id}
        print(f"  {name} (ID {cid}): {dst_val!r} -> {new_val!r}")
        if args.dry_run:
            continue

        ok = kylas.update_company_custom_field(cid, DST_KEY, new_val)
        if ok:
            migrated += 1
        else:
            failed += 1

    label = "[DRY RUN] " if args.dry_run else ""
    print(f"\n{label}Migrated: {migrated} | Already matching: {skipped_match} | "
          f"No source value: {skipped_empty} | Unknown value: {skipped_unknown} | Failed: {failed}")


if __name__ == "__main__":
    main()
