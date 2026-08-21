"""
Read-only probe of the Kylas Call Log + Webhook APIs against this tenant.

The official Postman collection documents both, but a published collection is
not proof that a given tenant/plan exposes them. This confirms against the real
tenant before anything is designed on top:

  GET  /v1/call-logs/{id}?relatedToType=deal   — do call logs exist, what shape?
  GET  /v1/webhooks                            — are webhooks readable/settable?

Specifically answers the question the create-payload does NOT: which field
carries WHO made the call. The documented POST body has outcome/startTime/
duration/callType/phoneNumber/relatedTo/associatedTo but no caller field, so
attribution has to be read off a real record.

SAFETY: the repo is public, so Actions logs are public. Field names print
verbatim; string values are masked to type+length. Phone numbers are masked
to their last 3 digits. Do not loosen this while the repo is public.

Run: python scripts/probe_call_logs.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from dotenv import load_dotenv

from utils.redact import mask_email

load_dotenv()

BASE = "https://api.kylas.io/v1"
HEADERS = {"api-key": os.environ["KYLAS_API_KEY"], "Content-Type": "application/json"}

# Non-identifying keys -> safe to print verbatim. Everything else is masked.
SAFE_KEYS = {
    "id", "outcome", "callType", "duration", "startTime", "endTime",
    "createdAt", "updatedAt", "entity", "entityType", "relatedToType",
    "active", "requestType", "authenticationType", "events",
    "ownerId", "createdById", "updatedById", "userId", "tenantId",
    "totalElements", "totalPages", "recordActionType",
}


def preview(key, val):
    if val is None or isinstance(val, (bool, int, float)):
        return val
    if isinstance(val, str):
        if key in SAFE_KEYS:
            return val
        if "@" in val:
            return mask_email(val)
        if key.lower().endswith("phonenumber") or key.lower() == "phone":
            return f"***{val[-3:]}" if len(val) > 3 else "***"
        return f"<str len={len(val)}>"
    if key in SAFE_KEYS and isinstance(val, list) and all(isinstance(v, str) for v in val):
        return val
    if isinstance(val, list):
        return [preview(key, v) for v in val[:2]] + (["..."] if len(val) > 2 else [])
    if isinstance(val, dict):
        return {k: preview(k, v) for k, v in val.items()}
    return f"<{type(val).__name__}>"


def get(path, params=None):
    try:
        r = requests.get(f"{BASE}/{path}", params=params, headers=HEADERS, timeout=30)
    except Exception as e:
        return None, f"EXC {e}"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code} {r.text[:200]}"
    try:
        return r.json(), "HTTP 200"
    except Exception:
        return None, "non-JSON"


def recent_deal_ids(n=12):
    """Most recently updated deals, to look for one carrying call logs."""
    r = requests.post(f"{BASE}/search/deal",
                      params={"page": 0, "size": n, "sort": "updatedAt,desc"},
                      json={"fields": ["id", "name", "updatedAt"], "jsonRule": None},
                      headers=HEADERS, timeout=30)
    r.raise_for_status()
    return [d.get("id") for d in r.json().get("content", []) if d.get("id")]


def main():
    print("=" * 70)
    print("KYLAS CALL-LOG + WEBHOOK PROBE  (values masked — logs are public)")
    print("=" * 70)

    print("\n--- 1. GET /call-logs/{deal_id}?relatedToType=deal ---")
    ids = recent_deal_ids()
    print(f"  scanning {len(ids)} most-recently-updated deals\n")
    found_any = None
    for did in ids:
        body, msg = get(f"call-logs/{did}", {"relatedToType": "deal"})
        rows = []
        if isinstance(body, list):
            rows = body
        elif isinstance(body, dict):
            rows = body.get("content") or body.get("data") or []
        print(f"    deal {did}: {msg}  rows={len(rows)}")
        if rows and found_any is None:
            found_any = (did, rows)

    if found_any:
        did, rows = found_any
        print(f"\n--- 2. Shape of a real call log (deal {did}) ---")
        rec = rows[0]
        print(f"  {len(rec)} keys:")
        print(json.dumps({k: preview(k, v) for k, v in rec.items()},
                         indent=4, default=str)[:3000])
        print("\n  ATTRIBUTION — keys that could carry 'who made the call':")
        who = {k: preview(k, v) for k, v in rec.items()
               if any(t in k.lower() for t in
                      ("own", "user", "creat", "updat", "by", "agent", "caller"))}
        print("  " + json.dumps(who, default=str)[:1200])
    else:
        print("\n--- 2. No call logs found on any scanned deal ---")
        print("  Endpoint reachability is what matters above: HTTP 200 with rows=0")
        print("  means the API works and the tenant simply has no calls logged yet")
        print("  (nothing writes them today). A 4xx means it is not available here.")

    print("\n--- 3. GET /webhooks (for DEAL_UPDATED) ---")
    body, msg = get("webhooks")
    print(f"  {msg}")
    if isinstance(body, (list, dict)):
        hooks = body if isinstance(body, list) else (body.get("content") or body.get("data") or [])
        print(f"  {len(hooks)} webhook(s) currently registered")
        for h in hooks[:5]:
            if isinstance(h, dict):
                print("    " + json.dumps({k: preview(k, v) for k, v in h.items()},
                                          default=str)[:600])

    print("\nDone.")


if __name__ == "__main__":
    main()
