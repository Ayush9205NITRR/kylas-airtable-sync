"""
Find a read shape for /call-logs that returns MORE than the newest ten.

Measured on run 32554452867: GET /call-logs?entityId=X&entityType=Y returns the
same ten records for every X and Y -- ids 43794436..43843303, spanning 20-22 Aug
only. Contact 5927888's calls of 19 and 20 Aug are NOT among them, though
GET /call-logs/43734306 reads the 19 Aug one back by its own id without trouble.
So the records exist and are readable; the listing endpoint just will not list
them. A summary built on that listing is silently incomplete on every date.

This probes, read-only, for a shape that is complete:

  1. the response envelope -- if it carries totalElements/totalPages/last, the
     listing IS paged and only the parameter names are wrong
  2. paging parameter spellings (page/size were measured to 404, but not the
     dozen other names an API might use)
  3. the documented GET /call-logs/{id}?relatedToType=... against a CONTACT.
     That was tried once against a DEAL and 404d, which is what you would get
     if the path segment is always read as a call-log id -- so the contact case
     has never actually been tested, and reps log against contacts
  4. POST /search/call-log and friends, the pattern every other Kylas entity in
     this repo is read through
  5. date-range filters, the thing an EOD summary actually wants

SAFETY: repo is public, logs are public. Ids, counts, HTTP codes and envelope
keys print; names, phone numbers and free text are masked.
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

CONTACT = int(os.environ.get("MISSING_CONTACT") or 5927888)
DEAL = int(os.environ.get("MISSING_DEAL") or 4676048)
WANT = int(os.environ.get("MISSING_CALL_LOG") or 43734306)

# Baseline: the ten the current sweep can see. Anything that returns an id
# outside this set is strictly better than what we have.
BASELINE = set()


def rows_of(body):
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for k in ("content", "data", "items", "results"):
            v = body.get(k)
            if isinstance(v, list):
                return v
    return []


def envelope(body):
    """Top-level keys with scalar values -- paging metadata lives here."""
    if not isinstance(body, dict):
        return f"<{type(body).__name__}>"
    return {k: v for k, v in body.items()
            if isinstance(v, (int, float, bool)) or v is None}


def call(label, method, path, params=None, body=None, show_env=False):
    try:
        r = requests.request(method, f"{BASE}/{path}", headers=HEADERS,
                             params=params, json=body, timeout=30)
    except Exception as e:
        print(f"  {label:54s} EXC {str(e)[:60]}")
        return None
    if r.status_code != 200:
        print(f"  {label:54s} HTTP {r.status_code} {r.text[:90]}")
        return None
    try:
        data = r.json()
    except Exception:
        print(f"  {label:54s} HTTP 200 (unparseable)")
        return None
    rows = rows_of(data)
    ids = {x.get("id") for x in rows if isinstance(x, dict) and x.get("id")}
    extra = ids - BASELINE if BASELINE else set()
    flag = ""
    if WANT in ids:
        flag = "  <<< HAS THE MISSING RECORD"
    elif extra:
        flag = f"  <<< {len(extra)} id(s) the sweep cannot see"
    print(f"  {label:54s} HTTP 200  {len(rows)} row(s){flag}")
    if show_env:
        print(f"       envelope: {json.dumps(envelope(data), default=str)[:300]}")
    return data


def main():
    global BASELINE
    print("=" * 74)
    print("CALL-LOG LISTING: PROBE FOR A COMPLETE READ")
    print("=" * 74)

    print("\n[0] baseline + response envelope (does it admit to being paged?)")
    base = call("GET /call-logs?entityId&entityType", "GET", "call-logs",
                params={"entityId": DEAL, "entityType": "deal"}, show_env=True)
    BASELINE = {x.get("id") for x in rows_of(base) if x.get("id")}
    if isinstance(base, dict):
        print(f"       all top-level keys: {sorted(base.keys())}")

    print("\n[1] paging parameter spellings")
    for k in ("page", "size", "pageNo", "pageNumber", "pageSize", "perPage",
              "per_page", "limit", "offset", "start", "count", "max", "top"):
        for v in (1, 100):
            call(f"?{k}={v}", "GET", "call-logs",
                 params={"entityId": DEAL, "entityType": "deal", k: v})

    print("\n[2] the documented per-entity read, against a CONTACT this time")
    for label, path, params in [
        (f"GET /call-logs/{CONTACT}?relatedToType=contact", f"call-logs/{CONTACT}",
         {"relatedToType": "contact"}),
        (f"GET /call-logs/{CONTACT}?relatedToType=CONTACT", f"call-logs/{CONTACT}",
         {"relatedToType": "CONTACT"}),
        (f"GET /call-logs/{CONTACT} (no param)", f"call-logs/{CONTACT}", None),
        (f"GET /call-logs/{DEAL}?relatedToType=deal", f"call-logs/{DEAL}",
         {"relatedToType": "deal"}),
    ]:
        call(label, "GET", path, params=params, show_env=True)

    print("\n[3] the search pattern every other entity here uses")
    for path in ("search/call-log", "search/call-logs", "search/calllog",
                 "search/callLog", "call-logs/search"):
        call(f"POST /{path}", "POST", path,
             params={"page": 0, "size": 100, "sort": "startTime,desc"},
             body={"fields": None, "jsonRule": None}, show_env=True)

    print("\n[4] date-range filters (what an EOD summary actually wants)")
    lo, hi = "2026-08-18T00:00:00.000Z", "2026-08-23T00:00:00.000Z"
    for params in (
        {"startTime": lo}, {"from": lo, "to": hi}, {"fromDate": lo, "toDate": hi},
        {"startDate": lo, "endDate": hi}, {"createdAtGte": lo},
        {"startTimeFrom": lo, "startTimeTo": hi},
    ):
        p = {"entityId": DEAL, "entityType": "deal", **params}
        call(f"?{'&'.join(params)}", "GET", "call-logs", params=p)

    print("\n[5] nested-under-entity shapes")
    for label, path in [
        (f"GET /contacts/{CONTACT}/call-logs", f"contacts/{CONTACT}/call-logs"),
        (f"GET /deals/{DEAL}/call-logs", f"deals/{DEAL}/call-logs"),
        (f"GET /call-logs/contact/{CONTACT}", f"call-logs/contact/{CONTACT}"),
    ]:
        call(label, "GET", path, show_env=True)

    print("\n" + "=" * 74)
    print(f"BASELINE (what the current sweep sees): {len(BASELINE)} ids, "
          f"{min(BASELINE) if BASELINE else '-'}..{max(BASELINE) if BASELINE else '-'}")
    print("Any line flagged above beats it. If none is flagged, the listing")
    print("endpoint cannot be made complete and the summary needs a different")
    print("source for its candidate calls entirely.")
    print("=" * 74)


if __name__ == "__main__":
    main()
