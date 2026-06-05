"""Print all tables, field names, and field types for both Airtable bases."""
import os
import requests
from dotenv import load_dotenv

load_dotenv()

PAT = os.environ["AIRTABLE_PAT"]
BASES = []
company_base = os.environ.get("AIRTABLE_COMPANY_BASE_ID", "")
if company_base:
    BASES.append(("Company List", company_base))
else:
    print("INFO: AIRTABLE_COMPANY_BASE_ID not set — skipping Company Database base")
BASES.append(("Contacts/Deals/Sync Log", os.environ["AIRTABLE_BASE_ID"]))

def inspect(label: str, base_id: str):
    print(f"\n{'='*60}")
    print(f"BASE: {label}  ({base_id})")
    print(f"{'='*60}")
    r = requests.get(
        f"https://api.airtable.com/v0/meta/bases/{base_id}/tables",
        headers={"Authorization": f"Bearer {PAT}"},
        timeout=30,
    )
    r.raise_for_status()
    for table in r.json().get("tables", []):
        print(f"\n  TABLE: {table['name']}")
        print(f"  {'FIELD NAME':<40} TYPE")
        print(f"  {'-'*40} {'-'*20}")
        for field in table.get("fields", []):
            print(f"  {field['name']:<40} {field['type']}")

for label, base_id in BASES:
    inspect(label, base_id)

print("\nDone.")
