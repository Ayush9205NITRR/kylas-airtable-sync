"""
Assign Companies from Airtable → Kylas

Reads an Airtable view (from the CRM Companies base by default).
For each company record in that view:
  1. Reads "Owner Email" — the target owner
  2. Looks up the Kylas user ID for that email
  3. Updates the company owner in Kylas
  4. Fetches all contacts linked to that company in Kylas
  5. Updates each contact's owner to the same user

Usage:
  python modules/07_assign_owner.py --view "To Assign"
  python modules/07_assign_owner.py --view "To Assign" --base list
  python modules/07_assign_owner.py --view "To Assign" --dry-run

Arguments:
  --view      Airtable view name to read from (required)
  --base      crm (default) | list   — which Airtable base to read
  --dry-run   Show what would happen without writing to Kylas
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.kylas_client import KylasClient
from utils.airtable_client import AirtableClient
from utils.redact import mask_email


def _records_from_view(table_name: str, base_id: str, view: str) -> list:
    """Fetch all records from a specific Airtable view."""
    client = AirtableClient(table_name, base_id=base_id)
    return client.table.all(view=view)


def run(view: str, base: str = "crm", dry_run: bool = False) -> dict:
    crm_base  = os.environ.get("AIRTABLE_COMPANY_BASE_ID", "")
    list_base = os.environ.get("AIRTABLE_BASE_ID", "")

    if base == "crm":
        table_name = "Companies"
        base_id    = crm_base
        id_field   = "Kylas Company Id"
        email_field = "Owner Email"
        name_field  = "Company Name"
    else:
        table_name  = "Company List"
        base_id     = list_base
        id_field    = "Kylas Company Id"
        email_field = "Owner Email"
        name_field  = "Company Name - Kylas"

    if not base_id:
        print(f"[AssignOwner] ERROR: base_id not set for base='{base}'")
        return {"error": "missing base_id"}

    print(f"[AssignOwner] Reading view '{view}' from {table_name} ({base})")
    try:
        records = _records_from_view(table_name, base_id, view)
    except Exception as exc:
        print(f"[AssignOwner] ERROR reading Airtable view: {exc}")
        return {"error": str(exc)}

    if not records:
        print(f"[AssignOwner] No records found in view '{view}'")
        return {"companies": 0, "contacts": 0}

    print(f"[AssignOwner] {len(records)} companies in view")

    kylas        = KylasClient()
    email_to_uid = kylas.get_users_by_email()
    print(f"[AssignOwner] Kylas user map: {len(email_to_uid)} entries")

    co_ok = co_fail = ct_ok = ct_fail = 0

    for rec in records:
        fields      = rec.get("fields", {})
        kylas_co_id = str(fields.get(id_field, "")).strip()
        owner_email = str(fields.get(email_field, "")).strip().lower()
        co_name     = fields.get(name_field, kylas_co_id)

        if not kylas_co_id:
            print(f"  [SKIP] '{co_name}' — no Kylas Company Id")
            continue
        if not owner_email:
            print(f"  [SKIP] '{co_name}' — Owner Email is blank")
            continue

        user_id = email_to_uid.get(owner_email)
        if not user_id:
            # New user not yet in bulk list — try direct search
            user_id = kylas.find_user_id_by_email(owner_email)
            if user_id:
                email_to_uid[owner_email] = user_id  # cache for subsequent records
                print(f"  [INFO] Found {mask_email(owner_email)} via direct search → uid:{user_id}")
            else:
                print(f"  [SKIP] '{co_name}' — {mask_email(owner_email)} not found in Kylas (new user not yet active?)")
                continue

        if dry_run:
            print(f"  [DRY] '{co_name}' (co:{kylas_co_id}) → {mask_email(owner_email)} (uid:{user_id})")
            # Still fetch contacts to show count
            contacts = kylas.get_contacts_by_company(int(kylas_co_id))
            print(f"         would also update {len(contacts)} contact(s)")
            co_ok += 1
            ct_ok += len(contacts)
            continue

        # Update company owner
        ok = kylas.update_company_owner(int(kylas_co_id), user_id)
        if ok:
            co_ok += 1
            print(f"  [OK] Company '{co_name}' → {mask_email(owner_email)}")
        else:
            co_fail += 1
            print(f"  [FAIL] Company '{co_name}' owner update failed")
            continue

        # Update all contacts of this company
        contacts = kylas.get_contacts_by_company(int(kylas_co_id))
        print(f"         {len(contacts)} contact(s) to reassign")
        for ct in contacts:
            ct_id = ct.get("id")
            if not ct_id:
                continue
            ct_name = ct.get("name") or str(ct_id)
            ok = kylas.update_contact_owner(int(ct_id), user_id)
            if ok:
                ct_ok += 1
            else:
                ct_fail += 1
                print(f"         [FAIL] Contact '{ct_name}' ({ct_id})")

    suffix = " [DRY RUN]" if dry_run else ""
    print(f"\n[AssignOwner]{suffix} Companies: {co_ok} OK, {co_fail} failed")
    print(f"[AssignOwner]{suffix} Contacts:  {ct_ok} OK, {ct_fail} failed")
    return {"companies_ok": co_ok, "companies_fail": co_fail,
            "contacts_ok": ct_ok, "contacts_fail": ct_fail}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--view",    required=True, help="Airtable view name to read")
    parser.add_argument("--base",    default="crm", choices=["crm", "list"],
                        help="Which Airtable base: crm (default) or list")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would happen without updating Kylas")
    args = parser.parse_args()
    run(view=args.view, base=args.base, dry_run=args.dry_run)
