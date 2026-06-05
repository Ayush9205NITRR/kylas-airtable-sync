"""
Airtable → Kylas owner assignment.

Reads a named view from Company List (AIRTABLE_COMPANY_BASE_ID).
For each record that has both a Kylas Company Id and an Owner Email:
  1. Resolves email → Kylas user ID via config/team.json
  2. Updates the company owner in Kylas via PATCH
  3. Fetches all contacts of that company from Kylas
  4. Updates each contact's owner to the same user

Usage:
    python scripts/assign_from_airtable.py --view "BD Assignment"
    python scripts/assign_from_airtable.py --view "BD Assignment" --dry-run
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pyairtable import Api as AirtableApi
from utils.kylas_client import KylasClient

KYLAS_BASE = "https://api.kylas.io/v1"
PAGE_SIZE  = 200


def _load_email_to_id(config_path: str) -> dict:
    """Returns {lowercase_email: kylas_user_id} from config/team.json."""
    with open(config_path) as f:
        cfg = json.load(f)
    users       = cfg.get("kylas_users", {})        # "74757" → "Bhaumik Sachdeva"
    user_emails = cfg.get("kylas_user_emails", {})  # "Bhaumik Sachdeva" → "bhaumik@enout.in"
    email_to_id = {}
    for uid_str, name in users.items():
        email = user_emails.get(name, "")
        if email:
            email_to_id[email.lower()] = int(uid_str)
    return email_to_id


def _patch(client: KylasClient, path: str, data: dict) -> bool:
    time.sleep(0.15)
    try:
        r = client.session.patch(f"{KYLAS_BASE}/{path}", json=data, timeout=30)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"  [WARN] PATCH {path} failed: {e}")
        return False


def _contacts_for_company(client: KylasClient, company_id: int) -> list:
    records, page = [], 0
    while True:
        time.sleep(0.1)
        try:
            r = client.session.post(
                f"{KYLAS_BASE}/search/contact",
                params={"page": page, "size": PAGE_SIZE, "sort": "updatedAt,desc"},
                json={
                    "fields": ["id"],
                    "jsonRule": {
                        "condition": "AND",
                        "rules": [{
                            "id": "companyId", "field": "companyId",
                            "type": "integer", "operator": "equal",
                            "value": company_id,
                        }],
                    },
                },
                timeout=60,
            )
            r.raise_for_status()
            resp    = r.json()
            content = resp.get("content", [])
            records.extend(content)
            if page >= resp.get("totalPages", 1) - 1 or not content:
                break
            page += 1
        except Exception as e:
            print(f"  [WARN] contact search failed: {e}")
            break
    return records


def run(view_name: str, dry_run: bool = False):
    company_base = os.environ.get("AIRTABLE_COMPANY_BASE_ID") or os.environ["AIRTABLE_BASE_ID"]
    api   = AirtableApi(os.environ["AIRTABLE_PAT"])
    table = api.table(company_base, "Company List")

    cfg_path    = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "config", "team.json")
    email_to_id = _load_email_to_id(cfg_path)
    print(f"Loaded {len(email_to_id)} email→ID mappings\n")

    print(f"Reading view '{view_name}' from Company List...")
    records = table.all(view=view_name)
    print(f"Found {len(records)} companies\n")
    if not records:
        return

    client = KylasClient()
    assigned_co = assigned_ct = skipped = failed = 0

    for rec in records:
        f          = rec["fields"]
        co_id_str  = str(f.get("Kylas Company Id", "")).strip()
        owner_email = (f.get("Owner Email") or "").strip().lower()
        co_name     = f.get("Company Name - Kylas") or f.get("Company Name") or co_id_str

        if not co_id_str:
            print(f"  [SKIP] '{co_name}' — no Kylas Company Id")
            skipped += 1
            continue
        user_id = email_to_id.get(owner_email)
        if not user_id:
            print(f"  [SKIP] '{co_name}' — unknown owner email '{owner_email}'")
            skipped += 1
            continue

        co_id = int(co_id_str)
        print(f"  '{co_name}' → user {user_id} ({owner_email})")

        if dry_run:
            print(f"    [DRY] PATCH companies/{co_id}")
            assigned_co += 1
        else:
            if _patch(client, f"companies/{co_id}", {"ownedById": user_id}):
                assigned_co += 1
            else:
                failed += 1
                continue

        contacts = _contacts_for_company(client, co_id)
        print(f"    → {len(contacts)} contacts")
        for ct in contacts:
            ct_id = ct.get("id")
            if not ct_id:
                continue
            if dry_run:
                assigned_ct += 1
            else:
                if _patch(client, f"contacts/{ct_id}", {"ownedById": user_id}):
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
