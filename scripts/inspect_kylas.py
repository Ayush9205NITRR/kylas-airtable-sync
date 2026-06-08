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


# ── Probe the NOTES read endpoint (v4) ─────────────────────────────────────────
# Kylas has TWO search patterns:
#   /search/{entity}   — lead/deal/contact/company   (tried: /search/note -> "Entity
#                        Definition does not exist", so notes aren't here)
#   /{entity}/search   — meetings, messages, pipelines (POST + pagination)
# Meetings relate to deals via relatedTo:[{id, entity:"deal"}]; notes are created
# via POST /notes/relation {targetEntityType:"DEAL", targetEntityId}. So the most
# likely LIST endpoint is POST /notes/search (mirroring /meetings/search). We probe
# that family with FULL response-body capture (repr) so the real 400 error shows.
print(f"\n{'='*60}\nDEAL NOTES — endpoint probe (v4, /notes/search family)\n{'='*60}")
deals = search("deal", fields=["id", "name", "updatedAt"], n=5)
if not deals:
    print("  (no deals to probe)")
else:
    did0 = deals[0]["id"]
    PAGE = {"page": 0, "size": 20, "sort": "createdAt,desc"}

    def dump(method, path, params=None, body=None):
        try:
            if method == "get":
                r = requests.get(f"{BASE}/{path}", params=params or {},
                                 headers=HEADERS, timeout=30)
            else:
                r = requests.post(f"{BASE}/{path}", params=params or {},
                                  json=body, headers=HEADERS, timeout=30)
            b = f" body={json.dumps(body)}" if body is not None else ""
            print(f"\n  [{method.upper()} /{path}] params={params}{b} -> {r.status_code}")
            print("    " + (repr(r.text[:600]) if r.text else "(empty body)"))
            return r
        except Exception as e:
            print(f"  [{method.upper()} /{path}] ERROR: {e}")
            return None

    print(f"\n  Probing with deal {did0} ({deals[0].get('name','')})")

    # ── Pattern: POST /notes/search (mirror /meetings/search) ───────────────────
    dump("post", "notes/search", PAGE, {})
    dump("post", "notes/search", PAGE, {"relatedTo": [{"id": did0, "entity": "deal"}]})
    dump("post", "notes/search", PAGE,
         {"jsonRule": {"condition": "AND", "rules": [
             {"id": "entityId", "field": "entityId",
              "operator": "equal", "value": str(did0)}]}})
    dump("get",  "notes/search", PAGE)

    # ── Pattern: GET /notes list, full error capture ────────────────────────────
    dump("get",  "notes", PAGE)
    dump("get",  "notes/relation", {"entityId": str(did0), "entityType": "DEAL"})
    dump("get",  "notes/relation", {"id": str(did0), "type": "deal"})

    # ── If POST /notes/search returned content, scan deals for one WITH notes ───
    print(f"\n  SCAN — POST /notes/search per deal, looking for non-empty notes")
    for d in deals:
        r = None
        try:
            r = requests.post(f"{BASE}/notes/search", params=PAGE,
                              json={"relatedTo": [{"id": d["id"], "entity": "deal"}]},
                              headers=HEADERS, timeout=30)
        except Exception as e:
            print(f"  deal {d['id']} ERROR: {e}")
            continue
        if r is not None and r.status_code == 200:
            try:
                data  = r.json()
                items = data.get("content") if isinstance(data, dict) else data
            except Exception:
                items = None
            if items:
                print(f"\n  ✓ deal {d['id']} ({d.get('name','')}) notes:")
                print("    " + json.dumps(data, default=str)[:1000])
                break
        time.sleep(0.2)
