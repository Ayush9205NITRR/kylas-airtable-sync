"""
How do we find the deals that had calls today, without sweeping every deal?

Call logs can only be read per entity (GET /call-logs?entityId&entityType), so
building an EOD summary needs a candidate list of deals. Asking every deal is
the N+1 that made the offsite rollup take 45 minutes -- not repeating it.

The cheap candidate set is "deals updated since this morning", which the sync
already fetches. That only works if logging a call bumps the parent deal's
updatedAt. This checks whether it does, on a deal with known call times, and
sizes the candidate set for a real day.

Read-only. Repo is public, so logs are public: ids, timestamps and enums print;
names and numbers are masked.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

from utils.kylas_client import KylasClient

load_dotenv()

IST = timezone(timedelta(hours=5, minutes=30))


def main():
    deal_id = int(os.environ.get("PROBE_DEAL_ID") or 4383813)
    client = KylasClient()

    print("=" * 70)
    print("CALL RECENCY PROBE")
    print("=" * 70)

    # 1. Does a call log bump the deal's updatedAt?
    try:
        deal = client.get_deal(deal_id)
    except Exception as exc:
        print(f"  could not read deal {deal_id}: {exc}")
        return 1
    print(f"\n  deal {deal_id}")
    print(f"    updatedAt : {deal.get('updatedAt')}")
    print(f"    createdAt : {deal.get('createdAt')}")

    calls = client.get_call_logs(deal_id, "deal")
    print(f"    {len(calls)} call log(s), newest first by startTime:")
    for c in sorted(calls, key=lambda x: str(x.get("startTime")), reverse=True)[:5]:
        cb = (c.get("createdBy") or {}).get("id")
        print(f"      [{c.get('id')}] start={c.get('startTime')} "
              f"created={c.get('createdAt')} by_id={cb} "
              f"{c.get('outcome')}/{c.get('duration')}")

    newest_call = max((str(c.get("createdAt") or "") for c in calls), default="")
    print(f"\n    newest call createdAt : {newest_call}")
    print(f"    deal updatedAt        : {deal.get('updatedAt')}")
    if newest_call and str(deal.get("updatedAt") or "") >= newest_call:
        print("    => deal.updatedAt is at/after the newest call: a call plausibly")
        print("       bumps the deal, so 'deals updated today' is a valid candidate set.")
    else:
        print("    => deal.updatedAt is OLDER than the newest call: logging a call")
        print("       does NOT bump the deal. 'deals updated today' would MISS deals")
        print("       whose only activity was a call. Need a different candidate set.")

    # 2. How big is a day's candidate set?
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"\n  deals updated since {since} (24h):")
    try:
        recent = client.get_deals(since=since)
        print(f"    {len(recent)} deal(s)")
        print(f"    => one get_call_logs each at ~0.12s pacing "
              f"= ~{len(recent) * 0.35:.0f}s of API time")
    except Exception as exc:
        print(f"    failed: {exc}")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
