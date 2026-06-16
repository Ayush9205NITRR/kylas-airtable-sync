"""
Airtable → Kylas owner assignment.

Reads a named view from Company List (AIRTABLE_COMPANY_BASE_ID).
For each record that has a Kylas Company Id and an owner in the
'Owner - Kylas' field (an email OR a name; falls back to 'Owner Email'):
  1. Resolves owner → Kylas user ID (team.json + live Kylas user list)
  2. Updates the company owner in Kylas (GET full object, set ownedBy, PUT back)
  3. Looks up that company's contacts (from one bulk contact fetch)
  4. Updates each contact's owner to the same user

Usage:
    python scripts/assign_from_airtable.py --view "BD Assignment" --dry-run
    python scripts/assign_from_airtable.py --view "BD Assignment"
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pyairtable import Api as AirtableApi
from utils.kylas_client import KylasClient


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


def _resolve_user_id(raw: str, email_to_id: dict, name_to_id: dict):
    """raw may be an email or a full name; returns a Kylas user id or None."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if "@" in raw:
        return email_to_id.get(raw.lower())
    return name_to_id.get(raw.lower())





def run(view_name: str, dry_run: bool = False):
    company_base = os.environ.get("AIRTABLE_COMPANY_BASE_ID") or os.environ["AIRTABLE_BASE_ID"]
    api   = AirtableApi(os.environ["AIRTABLE_PAT"])
    table = api.table(company_base, "Company List")

    cfg_path    = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "config", "team.json")
    email_to_id, name_to_id = _load_user_maps(cfg_path)

    # Augment with live Kylas user list so users not in team.json (e.g. gurnoor@enout.in)
    # are still resolved correctly.
    client = KylasClient()
    try:
        live_map = client.get_users_by_email()  # {email_lower: uid}
        before = len(email_to_id)
        for email, uid in live_map.items():
            email_to_id.setdefault(email, uid)  # team.json takes priority
        print(f"Loaded {before} email→ID from team.json + {len(email_to_id) - before} extra from Kylas API")
    except Exception as e:
        print(f"[WARN] Could not fetch live Kylas users: {e}")
    print(f"Total: {len(email_to_id)} email→ID and {len(name_to_id)} name→ID mappings\n")

    print(f"Reading view '{view_name}' from Company List...")
    records = table.all(view=view_name)
    print(f"Found {len(records)} companies\n")
    if not records:
        return

    # Build a {company_id: [contacts]} map from a single bulk contact fetch.
    # The per-company filtered search (search/contact with a company.id rule)
    # returns 500 from Kylas, so we pull all contacts once via the same
    # endpoint the daily sync uses (jsonRule: null) and group them locally.
    target_ids = {str(r["fields"].get("Kylas Company Id", "")).strip()
                  for r in records if str(r["fields"].get("Kylas Company Id", "")).strip()}
    contacts_by_company: dict = {}
    try:
        all_contacts = client.get_contacts()
        for ct in all_contacts:
            co = ct.get("company")
            cid = (str(co.get("id")) if isinstance(co, dict)
                   else str(co) if isinstance(co, (int, float)) else "")
            if cid and cid in target_ids:
                contacts_by_company.setdefault(cid, []).append(ct)
        print(f"Fetched {len(all_contacts)} contacts; "
              f"{sum(len(v) for v in contacts_by_company.values())} linked to target companies\n")
    except Exception as e:
        print(f"[WARN] Could not bulk-fetch contacts: {e} — contacts will be skipped\n")

    assigned_co = assigned_ct = skipped = failed = 0

    for rec in records:
        f          = rec["fields"]
        co_id_str  = str(f.get("Kylas Company Id", "")).strip()
        # Owner source: 'Owner - Kylas' (email or name), falling back to 'Owner Email'.
        owner_raw  = (f.get("Owner - Kylas") or "").strip() or (f.get("Owner Email") or "").strip()
        co_name    = f.get("Company Name - Kylas") or f.get("Company Name") or co_id_str

        if not co_id_str:
            print(f"  [SKIP] '{co_name}' — no Kylas Company Id")
            skipped += 1
            continue
        user_id = _resolve_user_id(owner_raw, email_to_id, name_to_id)
        if not user_id:
            # Last-chance lookup directly from Kylas (handles brand-new users)
            if "@" in owner_raw:
                user_id = client.find_user_id_by_email(owner_raw)
                if user_id:
                    email_to_id[owner_raw.lower()] = user_id
                    print(f"  [INFO] '{owner_raw}' found via Kylas direct search → uid:{user_id}")
            if not user_id:
                print(f"  [SKIP] '{co_name}' — owner '{owner_raw}' not found in Kylas")
                skipped += 1
                continue

        co_id = int(co_id_str)
        print(f"  '{co_name}' → user {user_id} ({owner_raw})")

        if dry_run:
            print(f"    [DRY] PUT companies/{co_id}")
            assigned_co += 1
        else:
            if client.update_company_owner(co_id, user_id):
                assigned_co += 1
            else:
                failed += 1
                continue

        contacts = contacts_by_company.get(co_id_str, [])
        print(f"    → {len(contacts)} contacts")
        for ct in contacts:
            ct_id = ct.get("id")
            if not ct_id:
                continue
            if dry_run:
                assigned_ct += 1
            else:
                if client.update_contact_owner(ct_id, user_id):
                    assigned_ct += 1
                else:
                    failed += 1

    print(f"\n{'[DRY RUN] ' if dry_run else ''}Done")
    print(f"Companies: {assigned_co}  Contacts: {assigned_ct}  Skipped: {skipped}  Failed: {failed}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--view",    required=True, help="Airtable view name to read from")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    from dotenv import load_dotenv; load_dotenv()
    run(view_name=args.view, dry_run=args.dry_run)
