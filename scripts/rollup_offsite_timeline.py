"""
Contact → Company "Offsite Timeline" rollup.

For each company in a given Airtable view, reads every linked contact's
Offsite Timeline (single-select) value, collects the union of labels, and
merges them into the company's "Offsite Timeline (BD - New)" multi-select field
in Kylas.

Usage:
    python scripts/rollup_offsite_timeline.py --inspect
    python scripts/rollup_offsite_timeline.py --view "Company List" --dry-run
    python scripts/rollup_offsite_timeline.py --view "Company List"

The company multi-select field must exist in Kylas before running rollup.
If it doesn't, create it first:
    python scripts/create_offsite_field.py --dry-run   (preview)
    python scripts/create_offsite_field.py             (create)
or manually in Kylas: Settings → Customization → Form Fields → Company →
add a multi-select picklist "Offsite Timeline (BD - New)".
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from pyairtable import Api as AirtableApi
from utils.kylas_client import KylasClient


def _to_id_str(val) -> str:
    """Normalise any numeric/string company id to a plain integer string."""
    try:
        return str(int(float(str(val).strip())))
    except (ValueError, TypeError):
        return str(val).strip()


def _inspect(client: KylasClient, company_field: str, contact_field: str):
    """Print resolved keys + option maps for both fields, then exit."""
    print("=" * 60)
    print("INSPECT — resolved field definitions")
    print("=" * 60)

    # Company multi-select field.
    co_key = client.cf_key_for_display("company", company_field)
    if co_key:
        defn = client.get_custom_field_defs("company").get(co_key, {})
        print(f"\nCompany field '{company_field}'")
        print(f"  cf_key     : {co_key}")
        print(f"  multiValue : {defn.get('multiValue')}")
        opts = defn.get("options") or {}
        print(f"  options (label -> id):")
        for lbl, oid in sorted(opts.items()):
            print(f"    {lbl!r:30s} -> {oid}")
    else:
        print(f"\nCompany field '{company_field}': NOT FOUND")
        print("  Create it first with: python scripts/create_offsite_field.py")

    # Contact single-select field.
    ct_key = client.cf_key_for_display("contact", contact_field) or "cfOffsiteTimeline"
    ct_defn = client.get_custom_field_defs("contact").get(ct_key, {})
    print(f"\nContact field '{contact_field}'")
    print(f"  cf_key : {ct_key}")
    labs = ct_defn.get("labels") or {}
    print(f"  labels (id -> label):")
    for oid, lbl in sorted(labs.items(), key=lambda kv: kv[0]):
        print(f"    {oid!r:10} -> {lbl!r}")


def run(view_name: str, dry_run: bool, company_field: str, contact_field: str,
        inspect: bool):
    load_dotenv()
    company_base = os.environ.get("AIRTABLE_COMPANY_BASE_ID") or os.environ["AIRTABLE_BASE_ID"]
    client = KylasClient()

    if inspect:
        _inspect(client, company_field, contact_field)
        return

    # Resolve company field key.
    company_cf_key = client.cf_key_for_display("company", company_field)
    if not company_cf_key:
        print(f"ERROR: company field '{company_field}' not found in Kylas.")
        print("Create it first:")
        print("  python scripts/create_offsite_field.py --dry-run   (preview body)")
        print("  python scripts/create_offsite_field.py             (create field)")
        print("Or in Kylas UI: Settings → Customization → Form Fields → Company")
        print(f"  → add multi-select picklist '{company_field}'")
        sys.exit(1)

    # Resolve contact field key + build id->label map.
    contact_cf_key = client.cf_key_for_display("contact", contact_field) or "cfOffsiteTimeline"
    ct_defs        = client.get_custom_field_defs("contact")
    ct_labels      = dict((ct_defs.get(contact_cf_key) or {}).get("labels") or {})

    print(f"Company field  : {company_field!r} -> {company_cf_key}")
    print(f"Contact field  : {contact_field!r} -> {contact_cf_key}")
    print(f"Label map      : {ct_labels}")
    print()

    # Read Airtable view.
    print(f"Reading view '{view_name}' from Company List...")
    api     = AirtableApi(os.environ["AIRTABLE_PAT"])
    table   = api.table(company_base, "Company List")
    records = table.all(view=view_name)
    print(f"Found {len(records)} companies{' (DRY RUN)' if dry_run else ''}\n")

    tallies = {"updated": 0, "unchanged": 0, "failed": 0, "skipped": 0}

    for rec in records:
        f         = rec["fields"]
        co_id_str = _to_id_str(f.get("Kylas Company Id", ""))
        co_name   = f.get("Company Name - Kylas") or f.get("Company Name") or co_id_str

        if not co_id_str:
            print(f"  [SKIP] '{co_name}' — no Kylas Company Id")
            tallies["skipped"] += 1
            continue

        co_id    = int(co_id_str)
        contacts = client.get_contacts_by_company(co_id)

        # Collect the union of contact offsite-timeline labels for this company.
        label_set = set()
        for ct in contacts:
            raw = (ct.get("customFieldValues") or {}).get(contact_cf_key)
            # Contacts may only have the key when fetched in full detail;
            # get_contacts_by_company fetches a limited field list, so we
            # fetch each contact's full record to read custom fields.
            if raw is None:
                try:
                    ct_full = client.get_contact(ct["id"])
                    raw = (ct_full.get("customFieldValues") or {}).get(contact_cf_key)
                except Exception:
                    pass
            if raw is None:
                continue
            # single-select: raw is an int id (or {"id":...}).
            if isinstance(raw, dict):
                raw = raw.get("id")
            try:
                oid = int(raw)
            except (TypeError, ValueError):
                continue
            lbl = ct_labels.get(oid)
            if lbl:
                label_set.add(lbl)

        if not label_set:
            print(f"  [SKIP] '{co_name}' (id={co_id}) — no contact offsite timeline values")
            tallies["skipped"] += 1
            continue

        result = client.merge_company_multiselect(co_id, company_cf_key,
                                                  list(label_set), dry_run=dry_run)
        print(f"  '{co_name}' (id={co_id}) labels={sorted(label_set)} -> {result}")
        tallies[result] = tallies.get(result, 0) + 1

    print(f"\n{'[DRY RUN] ' if dry_run else ''}Done")
    print(f"Updated: {tallies['updated']}  Unchanged: {tallies['unchanged']}  "
          f"Failed: {tallies['failed']}  Skipped: {tallies['skipped']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Roll up contact Offsite Timeline → company multi-select in Kylas."
    )
    parser.add_argument("--view", help="Airtable Company List view name (required unless --inspect)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print intended changes without writing to Kylas")
    parser.add_argument("--inspect", action="store_true",
                        help="Print resolved keys + option maps then exit")
    parser.add_argument("--company-field", default="Offsite Timeline (BD - New)",
                        help="Display name of the company multi-select field in Kylas")
    parser.add_argument("--contact-field", default="Offsite Timeline",
                        help="Display name of the contact single-select field in Kylas")
    args = parser.parse_args()

    if not args.inspect and not args.view:
        parser.error("--view is required unless --inspect is used")

    run(
        view_name=args.view,
        dry_run=args.dry_run,
        company_field=args.company_field,
        contact_field=args.contact_field,
        inspect=args.inspect,
    )
