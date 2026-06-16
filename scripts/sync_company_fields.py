"""
Airtable → Kylas multi-field company sync.

Reads a named view from Company List, then for each row syncs the Airtable
columns listed in FIELD_MAP to the matching Kylas company (and its contacts
when the owner changes).

Configuration is at the top of this file — add rows to FIELD_MAP to sync
more fields, no code changes needed.

Usage:
    python scripts/sync_company_fields.py --view "BD Assignment" --dry-run
    python scripts/sync_company_fields.py --view "BD Assignment"
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pyairtable import Api as AirtableApi
from utils.kylas_client import KylasClient

# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURATION — edit this block to add / remove synced fields
# ──────────────────────────────────────────────────────────────────────────────

# Airtable column name  →  Kylas action / field name.
#
# Reserved action:
#   "owner"             — reassign company + all contacts via PUT /{entity}/{id}/owner
#
# Standard Kylas company fields (set at the top level of the PUT body):
#   "numberOfEmployees", "website", "description", "name", …
#
# Custom Kylas fields start with "cf" and live in customFieldValues:
#   "cfSourceOfData", "cfIndustryAt", "cfLinkedIn", …
FIELD_MAP = {
    "Owner - Kylas"           : "owner",
    "No. of Employees (kylas)": "numberOfEmployees",
    "Source - Concatenate"    : "cfSourceOfData",
    # Uncomment / add more lines here:
    # "Industry"  : "cfIndustryAt",
    # "LinkedIn"  : "cfLinkedIn",
    # "Website"   : "website",
}

# Airtable text  →  Kylas numberOfEmployees select option {id, name}.
# To find your tenant's option IDs run: python scripts/inspect_kylas.py
EMP_MAP = {
    "1-10"    : {"id": 241911, "name": "1-10"},
    "11-50"   : {"id": 241912, "name": "11-50"},
    "51-200"  : {"id": 241913, "name": "51-200"},
    "201-500" : {"id": 241914, "name": "201-500"},
    "501-1000": {"id": 241915, "name": "501-1000"},
    "1001+"   : {"id": 241916, "name": "1001+"},
}

# ──────────────────────────────────────────────────────────────────────────────


_READONLY_COMPANY = ("createdAt", "updatedAt", "updatedBy", "createdBy", "recordActions")


def _load_user_maps(config_path: str):
    """Returns (email_to_id, name_to_id) from config/team.json, keys lowercased."""
    with open(config_path) as f:
        cfg = json.load(f)
    users       = cfg.get("kylas_users", {})
    user_emails = cfg.get("kylas_user_emails", {})
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
    try:
        return str(int(float(str(val).strip())))
    except (ValueError, TypeError):
        return str(val).strip()


def _resolve_user_id(raw: str, email_to_id: dict, name_to_id: dict):
    raw = (raw or "").strip()
    if not raw:
        return None
    if "@" in raw:
        return email_to_id.get(raw.lower())
    return name_to_id.get(raw.lower())


def _resolve_value(raw, kylas_field: str):
    """
    Convert an Airtable raw value to what Kylas expects.
    Returns (kylas_value, skip_reason_or_None).
    skip_reason "empty" means silently skip (no warning needed).
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, "empty"

    if kylas_field == "numberOfEmployees":
        key = str(raw).strip()
        mapped = EMP_MAP.get(key)
        if not mapped:
            # Try case-insensitive match
            for k, v in EMP_MAP.items():
                if k.lower() == key.lower():
                    return v, None
            return None, f"'{raw}' not in EMP_MAP"
        return mapped, None

    # Linked records come back as a list; take first value
    if isinstance(raw, list):
        raw = raw[0] if raw else None
        if raw is None:
            return None, "empty"
    if isinstance(raw, dict):
        raw = raw.get("value") or raw.get("name") or str(raw)

    val = str(raw).strip()
    return (val, None) if val else (None, "empty")


def _apply_field(company: dict, kylas_field: str, value):
    """Write one field into a Kylas company dict (mutates in-place)."""
    if kylas_field.startswith("cf"):
        cfvs = company.setdefault("customFieldValues", [])
        for cf in cfvs:
            if cf.get("fieldName") == kylas_field:
                cf["value"] = value
                return
        cfvs.append({"fieldName": kylas_field, "value": value})
    else:
        company[kylas_field] = value


def run(view_name: str, dry_run: bool = False):
    company_base = os.environ.get("AIRTABLE_COMPANY_BASE_ID") or os.environ["AIRTABLE_BASE_ID"]
    api   = AirtableApi(os.environ["AIRTABLE_PAT"])
    table = api.table(company_base, "Company List")

    cfg_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "config", "team.json")
    email_to_id, name_to_id = _load_user_maps(cfg_path)

    client = KylasClient()
    try:
        live_map = client.get_users_by_email()
        before = len(email_to_id)
        for email, uid in live_map.items():
            email_to_id.setdefault(email, uid)
        print(f"Loaded {before} email→ID from team.json + {len(email_to_id) - before} from Kylas API")
    except Exception as e:
        print(f"[WARN] Could not fetch live Kylas users: {e}")
    print(f"Total: {len(email_to_id)} email→ID, {len(name_to_id)} name→ID\n")

    active = [f"{at!r} → {ky!r}" for at, ky in FIELD_MAP.items()]
    print("Syncing:\n  " + "\n  ".join(active))
    print(f"\nReading view '{view_name}' from Company List...")
    records = table.all(view=view_name)
    print(f"Found {len(records)} companies\n")
    if not records:
        return

    updated_co = updated_ct = skipped = failed = 0

    for rec in records:
        f         = rec["fields"]
        co_id_str = _to_id_str(f.get("Kylas Company Id", ""))
        co_name   = f.get("Company Name - Kylas") or f.get("Company Name") or co_id_str

        if not co_id_str:
            print(f"  [SKIP] '{co_name}' — no Kylas Company Id")
            skipped += 1
            continue

        co_id = int(co_id_str)

        # ── Collect what needs updating ───────────────────────────────────────
        owner_id      = None
        other_updates = {}  # kylas_field → ready value

        for at_field, kylas_field in FIELD_MAP.items():
            raw = f.get(at_field)

            if kylas_field == "owner":
                raw_str = (str(raw).strip() if raw is not None else "") \
                          or (f.get("Owner Email") or "").strip()
                uid = _resolve_user_id(raw_str, email_to_id, name_to_id)
                if not uid and "@" in raw_str:
                    uid = client.find_user_id_by_email(raw_str)
                    if uid:
                        email_to_id[raw_str.lower()] = uid
                if uid:
                    owner_id = uid
                elif raw_str:
                    print(f"  [WARN] '{co_name}' — owner '{raw_str}' not found in Kylas")
                continue

            kylas_val, skip_reason = _resolve_value(raw, kylas_field)
            if kylas_val is None:
                if skip_reason != "empty":
                    print(f"  [WARN] '{co_name}' field '{at_field}': {skip_reason}")
                continue
            other_updates[kylas_field] = kylas_val

        if not owner_id and not other_updates:
            skipped += 1
            continue

        print(f"  '{co_name}' (id:{co_id})")

        # ── Owner reassignment (company + contacts) ───────────────────────────
        if owner_id:
            if dry_run:
                print(f"    [DRY] PUT companies/{co_id}/owner  uid:{owner_id}")
                updated_co += 1
            else:
                if client.update_company_owner(co_id, owner_id):
                    updated_co += 1
                    contacts = client.get_contacts_by_company(co_id)
                    print(f"    owner → uid:{owner_id}, {len(contacts)} contacts")
                    for ct in contacts:
                        ct_id = ct.get("id")
                        if not ct_id:
                            continue
                        cur = ct.get("ownerId") or (ct.get("ownedBy") or {}).get("id")
                        if str(cur) == str(owner_id):
                            updated_ct += 1
                            continue
                        if client.update_contact_owner(ct_id, owner_id, contact_data=ct):
                            updated_ct += 1
                        else:
                            failed += 1
                else:
                    print(f"    [ERROR] company owner update failed")
                    failed += 1

        # ── Field updates (GET full company → apply → PUT back) ──────────────
        if other_updates:
            if dry_run:
                for kf, kv in other_updates.items():
                    print(f"    [DRY] {kf} = {kv!r}")
            else:
                try:
                    company = client.get_company(co_id)
                    for kf, kv in other_updates.items():
                        _apply_field(company, kf, kv)
                    for ro in _READONLY_COMPANY:
                        company.pop(ro, None)
                    client._put(f"companies/{co_id}", company)
                    print(f"    updated: {list(other_updates.keys())}")
                except Exception as exc:
                    print(f"    [ERROR] field update: {exc}")
                    failed += 1

    print(f"\n{'[DRY RUN] ' if dry_run else ''}Done")
    print(f"Companies updated: {updated_co}  Contacts updated: {updated_ct}  "
          f"Skipped: {skipped}  Failed: {failed}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--view", required=True, help="Airtable view name")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    from dotenv import load_dotenv; load_dotenv()
    run(view_name=args.view, dry_run=args.dry_run)
