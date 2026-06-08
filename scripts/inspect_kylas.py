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
time.sleep(0.5)


# ── Probe the notes endpoints for the most-recently-updated deal ───────────────
print(f"\n{'='*60}\nDEAL NOTES — endpoint probe\n{'='*60}")
deals = search("deal", fields=["id", "name", "updatedAt"], n=1)
if not deals:
    print("  (no deals to probe)")
else:
    did = deals[0]["id"]
    print(f"  Probing notes for deal {did} ({deals[0].get('name','')})")

    attempts = [
        ("get",  f"deals/{did}/notes",      None),
        ("get",  f"deals/{did}/activities", None),
        ("get",  f"deals/{did}/comments",   None),
        ("get",  f"deals/{did}/timeline",   None),
        ("get",  "notes",      {"entityType": "DEAL", "entityId": did}),
        ("get",  "notes",      {"entityType": "deal", "entityId": did}),
        ("get",  "activities", {"entityType": "DEAL", "entityId": did}),
        ("get",  "activities", {"entityType": "deal", "entityId": did}),
        ("post", "search/note",
         {"jsonRule": {"rules": [{"id": "entityId", "field": "entityId",
                                  "operator": "equal", "value": str(did)}]}}),
        ("post", "search/activity",
         {"jsonRule": {"rules": [{"id": "entityId", "field": "entityId",
                                  "operator": "equal", "value": str(did)}]}}),
        ("post", "search/note",
         {"jsonRule": {"rules": [{"id": "dealId", "field": "dealId",
                                  "operator": "equal", "value": str(did)}]}}),
    ]
    for method, path, payload in attempts:
        try:
            if method == "get":
                r = requests.get(f"{BASE}/{path}", params=payload or {},
                                 headers=HEADERS, timeout=30)
            else:
                r = requests.post(f"{BASE}/{path}",
                                  params={"page": 0, "size": 5, "sort": "createdAt,desc"},
                                  json=payload, headers=HEADERS, timeout=30)
            body_preview = json.dumps(r.json(), default=str)[:400] if r.ok else r.text[:200]
            print(f"\n  [{method.upper()} /{path}] params={payload} -> {r.status_code}")
            if r.status_code == 200:
                print("    " + body_preview)
        except Exception as e:
            print(f"  [{method.upper()} /{path}] ERROR: {e}")
        time.sleep(0.3)
