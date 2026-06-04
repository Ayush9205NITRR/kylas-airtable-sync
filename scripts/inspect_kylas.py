"""
Fetch 1 raw record per entity (no field filter) and print all fields.
Custom field values are printed in full (not truncated).
Also fetches contacts with explicit ownedBy to test owner resolution.
"""
import json
import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()

BASE = "https://api.kylas.io/v1"
HEADERS = {
    "api-key": os.environ["KYLAS_API_KEY"],
    "Content-Type": "application/json",
}


def search(entity: str, fields=None, n: int = 1) -> list:
    r = requests.post(
        f"{BASE}/search/{entity}",
        params={"page": 0, "size": n, "sort": "updatedAt,desc"},
        json={"fields": fields, "jsonRule": None},
        headers=HEADERS,
        timeout=60,
    )
    r.raise_for_status()
    return r.json().get("content", [])


def show(label: str, entity: str, fields=None):
    print(f"\n{'='*60}")
    print(f"ENTITY: {label}  (fields={fields!r})")
    print("=" * 60)
    records = search(entity, fields=fields, n=1)
    if not records:
        print("  (no records)")
        return
    rec = records[0]
    for key, value in rec.items():
        if key == "customFieldValues":
            print(f"\n  --- customFieldValues ---")
            if isinstance(value, dict):
                for cf_key, cf_val in value.items():
                    print(f"    {cf_key:<40} {json.dumps(cf_val, default=str)}")
            else:
                print(f"    {json.dumps(value, default=str)}")
            print(f"  --- end customFieldValues ---\n")
        else:
            s = json.dumps(value, default=str)
            print(f"  {key:<35} {s[:200]}")


# All fields unfiltered
show("Contact (all fields)", "contact", fields=None)
time.sleep(0.5)

# Contacts with explicit ownedBy to see if it returns as object
show("Contact (explicit ownedBy)", "contact",
     fields=["id", "name", "firstName", "lastName", "ownedBy", "emails",
             "phoneNumbers", "designation", "company", "linkedin",
             "city", "state", "country", "source", "customFieldValues",
             "createdAt", "updatedAt"])
time.sleep(0.5)

show("Company (all fields)", "company", fields=None)
time.sleep(0.5)

show("Deal (all fields)", "deal", fields=None)
