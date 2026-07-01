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
    python scripts/rollup_offsite_timeline.py --discover --company-id 12345

The company multi-select field must exist in Kylas before running rollup.
If it doesn't, create it first:
    python scripts/create_offsite_field.py --dry-run   (preview)
    python scripts/create_offsite_field.py             (create)
or manually in Kylas: Settings → Customization → Form Fields → Company →
add a multi-select picklist "Offsite Timeline (BD - New)".
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from utils.kylas_client import KylasClient


def _to_id_str(val) -> str:
    """Normalise any numeric/string company id to a plain integer string."""
    try:
        return str(int(float(str(val).strip())))
    except (ValueError, TypeError):
        return str(val).strip()


def _company_key_from_config(cfg: dict):
    """Return the single cf_key from the 'company' block of the picklist config.

    Returns the key string if exactly one key is present, else None.
    """
    company_cfg = cfg.get("company") or {}
    keys = list(company_cfg.keys())
    if len(keys) == 1:
        return keys[0]
    return None


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


def _discover(client: KylasClient, company_id: int, labels_str: str):
    """Read a company record and print customFieldValues + proposed config snippet."""
    print("=" * 60)
    print(f"DISCOVER — customFieldValues for company id={company_id}")
    print("=" * 60)

    body = client.get_company(company_id)
    cfv = body.get("customFieldValues") or {}

    if not cfv:
        print("  (no customFieldValues found on this record)")
        return

    print("\nAll customFieldValues (key -> value):")
    for k, v in cfv.items():
        print(f"  {k}: {v!r}")

    # Candidate fields: key contains "offsite" (case-insensitive) OR value is a list.
    labels = [s.strip() for s in labels_str.split(",") if s.strip()]
    candidates = [
        (k, v) for k, v in cfv.items()
        if "offsite" in str(k).lower() or isinstance(v, list)
    ]

    if not candidates:
        print("\nNo candidate fields found (no key containing 'offsite' and no list values).")
        return

    print(f"\nCandidate fields ({len(candidates)} found):")
    for cf_key, raw_val in candidates:
        ids = sorted(client._as_id_set(raw_val))
        print(f"\n  cfKey : {cf_key}")
        print(f"  value : {raw_val!r}")
        print(f"  ids   : {ids}")

        if not ids:
            print("  (no numeric ids found — cannot build mapping)")
            continue

        if len(ids) != len(labels):
            print(f"  WARNING: {len(ids)} option id(s) found but {len(labels)} label(s) provided.")
            print(f"    Provided labels : {labels}")
            print(f"    Adjust --labels to match the actual number of options.")

        # Zip ids (sorted ascending ~ creation order) to labels in order.
        pairs = list(zip(ids, labels))
        mapping = {lbl: oid for oid, lbl in pairs}

        print()
        print("  PROPOSED config/kylas_picklists.json entry (VERIFY the label->id order):")
        snippet = {"company": {cf_key: mapping}}
        print(json.dumps(snippet, indent=2))

    print()
    print("=" * 60)
    print("After verifying above, merge the 'company' block into config/kylas_picklists.json")
    print("and re-run the rollup.")
    print("=" * 60)


def run(view_name: str, dry_run: bool, company_field: str, contact_field: str,
        inspect: bool, company_cf_key_arg: str = None):
    load_dotenv()
    company_base = os.environ.get("AIRTABLE_COMPANY_BASE_ID") or os.environ["AIRTABLE_BASE_ID"]
    client = KylasClient()

    if inspect:
        _inspect(client, company_field, contact_field)
        return

    # Resolve company field key — three-level fallback chain.
    company_cf_key = None

    # 1. Explicit --company-cf-key argument.
    if company_cf_key_arg:
        company_cf_key = company_cf_key_arg
        print(f"INFO: using company cf_key from --company-cf-key: {company_cf_key}")

    # 2. API display-name resolution.
    if not company_cf_key:
        company_cf_key = client.cf_key_for_display("company", company_field)

    # 3. Single-key config fallback.
    if not company_cf_key:
        cfg = client._load_picklist_config()
        key_from_cfg = _company_key_from_config(cfg)
        if key_from_cfg:
            company_cf_key = key_from_cfg
            print(f"INFO: company cf_key resolved from config/kylas_picklists.json: {company_cf_key}")

    if not company_cf_key:
        print(f"ERROR: company field '{company_field}' not found in Kylas.")
        print("Create it first:")
        print("  python scripts/create_offsite_field.py --dry-run   (preview body)")
        print("  python scripts/create_offsite_field.py             (create field)")
        print("Or in Kylas UI: Settings -> Customization -> Form Fields -> Company")
        print(f"  -> add multi-select picklist '{company_field}'")
        print()
        print("Company fields aren't exposed via API on this tenant — set the field on one")
        print("company, then run:")
        print("  python scripts/rollup_offsite_timeline.py --discover --company-id <ID>")
        print("to learn its key/option-ids and add them to config/kylas_picklists.json.")
        sys.exit(1)

    # Resolve contact field key + build id->label map.
    contact_cf_key = client.cf_key_for_display("contact", contact_field) or "cfOffsiteTimeline"
    ct_defs        = client.get_custom_field_defs("contact")
    ct_labels      = dict((ct_defs.get(contact_cf_key) or {}).get("labels") or {})

    print(f"Company field  : {company_field!r} -> {company_cf_key}")
    print(f"Contact field  : {contact_field!r} -> {contact_cf_key}")
    print(f"Label map      : {ct_labels}")
    print()

    # Fetch all companies where "Last Called AT - Date" is filled.
    from pyairtable import Api as AirtableApi  # lazy — not needed in discover/inspect paths
    print("Reading Company List (filter: Last Called At (Contacts) not empty)...")
    api     = AirtableApi(os.environ["AIRTABLE_PAT"])
    table   = api.table(company_base, "Company List")
    records = table.all(formula="NOT({Last Called At (Contacts)} = '')")
    print(f"Found {len(records)} companies with Last Called At (Contacts) set{' (DRY RUN)' if dry_run else ''}\n")

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

        # Collect contact offsite-timeline labels for this company.
        contact_labels = set()
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
            # SINGLE_PICKLIST: raw may be a dict {'name': ..., 'id': ...} or an int id.
            if isinstance(raw, dict):
                lbl = raw.get("name")
                if lbl:
                    contact_labels.add(lbl)
            else:
                # Fallback: treat as numeric id and look up in label map.
                try:
                    oid = int(raw)
                    lbl = ct_labels.get(oid)
                    if lbl:
                        contact_labels.add(lbl)
                except (ValueError, TypeError):
                    pass

        # Fetch company's current cfOffsiteTimelineBdNew value to diff against.
        company_names: set = set()
        try:
            co_full = client.get_company(co_id)
            co_raw  = (co_full.get("customFieldValues") or {}).get(company_cf_key)
            # Multi-select: list of dicts like [{'name': 'Jul - Sep', 'id': 258139}] or None.
            if isinstance(co_raw, list):
                for item in co_raw:
                    if isinstance(item, dict):
                        n = item.get("name")
                        if n:
                            company_names.add(n)
        except Exception as exc:
            print(f"  [WARN] could not fetch company {co_id} from Kylas: {exc}")

        # Only propagate labels the company doesn't already have.
        to_add = contact_labels - company_names

        print(f"  '{co_name}' (id={co_id}) contact_labels={sorted(contact_labels)} "
              f"company_has={sorted(company_names)} diff={sorted(to_add)}")

        if not to_add:
            print(f"  [SKIP] '{co_name}' (id={co_id}) — no new labels to add")
            tallies["skipped"] += 1
            continue

        result = client.merge_company_multiselect(co_id, company_cf_key,
                                                  list(to_add), dry_run=dry_run)
        print(f"  '{co_name}' (id={co_id}) diff={sorted(to_add)} -> {result}")
        tallies[result] = tallies.get(result, 0) + 1

    print(f"\n{'[DRY RUN] ' if dry_run else ''}Done")
    print(f"Updated: {tallies['updated']}  Unchanged: {tallies['unchanged']}  "
          f"Failed: {tallies['failed']}  Skipped: {tallies['skipped']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Roll up contact Offsite Timeline -> company multi-select in Kylas."
    )
    parser.add_argument("--view", help="(unused, kept for backwards compat)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print intended changes without writing to Kylas")
    parser.add_argument("--inspect", action="store_true",
                        help="Print resolved keys + option maps then exit")
    parser.add_argument("--discover", action="store_true",
                        help="Read a company record and print customFieldValues to discover cf_key + option ids")
    parser.add_argument("--company-id", type=int,
                        help="Kylas company id to inspect (required for --discover)")
    parser.add_argument("--company-cf-key", default=None,
                        help="Override: explicit cf_key for the company multi-select field")
    parser.add_argument("--labels", default="Jan - Mar,Apr - Jun,Jul - Sep,Oct - Dec",
                        help="Comma-separated option labels in creation order (used by --discover)")
    parser.add_argument("--company-field", default="Offsite Timeline (BD - New)",
                        help="Display name of the company multi-select field in Kylas")
    parser.add_argument("--contact-field", default="Offsite Timeline",
                        help="Display name of the contact single-select field in Kylas")
    args = parser.parse_args()

    if args.discover:
        if not args.company_id:
            parser.error("--company-id is required with --discover")
        load_dotenv()
        client = KylasClient()
        _discover(client, args.company_id, args.labels)
        sys.exit(0)

    run(
        view_name=args.view,
        dry_run=args.dry_run,
        company_field=args.company_field,
        contact_field=args.contact_field,
        inspect=args.inspect,
        company_cf_key_arg=args.company_cf_key,
    )
