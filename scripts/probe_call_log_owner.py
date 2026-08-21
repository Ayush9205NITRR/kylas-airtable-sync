"""
Can the caller be set on a call log with the SINGLE existing admin key?

The create payload documents no caller field, and the record comes back with
createdBy = updatedBy = owner = the API key's user. But Kylas clearly permits
one key to assign records to other users elsewhere: Create Deal takes
ownedBy{...}, Update Deal takes ownedBy and createdBy, and this repo already
uses update_company_owner / update_contact_owner successfully. So the same may
work on call logs, undocumented.

This PATCHes the EXISTING test call log (recordActions said update: true) with
each plausible owner shape and reads the record back after each, reporting
which -- if any -- actually sticks. It creates nothing new.

If one sticks, no per-rep API keys are needed: create with the admin key, then
set the owner to the real rep.

SAFETY: repo is public, logs are public. Ids and enums print; names, phone
numbers and free text are masked.
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


def read_record(cl_id, deal_id):
    """Fetch the call log via the read shape that actually works."""
    r = requests.get(f"{BASE}/call-logs",
                     params={"entityId": deal_id, "entityType": "deal"},
                     headers=HEADERS, timeout=30)
    if r.status_code != 200:
        return None
    body = r.json()
    rows = body if isinstance(body, list) else (body.get("content") or body.get("data") or [])
    for rec in rows:
        if str(rec.get("id")) == str(cl_id):
            return rec
    return None


def ids_of(rec):
    def _id(k):
        v = rec.get(k)
        return v.get("id") if isinstance(v, dict) else v
    return _id("owner"), _id("createdBy"), _id("updatedBy")


def main():
    cl_id = (os.environ.get("CALL_LOG_ID") or "").strip()
    deal_id = (os.environ.get("TEST_DEAL_ID") or "").strip()
    target = (os.environ.get("TARGET_USER_ID") or "").strip()
    if not (cl_id and deal_id and target):
        print("CALL_LOG_ID / TEST_DEAL_ID / TARGET_USER_ID required -- nothing done.")
        return 0

    print("=" * 70)
    print(f"CALL-LOG OWNER PROBE  call_log={cl_id} deal={deal_id} target={target}")
    print("=" * 70)

    rec = read_record(cl_id, deal_id)
    if not rec:
        print("  Could not read the call log back -- aborting.")
        return 1
    o, c, u = ids_of(rec)
    print(f"  BEFORE: owner={o}  createdBy={c}  updatedBy={u}")
    if str(o) == target:
        print("  Already owned by the target; pick a different TARGET_USER_ID.")
        return 0

    shapes = [
        ("owner as object",     {"owner": {"id": int(target)}}),
        ("ownedBy as object",   {"ownedBy": {"id": int(target)}}),
        ("ownerId scalar",      {"ownerId": int(target)}),
        ("createdBy as object", {"createdBy": {"id": int(target)}}),
        ("owner scalar",        {"owner": int(target)}),
    ]

    winner = None
    for label, body in shapes:
        r = requests.patch(f"{BASE}/call-logs/{cl_id}", json=body,
                           headers=HEADERS, timeout=30)
        after = read_record(cl_id, deal_id)
        o2, c2, u2 = ids_of(after) if after else ("?", "?", "?")
        stuck = str(o2) == target or str(c2) == target
        print(f"  PATCH {label:22s} HTTP {r.status_code:3d} "
              f"-> owner={o2} createdBy={c2} {'<-- STUCK' if stuck else ''}")
        if r.status_code >= 300:
            print(f"        body: {r.text[:200]}")
        if stuck:
            winner = (label, body, after)
            break

    print("\n" + "=" * 70)
    print("VERDICT")
    print("=" * 70)
    if winner:
        label, body, after = winner
        print(f"  WORKS: PATCH /call-logs/{{id}} with {json.dumps(body)}   ({label})")
        print("\n  No per-rep API keys needed: create with the admin key, then")
        print("  PATCH the owner to the real rep. Confirm in the Kylas UI which")
        print("  field 'Logged By' actually renders before relying on it.")
        if after:
            print("\n  record now:")
            print(json.dumps({k: mask(k, v) for k, v in after.items()},
                             indent=4, default=str)[:1800])
    else:
        print("  None of the shapes changed the owner. Attribution on a call log")
        print("  is fixed to the API key's user at creation and cannot be")
        print("  reassigned -- per-rep keys, or logging in the Kylas app, are")
        print("  the only ways to get the real caller onto the record.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
