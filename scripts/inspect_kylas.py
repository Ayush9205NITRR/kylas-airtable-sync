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


# ── Probe the NOTES read endpoint ──────────────────────────────────────────────
# From the official Kylas Postman collection, notes are CREATED via:
#     POST /v1/notes/relation
#     {"sourceEntity": {"description": "..."},
#      "targetEntityId": <id>, "targetEntityType": "DEAL"}
# The collection documents only create+delete (no list), so we empirically probe
# the realistic READ shapes using the correct param names ("targetEntityType" as
# the uppercase string "DEAL") and scan several deals to find one with notes.
print(f"\n{'='*60}\nDEAL NOTES — endpoint probe (v3, /notes/relation shapes)\n{'='*60}")
deals = search("deal", fields=["id", "name", "updatedAt"], n=25)
if not deals:
    print("  (no deals to probe)")
else:
    def candidates(did):
        return [
            ("get",  "notes/relation",
             {"targetEntityType": "DEAL", "targetEntityId": str(did)}),
            ("get",  "notes/relation",
             {"entityType": "DEAL", "entityId": str(did)}),
            ("get",  f"notes/relation/DEAL/{did}", None),
            ("get",  "notes",
             {"targetEntityType": "DEAL", "targetEntityId": str(did)}),
            ("get",  f"deals/{did}/notes/relation", None),
            ("post", "notes/relation/search",
             {"fields": [],
              "jsonRule": {"condition": "AND", "rules": [
                  {"id": "targetEntityId", "field": "targetEntityId",
                   "operator": "equal", "value": str(did)}]}}),
        ]

    # Step 1 — on the first deal, print status + body for every shape so we can
    # see which endpoint exists (200) vs 400/404.
    did0 = deals[0]["id"]
    print(f"\n  STEP 1 — endpoint shapes on deal {did0} ({deals[0].get('name','')})")
    for method, path, payload in candidates(did0):
        try:
            if method == "get":
                r = requests.get(f"{BASE}/{path}", params=payload or {},
                                 headers=HEADERS, timeout=30)
            else:
                r = requests.post(f"{BASE}/{path}",
                                  params={"page": 0, "size": 10, "sort": "createdAt,desc"},
                                  json=payload, headers=HEADERS, timeout=30)
            print(f"\n  [{method.upper()} /{path}] params={payload} -> {r.status_code}")
            try:
                body_preview = json.dumps(r.json(), default=str)[:600]
            except Exception:
                body_preview = r.text[:400]
            print("    " + (body_preview or "(empty body)"))
        except Exception as e:
            print(f"  [{method.upper()} /{path}] ERROR: {e}")
        time.sleep(0.3)

    # Step 2 — using the GET /notes/relation?targetEntityType=DEAL shape, scan up
    # to 25 deals and print the FIRST one that returns non-empty notes so we can
    # confirm the response/content shape (text + createdAt fields).
    print(f"\n  STEP 2 — scanning {len(deals)} deals for one WITH notes "
          f"(GET /notes/relation?targetEntityType=DEAL)")
    found = False
    for d in deals:
        did = d["id"]
        try:
            r = requests.get(f"{BASE}/notes/relation",
                             params={"targetEntityType": "DEAL", "targetEntityId": str(did)},
                             headers=HEADERS, timeout=30)
            if r.status_code != 200:
                continue
            data = r.json()
            items = data.get("content") if isinstance(data, dict) else data
            if items:
                print(f"\n  ✓ deal {did} ({d.get('name','')}) has notes:")
                print("    " + json.dumps(data, default=str)[:900])
                found = True
                break
        except Exception as e:
            print(f"  deal {did} ERROR: {e}")
        time.sleep(0.2)
    if not found:
        print("    (no deal among the 25 returned notes on this endpoint — "
              "either the shape is wrong or notes are not exposed via public API)")
