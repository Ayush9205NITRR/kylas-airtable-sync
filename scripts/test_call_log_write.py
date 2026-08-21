"""
ONE-SHOT: write a single test call log to Kylas, read it back, report attribution.

This is the only way to answer "can we identify who made the call?" — the tenant
has zero call logs, and the documented create payload has no caller field, so the
attribution key can only be observed on a record that actually exists.

THIS WRITES TO PRODUCTION. The Kylas public API documents no DELETE for call
logs (Create / Update / Fetch only), so the record it creates is likely
permanent and removable only through the Kylas UI. It therefore refuses to run
unless TEST_DEAL_ID names a deal the user explicitly nominated, and it labels
the record loudly so it is obvious in the UI.

SAFETY: repo is public, so Actions logs are public. Numeric ids and enums print
verbatim (needed to identify the attributed user); every human-readable name is
masked. The verdict line reports which KEY carries attribution and its id — map
the id to a person yourself.

Run: TEST_DEAL_ID=<deal id> python scripts/test_call_log_write.py
"""
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from dotenv import load_dotenv

load_dotenv()

BASE = "https://api.kylas.io/v1"
HEADERS = {"api-key": os.environ["KYLAS_API_KEY"], "Content-Type": "application/json"}

# Keys that identify a PERSON get masked; ids/enums print so we can trace them.
NAMEY = ("name", "firstname", "lastname", "email", "phone", "description")


def mask(key, val):
    if val is None or isinstance(val, (bool, int, float)):
        return val
    k = key.lower()
    if isinstance(val, str):
        if any(t in k for t in NAMEY):
            return f"<str len={len(val)}>"
        return val
    if isinstance(val, list):
        return [mask(key, v) for v in val[:3]]
    if isinstance(val, dict):
        return {kk: mask(kk, vv) for kk, vv in val.items()}
    return f"<{type(val).__name__}>"


def main():
    deal_id = (os.environ.get("TEST_DEAL_ID") or "").strip()
    if not deal_id:
        print("TEST_DEAL_ID is not set — refusing to write. Nothing done.")
        return 0

    print("=" * 70)
    print(f"CALL-LOG ATTRIBUTION TEST — deal {deal_id}")
    print("=" * 70)

    # 1. Confirm which deal we are about to touch.
    r = requests.get(f"{BASE}/deals/{deal_id}", headers=HEADERS, timeout=30)
    if r.status_code != 200:
        print(f"  GET /deals/{deal_id} -> HTTP {r.status_code} {r.text[:200]}")
        print("  Refusing to write against a deal that cannot be read.")
        return 1
    deal = r.json().get("data", r.json())
    stage = (deal.get("pipelineStage") or {})
    print(f"  deal id     : {deal.get('id')}")
    print(f"  deal name   : <masked, len={len(str(deal.get('name') or ''))}>")
    print(f"  stage       : {stage.get('name')!r} (id={stage.get('id')})")
    print(f"  ownedBy id  : {(deal.get('ownedBy') or {}).get('id')}")
    print(f"  createdBy id: {(deal.get('createdBy') or {}).get('id')}")

    # Pull an associated contact so the contact linkage is exercised too.
    assoc = deal.get("associatedContacts") or []
    contact_id, phone = None, None
    if isinstance(assoc, list) and assoc:
        c0 = assoc[0]
        contact_id = c0.get("id") if isinstance(c0, dict) else c0
    if contact_id:
        cr = requests.get(f"{BASE}/contacts/{contact_id}", headers=HEADERS, timeout=30)
        if cr.status_code == 200:
            cdata = cr.json().get("data", cr.json())
            pn = cdata.get("phoneNumbers") or []
            if pn and isinstance(pn[0], dict):
                phone = pn[0].get("value") or pn[0].get("number")
    phone = phone or "9999999999"
    print(f"  contact id  : {contact_id}   phone: ***{str(phone)[-3:]}")

    # 2. Write the test call log, labelled unmistakably.
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    payload = {
        "outcome": "connected",
        "callType": "outgoing",
        "startTime": now,
        "duration": "60",
        "phoneNumber": str(phone),
        "notes": [{"description":
                   "AUTOMATED API TEST — created to determine call-log "
                   "attribution. Not a real call. Safe to delete."}],
        "relatedTo": {"id": int(deal_id), "entity": "deal", "phoneNumber": str(phone)},
    }
    if contact_id:
        payload["associatedTo"] = [
            {"id": int(contact_id), "entity": "contact", "phoneNumber": str(phone)}]

    print("\n  POST /call-logs/ ...")
    w = requests.post(f"{BASE}/call-logs/", json=payload, headers=HEADERS, timeout=30)
    print(f"  -> HTTP {w.status_code}")
    if w.status_code >= 300:
        print(f"  body: {w.text[:600]}")
        print("\n  Write rejected — nothing was created.")
        return 1
    try:
        created = w.json()
        created = created.get("data", created)
        print("  created record:")
        print(json.dumps({k: mask(k, v) for k, v in created.items()},
                         indent=4, default=str)[:2500])
    except Exception:
        print(f"  (non-JSON response: {w.text[:300]})")
        created = {}

    # 3. Read it back the way a real integration would.
    print("\n  GET /call-logs/{deal}?relatedToType=deal ...")
    g = requests.get(f"{BASE}/call-logs/{deal_id}",
                     params={"relatedToType": "deal"}, headers=HEADERS, timeout=30)
    print(f"  -> HTTP {g.status_code}")
    rows = []
    if g.status_code == 200:
        b = g.json()
        rows = b if isinstance(b, list) else (b.get("content") or b.get("data") or [])
        print(f"  {len(rows)} row(s)")
        if rows:
            print(json.dumps({k: mask(k, v) for k, v in rows[0].items()},
                             indent=4, default=str)[:2500])
    else:
        print(f"  body: {g.text[:400]}")

    # 4. The verdict.
    rec = rows[0] if rows else created
    print("\n" + "=" * 70)
    print("ATTRIBUTION VERDICT")
    print("=" * 70)
    if not rec:
        print("  No readable record — inconclusive.")
        return 1
    hits = []
    for k, v in rec.items():
        if any(t in k.lower() for t in
               ("own", "creat", "updat", "user", "by", "agent", "caller")):
            ident = v.get("id") if isinstance(v, dict) else v
            hits.append((k, ident))
    if hits:
        for k, ident in hits:
            print(f"  {k:20s} -> id={ident}")
        print("\n  Map those ids to people. If they all resolve to the API key's")
        print("  own user rather than the actual rep, per-BD attribution is NOT")
        print("  possible with a single tenant key.")
    else:
        print("  No attribution-shaped key on the record at all. Kylas does not")
        print("  expose who created a call log — attribution must live in notes[].")
    return 0


if __name__ == "__main__":
    sys.exit(main())
