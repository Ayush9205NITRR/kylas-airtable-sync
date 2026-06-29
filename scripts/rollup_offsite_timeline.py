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

    # ------------------------------------------------------------------
    # RAW ENDPOINT DIAGNOSTIC
    # Each probe is wrapped in try/except so inspect never crashes.
    # ------------------------------------------------------------------
    print()
    print("=" * 60)
    print("RAW ENDPOINT DIAGNOSTIC")
    print("=" * 60)

    def _summarise_field_list(items, limit=40):
        """Print compact field summary rows (up to limit), return count found."""
        count = 0
        for fld in items:
            if not isinstance(fld, dict):
                continue
            key = (fld.get("fieldName") or fld.get("apiName")
                   or fld.get("name") or fld.get("id") or "?")
            disp = fld.get("displayName") or fld.get("label") or ""
            ftype = fld.get("type") or fld.get("fieldType") or ""
            opts_raw = (fld.get("pickLists") or fld.get("picklists")
                        or fld.get("options") or fld.get("pickList") or [])
            print(f"    key={key}  display={disp!r}  type={ftype}  opts={len(opts_raw)}")
            count += 1
            if count >= limit:
                print(f"    ... (truncated at {limit})")
                break
        return count

    def _walk_layout(node, collected, limit=60):
        """Recursively collect field-object summaries from a layout response."""
        if len(collected) >= limit:
            return
        if isinstance(node, list):
            for item in node:
                _walk_layout(item, collected, limit)
        elif isinstance(node, dict):
            iname = node.get("internalName") or node.get("fieldName") or ""
            ftype = node.get("type") or ""
            if iname and ftype:
                disp = (node.get("displayName") or node.get("label")
                        or node.get("name") or iname)
                opts_raw = (node.get("pickLists") or node.get("picklists")
                            or node.get("options") or node.get("pickList") or [])
                multi = node.get("multiValue")
                collected.append(
                    f"    internalName={iname}  display={disp!r}  type={ftype}"
                    f"  opts={len(opts_raw)}  multiValue={multi}"
                )
            for v in node.values():
                if isinstance(v, (dict, list)):
                    _walk_layout(v, collected, limit)

    # Accumulate all field objects found across company + lead probes so we can
    # scan for "offsite"-like fields at the end.
    _all_probed_fields: list = []   # each entry: (entity, source_endpoint, fld_dict)

    for entity in ("company", "contact"):
        entity_upper = entity.upper()
        print()
        print(f"--- Entity: {entity} ---")

        # /entities/{entity}/fields with custom-only=true
        for co in ("true", "false"):
            label = f"entities/{entity}/fields?custom-only={co}&page=0&size=200"
            try:
                resp = client._get(f"entities/{entity}/fields", {
                    "entityType": entity, "custom-only": co, "page": 0, "size": 200,
                })
                items = resp if isinstance(resp, list) else (
                    resp.get("content") or resp.get("data") or [])
                print(f"  GET {label}")
                print(f"    container={type(resp).__name__}  field_objects={len(items)}")
                _summarise_field_list(items, limit=40)
                for fld in items:
                    _all_probed_fields.append((entity, label, fld))
            except Exception as exc:
                print(f"  GET {label}  ERROR: {exc}")

        # ui/layouts/CREATE/{ENTITY_UPPER}
        layout_path = f"ui/layouts/CREATE/{entity_upper}"
        try:
            resp = client._get(layout_path)
            collected: list = []
            _walk_layout(resp, collected, limit=60)
            print(f"  GET {layout_path}  -> ok, {len(collected)} field objects found")
            for line in collected[:60]:
                print(line)
            if len(collected) >= 60:
                print(f"    ... (truncated at 60)")
        except Exception as exc:
            print(f"  GET {layout_path}  ERROR: {exc}")

    # ------------------------------------------------------------------
    # Extended company layout probes: list endpoint + all layout types.
    # ------------------------------------------------------------------
    print()
    print("--- Company layout discovery ---")

    # ui/layouts/list/{company / COMPANY}
    for list_path in ("ui/layouts/list/company", "ui/layouts/list/COMPANY"):
        try:
            resp = client._get(list_path)
            rtype = type(resp).__name__
            layouts = resp if isinstance(resp, list) else (
                resp.get("content") or resp.get("data") or resp.get("layouts") or [])
            summary = []
            for lay in (layouts if isinstance(layouts, list) else []):
                if not isinstance(lay, dict):
                    continue
                entry = {k: lay.get(k) for k in ("id", "name", "layoutType", "recordType") if lay.get(k) is not None}
                summary.append(entry)
            print(f"  GET {list_path}  -> container={rtype}  layouts_found={len(summary)}")
            for s in summary[:20]:
                print(f"    {s}")
            if len(summary) > 20:
                print(f"    ... (truncated at 20)")
        except Exception as exc:
            print(f"  GET {list_path}  ERROR: {exc}")

    # Probe all layout types for COMPANY
    print()
    for ltype in ("CREATE", "EDIT", "DETAIL", "VIEW"):
        lpath = f"ui/layouts/{ltype}/COMPANY"
        try:
            resp = client._get(lpath)
            collected: list = []
            _walk_layout(resp, collected, limit=60)
            print(f"  GET {lpath}  -> ok, {len(collected)} field objects found")
            # Register discovered fields for the offsite scan below.
            for line in collected[:60]:
                print(line)
            if len(collected) >= 60:
                print(f"    ... (truncated at 60)")
        except Exception as exc:
            print(f"  GET {lpath}  ERROR: {exc}")

    # ------------------------------------------------------------------
    # LEAD probes: /entities/lead/fields + ui/layouts/CREATE/LEAD
    # ------------------------------------------------------------------
    print()
    print("--- Entity: lead ---")

    lead_fields_path = "entities/lead/fields"
    try:
        resp = client._get(lead_fields_path, {
            "custom-only": "true", "page": 0, "size": 200,
        })
        items = resp if isinstance(resp, list) else (
            resp.get("content") or resp.get("data") or [])
        print(f"  GET {lead_fields_path}?custom-only=true&size=200")
        print(f"    container={type(resp).__name__}  field_objects={len(items)}")
        _summarise_field_list(items, limit=40)
        for fld in items:
            _all_probed_fields.append(("lead", lead_fields_path, fld))
    except Exception as exc:
        print(f"  GET {lead_fields_path}  ERROR: {exc}")

    lead_layout_path = "ui/layouts/CREATE/LEAD"
    try:
        resp = client._get(lead_layout_path)
        collected: list = []
        _walk_layout(resp, collected, limit=60)
        print(f"  GET {lead_layout_path}  -> ok, {len(collected)} field objects found")
        for line in collected[:60]:
            print(line)
        if len(collected) >= 60:
            print(f"    ... (truncated at 60)")
    except Exception as exc:
        print(f"  GET {lead_layout_path}  ERROR: {exc}")

    # ------------------------------------------------------------------
    # Scan ALL probed field objects for anything "offsite"-like.
    # ------------------------------------------------------------------
    print()
    print("--- Offsite field scan (all probed entities + endpoints) ---")
    found_any = False
    for (ent, src, fld) in _all_probed_fields:
        if not isinstance(fld, dict):
            continue
        key  = (fld.get("fieldName") or fld.get("apiName")
                or fld.get("name") or fld.get("internalName") or "")
        disp = fld.get("displayName") or fld.get("label") or ""
        if "offsite" in str(key).lower() or "offsite" in str(disp).lower():
            ftype    = fld.get("type") or fld.get("fieldType") or ""
            opts_raw = (fld.get("pickLists") or fld.get("picklists")
                        or fld.get("options") or fld.get("pickList") or [])
            opts_str = str(opts_raw)[:120]
            print(f"  FOUND offsite-like field: entity={ent} source={src}"
                  f" key={key!r} display={disp!r} type={ftype!r} opts={opts_str}")
            found_any = True
    if not found_any:
        print("  (none found — field may not exist on company or lead)")


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
            # MULTI_PICKLIST: raw may be a list of ints or {"id":...} dicts.
            for oid in client._as_id_set(raw):
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
