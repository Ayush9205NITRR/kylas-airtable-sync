"""
Read-only probe: does Kylas expose call logs, and what shape do they have?

Answers three questions before any call-log feature gets designed:
  1. Which call-ish entity does this tenant actually have? (probes candidates)
  2. What fields does it carry — specifically: who called, how long, when?
  3. How does a call link back to a deal / company / contact?

SAFETY: the repo is public right now, so Actions logs are public too. This
prints field NAMES and TYPES (schema, not data). Values are shown only for
keys that cannot be personal — ids, timestamps, durations, enums. Every other
value is masked to a type + length. Never loosen this while the repo is public.

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

# Entity names Kylas might file call logs under. Cheap to probe, so cast wide.
CANDIDATES = [
    "call", "calls", "call-log", "calllog", "phone-call",
    "activity", "activities", "task", "meeting", "event", "log",
]

# Keys whose values cannot identify a person -> safe to print verbatim.
SAFE_KEYS = {
    "id", "createdAt", "updatedAt", "calledAt", "startTime", "endTime",
    "duration", "durationInSeconds", "callDuration", "status", "type",
    "callType", "direction", "outcome", "callOutcome", "disposition",
    "entityType", "recordActionType", "totalElements", "totalPages",
    "dealId", "companyId", "contactId", "relatedToId", "ownerId", "userId",
}


def preview(key: str, val):
    """Value if provably non-identifying, else a masked type descriptor."""
    if val is None or isinstance(val, bool) or isinstance(val, (int, float)):
        return val
    if key in SAFE_KEYS:
        return val
    if isinstance(val, str):
        if "@" in val:
            return mask_email(val)
        return f"<str len={len(val)}>"
    if isinstance(val, list):
        return [preview(key, v) for v in val[:2]] + (["..."] if len(val) > 2 else [])
    if isinstance(val, dict):
        return {k: preview(k, v) for k, v in val.items()}
    return f"<{type(val).__name__}>"


def probe_fields(entity: str):
    """GET /entities/{entity}/fields — the field-definition endpoint."""
    try:
        r = requests.get(f"{BASE}/entities/{entity}/fields",
                         params={"entityType": entity, "custom-only": "false",
                                 "page": 0, "size": 200},
                         headers=HEADERS, timeout=30)
    except Exception as e:
        return None, f"EXC {e}"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    try:
        body = r.json()
    except Exception:
        return None, "non-JSON"
    items = body if isinstance(body, list) else (body.get("content") or body.get("data") or [])
    return items, f"HTTP 200 ({len(items)} fields)"


def probe_search(entity: str):
    """POST /search/{entity} — one record, to learn the runtime shape."""
    try:
        r = requests.post(f"{BASE}/search/{entity}",
                          params={"page": 0, "size": 1, "sort": "updatedAt,desc"},
                          json={"fields": None, "jsonRule": None},
                          headers=HEADERS, timeout=30)
    except Exception as e:
        return None, f"EXC {e}", None
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}", None
    body = r.json()
    content = body.get("content") or []
    return content, f"HTTP 200 (totalElements={body.get('totalElements')})", body.get("totalElements")


def main():
    print("=" * 70)
    print("KYLAS CALL-LOG PROBE  (schema only — values masked, logs are public)")
    print("=" * 70)

    found = []
    print("\n--- 1. Which call-ish entities exist? ---")
    for ent in CANDIDATES:
        fields, fmsg = probe_fields(ent)
        content, smsg, total = probe_search(ent)
        ok = bool(fields) or content is not None
        print(f"  {ent:14s} fields={fmsg:28s} search={smsg}")
        if ok:
            found.append((ent, fields, content, total))

    if not found:
        print("\nNo call entity responded. Kylas may not expose call logs on this")
        print("plan/tenant, or they live under a name not probed. Next step would")
        print("be the Kylas API docs or support — do not guess a schema.")
        return

    print("\n--- 2. Field definitions (name -> type) ---")
    for ent, fields, _, _ in found:
        if not fields:
            continue
        print(f"\n  [{ent}] {len(fields)} fields:")
        for f in fields:
            if not isinstance(f, dict):
                continue
            name = f.get("name") or f.get("fieldName")
            disp = f.get("displayName") or ""
            ftype = f.get("type") or f.get("fieldType") or "?"
            std = "custom" if str(name).startswith("cf") else "std"
            print(f"    {str(name):32s} {str(ftype):16s} {std:7s} {disp}")

    print("\n--- 3. Runtime shape of one record (values masked) ---")
    for ent, _, content, total in found:
        if not content:
            print(f"\n  [{ent}] search returned no rows (totalElements={total})")
            continue
        rec = content[0]
        print(f"\n  [{ent}] one record, {len(rec)} keys:")
        print(json.dumps({k: preview(k, v) for k, v in rec.items()},
                         indent=4, default=str)[:4000])

    print("\n--- 4. Deal / company / contact linkage ---")
    for ent, _, content, _ in found:
        if not content:
            continue
        rec = content[0]
        links = {k: preview(k, v) for k, v in rec.items()
                 if any(t in k.lower() for t in
                        ("deal", "company", "contact", "relat", "entity", "owner", "assoc"))}
        print(f"\n  [{ent}] linkage keys: {json.dumps(links, default=str)[:1500]}")

    print("\nDone.")


if __name__ == "__main__":
    main()
