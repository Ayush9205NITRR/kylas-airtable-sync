"""
Which paging parameters does /call-logs actually accept — and together?

Run 32554568828 showed `page` alone works (page=100 returned rows page 0 never
shows) and `size` alone works (size=100 returned 100 rows including the record
the old sweep could not see). Run 32554777140 then showed that sending BOTH
404s the endpoint. That is the same 404 an early probe hit when it sent page,
size and sort together and concluded no paging existed at all.

So the question is not "is it paged" -- it is which combinations answer, and
how large a single `size` can go. If size can reach the low thousands, one
request covers months of calls and paging is moot. If not, `page` alone at ten
rows a page is the only complete read, and a day's summary has to walk back to
the day it wants.

SAFETY: repo is public, logs are public. Only ids, counts, HTTP codes and
row timestamps print.
"""
import os
import sys
from collections import Counter
from datetime import timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from dotenv import load_dotenv

load_dotenv()

BASE = "https://api.kylas.io/v1"
HEADERS = {"api-key": os.environ["KYLAS_API_KEY"], "Content-Type": "application/json"}
IST = timezone(timedelta(hours=5, minutes=30))

DEAL = int(os.environ.get("MISSING_DEAL") or 4676048)
WANT = int(os.environ.get("MISSING_CALL_LOG") or 43734306)


def get(label, extra):
    params = {"entityId": DEAL, "entityType": "deal"}
    params.update(extra)
    try:
        r = requests.get(f"{BASE}/call-logs", headers=HEADERS, params=params, timeout=60)
    except Exception as e:
        print(f"  {label:34s} EXC {str(e)[:50]}")
        return None
    if r.status_code != 200:
        print(f"  {label:34s} HTTP {r.status_code} {r.text[:70]}")
        return None
    body = r.json()
    rows = body.get("content") or []
    env = {k: v for k, v in body.items() if not isinstance(v, (list, dict))}
    times = sorted(str(x.get("startTime") or "") for x in rows if x.get("startTime"))
    span = f"{times[0][:10]}..{times[-1][:10]}" if times else "-"
    flag = "  <<< HAS THE MISSING RECORD" if any(x.get("id") == WANT for x in rows) else ""
    print(f"  {label:34s} HTTP 200  {len(rows):5d} rows  {span}  "
          f"pages={env.get('totalPages')}{flag}")
    return rows


def main():
    print("=" * 78)
    print("WHICH PAGING PARAMS DOES /call-logs ACCEPT?")
    print("=" * 78)

    print("\n[A] size alone — how big can one response get?")
    best = None
    for n in (10, 50, 100, 200, 500, 1000, 2000, 5000, 10000):
        rows = get(f"size={n}", {"size": n})
        if rows is not None and len(rows) >= (best or 0):
            best = len(rows)

    print("\n[B] page alone (size defaults to 10)")
    for n in (0, 1, 2, 5, 50):
        get(f"page={n}", {"page": n})

    print("\n[C] page + size together, the combination that 404d")
    for p, sz in ((0, 100), (1, 100), (0, 50), (1, 50), (1, 20), (0, 10), (1, 10)):
        get(f"page={p}&size={sz}", {"page": p, "size": sz})

    print("\n[D] alternate spellings paired with page")
    for extra in ({"page": 1, "pageSize": 100}, {"page": 1, "limit": 100},
                  {"pageNo": 1, "size": 100}, {"offset": 10, "size": 100}):
        get("&".join(f"{k}={v}" for k, v in extra.items()), extra)

    print("\n[E] does `size` alone still reach older days as it grows?")
    for n in (100, 500, 1000):
        rows = get(f"size={n} (day histogram)", {"size": n})
        if rows:
            hist = Counter(str(r.get("startTime") or "")[:10] for r in rows)
            days = sorted(hist)
            print(f"       {len(days)} distinct day(s): {days[0]} .. {days[-1]}")

    print("\n" + "=" * 78)
    print(f"Largest single response seen: {best} rows.")
    print("If that covers the days a summary needs, one sized request beats")
    print("paging. Otherwise `page` alone is the only complete walk.")
    print("=" * 78)


if __name__ == "__main__":
    main()
