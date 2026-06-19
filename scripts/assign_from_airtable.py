"""
Airtable → Kylas: owner assignment + arbitrary field push.

Reads a named view from Company List (AIRTABLE_COMPANY_BASE_ID). For each
record that has a Kylas Company Id it can do two things (independently):

  OWNER (mode owner|both):
    1. Resolve 'Owner - Kylas' (email or name; falls back to 'Owner Email')
       → Kylas user ID
    2. Update the company owner in Kylas
    3. Update every associated contact's owner to the same user

  FIELDS (mode fields|both):
    Reads the 'Kylas Field Map' table (the mapping "UI" — edit it in Airtable)
    and pushes mapped values from the company's Company List row:
      - Entity=Company  → onto the company itself
      - Entity=Contact  → onto EVERY associated contact (same value for all)
    Keys named cfXxx are written as Kylas custom fields.

  INSPECT (--inspect):
    Read-only. Prints one sample company's and contact's writable field keys
    (standard + custom cf*) with current values, so you know exactly what to
    put in the 'Kylas Field' column of the mapping table.

Usage:
    python scripts/assign_from_airtable.py --inspect
    python scripts/assign_from_airtable.py --view "BD Assignment" --mode fields --dry-run
    python scripts/assign_from_airtable.py --view "BD Assignment" --mode both
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pyairtable import Api as AirtableApi
from utils.kylas_client import KylasClient

FIELD_MAP_TABLE = "Kylas Field Map"


def _load_user_maps(config_path: str):
    """Returns (email_to_id, name_to_id) from config/team.json, keys lowercased."""
    with open(config_path) as f:
        cfg = json.load(f)
    users       = cfg.get("kylas_users", {})        # "74757" → "Bhaumik Sachdeva"
    user_emails = cfg.get("kylas_user_emails", {})  # "Bhaumik Sachdeva" → "bhaumik@enout.in"
    email_to_id, name_to_id = {}, {}
    for uid_str, name in users.items():
        try:
            uid = int(uid_str)
        except (TypeError, ValueError):
            continue
        if name:
            name_to_id[name.strip().lower()] = uid
        email = user_emails.get(name, "")
        if email:
            email_to_id[email.strip().lower()] = uid
    return email_to_id, name_to_id


def _to_id_str(val) -> str:
    """Normalise any numeric/string company id to a plain integer string."""
    try:
        return str(int(float(str(val).strip())))
    except (ValueError, TypeError):
        return str(val).strip()


def _resolve_user_id(raw: str, email_to_id: dict, name_to_id: dict):
    """raw may be an email or a full name; returns a Kylas user id or None."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if "@" in raw:
        return email_to_id.get(raw.lower())
    return name_to_id.get(raw.lower())


def _load_field_map(company_base: str) -> dict:
    """Read the 'Kylas Field Map' table → {'company': [(col, key)], 'contact': [...]}.

    Only rows with Active checked (or no Active column) are used. Returns empty
    lists if the table is missing so the script still runs owner-only.
    """
    out = {"company": [], "contact": []}
    try:
        api  = AirtableApi(os.environ["AIRTABLE_PAT"])
        rows = api.table(company_base, FIELD_MAP_TABLE).all()
    except Exception as exc:
        print(f"[FieldMap] '{FIELD_MAP_TABLE}' not readable ({exc}) — no field push")
        return out
    for r in rows:
        f = r["fields"]
        if not f.get("Active", True):
            continue
        entity = str(f.get("Entity") or "").strip().lower()
        col    = str(f.get("Airtable Column") or "").strip()
        key    = str(f.get("Kylas Field") or "").strip()
        if entity in out and col and key:
            out[entity].append((col, key))
    print(f"[FieldMap] {len(out['company'])} company + {len(out['contact'])} contact mapping(s) active")
    return out


def _row_fields(mappings: list, row: dict) -> dict:
    """Build {kylas_key: value} from a Company List row, skipping blank values."""
    fields = {}
    for col, key in mappings:
        val = row.get(col)
        if val is None or (isinstance(val, str) and not val.strip()):
            continue
        fields[key] = val
    return fields


def _owner_column(mappings: list):
    """Airtable column mapped to ownedBy in the field map, or None.

    Lets an `ownedBy` mapping drive owner assignment even in --mode fields,
    instead of being silently dropped (owner isn't writable as a plain field).
    """
    for col, key in mappings:
        if key == "ownedBy":
            return col
    return None


def _inspect(client: KylasClient):
    """Print writable field keys of a sample company + contact (read-only)."""
    for entity in ("company", "contact"):
        print(f"\n{'='*60}\nSample {entity.upper()} fields (use these in 'Kylas Field')\n{'='*60}")
        sample = client.fetch_sample(entity)
        if not sample:
            print("  (could not fetch a sample record)")
            continue
        cfv = sample.get("customFieldValues") or {}
        print("  -- Standard fields --")
        for k in sorted(sample.keys()):
            if k == "customFieldValues":
                continue
            v = sample[k]
            preview = (str(v)[:60] + "…") if len(str(v)) > 60 else str(v)
            print(f"    {k}: {preview}")
        print("  -- Custom fields (write key exactly as shown, e.g. cfSourceOfData) --")
        # Fetch ALL defined custom fields so null-valued ones also appear.
        all_cf_keys = client.list_custom_field_keys(entity)
        shown = set()
        for k in sorted(all_cf_keys.keys()):
            display = all_cf_keys[k]
            v = cfv.get(k)
            preview = (str(v)[:60] + "…") if v is not None and len(str(v)) > 60 else str(v)
            suffix = f"  [{display}]" if display != k else ""
            print(f"    {k}: {preview}{suffix}")
            shown.add(k)
        # Also show any sample values not returned by the definitions endpoint.
        for k in sorted(cfv.keys()):
            if k not in shown:
                v = cfv[k]
                preview = (str(v)[:60] + "…") if len(str(v)) > 60 else str(v)
                print(f"    {k}: {preview}")


def run(view_name: str, mode: str = "owner", dry_run: bool = False, inspect: bool = False):
    company_base = os.environ.get("AIRTABLE_COMPANY_BASE_ID") or os.environ["AIRTABLE_BASE_ID"]
    client       = KylasClient()

    if inspect:
        _inspect(client)
        return

    do_owner  = mode in ("owner", "both")
    do_fields = mode in ("fields", "both")

    api   = AirtableApi(os.environ["AIRTABLE_PAT"])
    table = api.table(company_base, "Company List")

    field_map = _load_field_map(company_base) if do_fields else {"company": [], "contact": []}

    # Owner assignment runs in owner/both mode, and also in fields mode when the
    # field map maps a column to ownedBy — so that mapping actually takes effect
    # instead of being silently ignored.
    co_owner_col = _owner_column(field_map["company"])
    ct_owner_col = _owner_column(field_map["contact"])
    assign_co_owner = do_owner or bool(co_owner_col)
    assign_ct_owner = do_owner or bool(ct_owner_col)
    resolve_owner   = assign_co_owner or assign_ct_owner
    owner_col       = co_owner_col or ct_owner_col or "Owner - Kylas"

    email_to_id = name_to_id = {}
    if resolve_owner:
        cfg_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "config", "team.json")
        email_to_id, name_to_id = _load_user_maps(cfg_path)
        try:
            live_map = client.get_users_by_email()  # {email_lower: uid}
            before = len(email_to_id)
            for email, uid in live_map.items():
                email_to_id.setdefault(email, uid)  # team.json takes priority
            print(f"Loaded {before} email→ID from team.json + {len(email_to_id) - before} extra from Kylas API")
        except Exception as e:
            print(f"[WARN] Could not fetch live Kylas users: {e}")
        print(f"Total: {len(email_to_id)} email→ID and {len(name_to_id)} name→ID mappings"
              f"  (owner column: '{owner_col}')\n")

    print(f"Reading view '{view_name}' from Company List...")
    records = table.all(view=view_name)
    print(f"Found {len(records)} companies  (mode={mode}{', DRY RUN' if dry_run else ''})\n")
    if not records:
        return

    assigned_co = assigned_ct = 0
    co_fields_set = ct_fields_set = 0
    skipped = failed = 0

    for rec in records:
        f         = rec["fields"]
        co_id_str = _to_id_str(f.get("Kylas Company Id", ""))
        co_name   = f.get("Company Name - Kylas") or f.get("Company Name") or co_id_str

        if not co_id_str:
            print(f"  [SKIP] '{co_name}' — no Kylas Company Id")
            skipped += 1
            continue
        co_id = int(co_id_str)

        # Resolve owner up front (owner/both mode, or fields mode with ownedBy mapped)
        user_id = None
        if resolve_owner:
            owner_raw = (f.get(owner_col) or "").strip() or (f.get("Owner Email") or "").strip()
            user_id   = _resolve_user_id(owner_raw, email_to_id, name_to_id)
            if not user_id and "@" in owner_raw:
                user_id = client.find_user_id_by_email(owner_raw)
                if user_id:
                    email_to_id[owner_raw.lower()] = user_id
                    print(f"  [INFO] '{owner_raw}' found via Kylas direct search → uid:{user_id}")
            if not user_id:
                print(f"  [SKIP owner] '{co_name}' — owner '{owner_raw}' not found in Kylas")
                if not do_fields:
                    skipped += 1
                    continue

        print(f"  '{co_name}' (company {co_id})")

        # ---- Company-level updates ----
        # Fields FIRST, then owner: a field PUT resets the owner to the API
        # user, so owner assignment must run last to have the final say.
        if do_fields and field_map["company"]:
            cfields = _row_fields(field_map["company"], f)
            cfields.pop("ownedBy", None)   # owner is set via assignment, not the field PUT
            if cfields:
                res = client.update_company_fields(co_id, cfields, dry_run=dry_run)
                print(f"    company fields {list(cfields)} → {res}")
                if res == "updated":
                    co_fields_set += 1
                elif res == "failed":
                    failed += 1

        if assign_co_owner and user_id:
            if dry_run:
                print(f"    [DRY] set owner → {owner_raw} (uid {user_id})")
                assigned_co += 1
            elif client.update_company_owner(co_id, user_id):
                print(f"    owner → {owner_raw} (uid {user_id})")
                assigned_co += 1
            else:
                print(f"    [WARN] owner NOT set → {owner_raw} (uid {user_id})")
                failed += 1

        # ---- Contact-level updates (fetch contacts once, reuse) ----
        need_contacts = (assign_ct_owner and user_id) or (do_fields and field_map["contact"])
        if not need_contacts:
            continue

        contacts      = client.get_contacts_by_company(co_id)
        contact_vals  = _row_fields(field_map["contact"], f) if do_fields else {}
        contact_vals.pop("ownedBy", None)   # owner is set via assignment, not the field PUT
        print(f"    → {len(contacts)} contacts"
              + (f"  | contact fields {list(contact_vals)}" if contact_vals else ""))

        for ct in contacts:
            ct_id = ct.get("id")
            if not ct_id:
                continue

            # Fields FIRST, then owner (a field PUT resets the owner).
            if contact_vals:
                res = client.update_contact_fields(ct_id, contact_vals,
                                                   contact_data=ct, dry_run=dry_run)
                if res == "updated":
                    ct_fields_set += 1
                elif res == "failed":
                    failed += 1

            if assign_ct_owner and user_id:
                already = ct.get("ownerId") == user_id or (
                    isinstance(ct.get("ownedBy"), dict) and ct["ownedBy"].get("id") == user_id
                )
                if already:
                    assigned_ct += 1
                elif dry_run:
                    assigned_ct += 1
                elif client.update_contact_owner(ct_id, user_id, contact_data=ct):
                    assigned_ct += 1
                else:
                    failed += 1

    # Surface real Kylas field names if no contact filter shape worked.
    if (assigned_co or co_fields_set) and not (assigned_ct or ct_fields_set) \
            and getattr(client, "_contact_method", None) is None:
        fields = client.list_contact_filter_fields()
        if fields:
            print(f"\n[DIAG] No contacts matched any filter shape. "
                  f"Kylas contact fields containing 'company': {fields}")

    print(f"\n{'[DRY RUN] ' if dry_run else ''}Done")
    print(f"Owner → companies: {assigned_co}  contacts: {assigned_ct}")
    print(f"Fields → companies: {co_fields_set}  contacts: {ct_fields_set}")
    print(f"Skipped: {skipped}  Failed: {failed}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--view", help="Airtable view name to read from (Company List table)")
    parser.add_argument("--mode", choices=["owner", "fields", "both"], default="owner",
                        help="owner = reassign owners (default); fields = push mapped fields; both")
    parser.add_argument("--inspect", action="store_true",
                        help="Read-only: print sample Kylas field keys, then exit")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    from dotenv import load_dotenv; load_dotenv()

    if not args.inspect and not args.view:
        parser.error("--view is required unless --inspect is used")
    run(view_name=args.view, mode=args.mode, dry_run=args.dry_run, inspect=args.inspect)
