"""
The write worked; the documented read did not. Find the shape that does.

POST /call-logs/ returned HTTP 201 with {"id": 43843294} against deal 4383813,
but GET /call-logs/4383813?relatedToType=deal returned 404 errorCode 02002001
seconds later. So the record exists and the documented per-deal read is wrong,
or means something other than what the collection implies.

Note PATCH and PUT both address /call-logs/<call_log_id>, so the GET may want
the call-log id rather than the deal id. This tries that and every other
plausible shape, read-only, and reports which returns the record.

CALL_LOG_ID / TEST_DEAL_ID come from the environment.

SAFETY: repo is public, logs are public. Ids and enums print; names, emails,
phone numbers and note text are masked.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from dotenv import load_dotenv

load_dotenv()

BASE = "https://api.kylas.io/v1"
HEADERS = {"api-key": os.environ["KYLAS_API_KEY"], "Content-Type": "application/json"}
# Any key whose value can identify a person. "originator"/"receiver"/"ivrNumber"
# are phone numbers that do NOT contain "phone" — they printed verbatim into a
# public Actions log before this list included them. Add, never remove.
NAMEY = ("name", "firstname", "lastname", "email", "phone", "description",
         "originator", "receiver", "ivrnumber", "deviceid", "summary")


def mask(key, val):
    if val is None or isinstance(val, (bool, int, float)):
        return val
    if isinstance(val, str):
        return f"<str len={len(val)}>" if any(t in key.lower() for t in NAMEY) else val
    if isinstance(val, list):
        return [mask(key, v) for v in val[:3]]
    if isinstance(val, dict):
        return {k: mask(k, v) for k, v in val.items()}
    return f"<{type(val).__name__}>"


def show(label, method, path, **kw):
    try:
        r = requests.request(method, f"{BASE}/{path}", headers=HEADERS, timeout=30, **kw)
    except Exception as e:
        print(f"  {label:52s} EXC {e}")
        return None
    print(f"  {label:52s} HTTP {r.status_code} {r.text[:120]}")
    if r.status_code == 200:
        try:
            return r.json()
        except Exception:
            return None
    return None


def main():
    cl_id = (os.environ.get("CALL_LOG_ID") or "").strip()
    deal_id = (os.environ.get("TEST_DEAL_ID") or "").strip()
    print("=" * 70)
    print(f"CALL-LOG READ-SHAPE PROBE  (call_log={cl_id or '-'} deal={deal_id or '-'})")
    print("=" * 70)

    hits = []
    if cl_id:
        print("\n--- by call-log id ---")
        for label, path, params in [
            ("GET /call-logs/{cl}", f"call-logs/{cl_id}", None),
            ("GET /call-logs/{cl}?relatedToType=deal", f"call-logs/{cl_id}",
             {"relatedToType": "deal"}),
        ]:
            b = show(label, "GET", path, params=params)
            if b:
                hits.append((label, b))

    if deal_id:
        print("\n--- by deal id, alternate shapes ---")
        for label, path, params in [
            ("GET /call-logs?relatedToId&relatedToType", "call-logs",
             {"relatedToId": deal_id, "relatedToType": "deal"}),
            ("GET /call-logs?entityId&entityType", "call-logs",
             {"entityId": deal_id, "entityType": "deal"}),
            ("GET /call-logs/{deal}?relatedToType=DEAL", f"call-logs/{deal_id}",
             {"relatedToType": "DEAL"}),
            ("GET /call-logs/deal/{deal}", f"call-logs/deal/{deal_id}", None),
        ]:
            b = show(label, "GET", path, params=params)
            if b:
                hits.append((label, b))
        print("\n--- search shapes ---")
        for label, path, body in [
            ("POST /search/call-log", "search/call-log", {"fields": None, "jsonRule": None}),
            ("POST /call-logs/search", "call-logs/search", {}),
        ]:
            b = show(label, "POST", path, params={"page": 0, "size": 5}, json=body)
            if b:
                hits.append((label, b))

    print("\n" + "=" * 70)
    print("RESULT")
    print("=" * 70)
    if not hits:
        print("  No read shape returned 200. Call logs are write-only via this API")
        print("  on this tenant — which means attribution cannot be read back at all,")
        print("  and a Kylas-side report or the UI is the only way to see it.")
        return
    for label, body in hits:
        rec = body
        if isinstance(body, dict):
            c = body.get("content") or body.get("data")
            if isinstance(c, list) and c:
                rec = c[0]
            elif isinstance(c, dict):
                rec = c
        elif isinstance(body, list) and body:
            rec = body[0]
        print(f"\n  WORKS: {label}")
        if isinstance(rec, dict):
            print(json.dumps({k: mask(k, v) for k, v in rec.items()},
                             indent=4, default=str)[:2500])
            who = {k: (v.get("id") if isinstance(v, dict) else v)
                   for k, v in rec.items()
                   if any(t in k.lower() for t in
                          ("own", "creat", "updat", "user", "by", "agent", "caller"))}
            print(f"  ATTRIBUTION KEYS: {json.dumps(who, default=str)[:800]}")


if __name__ == "__main__":
    main()
