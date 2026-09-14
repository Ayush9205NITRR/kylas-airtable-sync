#!/usr/bin/env python3
"""
Count every record in both Airtable bases, per table, and project when the
50,000-record cap gets hit.

Read-only: this lists and counts, and never writes or deletes anything.

WHY THIS EXISTS
The base hit its record cap on 2026-09-13 and stopped accepting new rows —
BD Stage Changes lost 127 writes, Account Activity Log 69, and the digests
froze because no new stage change could be stored. Picking retention windows
to prevent that recurring needs real per-table counts, not estimates from run
logs: the two tables that actually matter (BD Metrics Daily at ~156 rows/day,
BD Stage Changes at ~120/day) are append-only, so the cap is structural and
arrives on a schedule rather than by accident.

The projection is deliberately crude -- rows-per-day measured over the rows
that carry a Date, extrapolated flat. It is meant to answer "which table puts
us over, and roughly when", not to be a forecast.

    python scripts/audit_airtable_usage.py
    python scripts/audit_airtable_usage.py --cap 50000
"""
import argparse
import collections
import os
import sys
from datetime import date, datetime, timedelta

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

META = "https://api.airtable.com/v0/meta/bases"
# Date-ish column names, in the order they should be believed. A table with
# none of these is treated as flat (no growth), which is right for Contacts,
# Deals and BD Members and wrong for nothing currently in either base.
DATE_FIELDS = ("Date", "Day", "Created At", "Updated At")


def list_tables(base_id: str, headers: dict) -> list:
    r = requests.get(f"{META}/{base_id}/tables", headers=headers, timeout=30)
    r.raise_for_status()
    return [(t["id"], t["name"]) for t in r.json().get("tables", [])]


def count_table(base_id: str, table_id: str, headers: dict) -> tuple:
    """(row_count, {date: rows}) — paginates the whole table.

    Asks for the date columns only. Airtable bills by request, not by field,
    but a narrow projection keeps a 10k-row table from dragging every column
    of every row across the wire just to be counted.
    """
    url = f"https://api.airtable.com/v0/{base_id}/{table_id}"
    params = [("pageSize", 100)] + [("fields[]", f) for f in DATE_FIELDS]
    total, by_date, offset = 0, collections.Counter(), None
    while True:
        q = list(params) + ([("offset", offset)] if offset else [])
        r = requests.get(url, headers=headers, params=q, timeout=60)
        if r.status_code == 422:
            # Unknown field in the projection — retry asking for everything.
            r = requests.get(url, headers=headers,
                             params=[("pageSize", 100)] +
                                    ([("offset", offset)] if offset else []),
                             timeout=60)
        r.raise_for_status()
        body = r.json()
        for rec in body.get("records", []):
            total += 1
            f = rec.get("fields", {})
            for key in DATE_FIELDS:
                val = str(f.get(key, "") or "")[:10]
                if len(val) == 10 and val[4] == "-":
                    by_date[val] += 1
                    break
        offset = body.get("offset")
        if not offset:
            return total, by_date


def per_day(by_date: dict, days: int = 30) -> float:
    """Mean rows/day over the last `days` calendar days that have any rows.

    Measured over days that actually produced rows, so weekends and holidays
    don't deflate the rate into something that under-projects the cap.
    """
    if not by_date:
        return 0.0
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    recent = {d: n for d, n in by_date.items() if d >= cutoff}
    return (sum(recent.values()) / len(recent)) if recent else 0.0


def audit(label: str, base_id: str, headers: dict) -> tuple:
    print(f"\n{'=' * 72}\n{label}  ({base_id})\n{'=' * 72}")
    print(f"  {'table':34} {'rows':>8}  {'rows/day':>9}  {'/year':>8}")
    print(f"  {'-' * 34} {'-' * 8}  {'-' * 9}  {'-' * 8}")
    rows_total, growth_total, detail = 0, 0.0, []
    for tid, name in sorted(list_tables(base_id, headers), key=lambda t: t[1]):
        try:
            n, by_date = count_table(base_id, tid, headers)
        except Exception as exc:
            print(f"  {name:34} {'ERROR':>8}  {str(exc)[:40]}")
            continue
        rate = per_day(by_date)
        rows_total += n
        growth_total += rate
        detail.append((name, n, rate))
        print(f"  {name:34} {n:8,}  {rate:9.1f}  {rate * 365:8,.0f}")
    print(f"  {'-' * 34} {'-' * 8}  {'-' * 9}  {'-' * 8}")
    print(f"  {'TOTAL':34} {rows_total:8,}  {growth_total:9.1f}  "
          f"{growth_total * 365:8,.0f}")
    return rows_total, growth_total, detail


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Count Airtable records per table and project the cap")
    ap.add_argument("--cap", type=int, default=50000,
                    help="Per-base record cap to measure headroom against")
    args = ap.parse_args()

    headers = {"Authorization": f"Bearer {os.environ['AIRTABLE_PAT']}"}
    main_base = os.environ["AIRTABLE_BASE_ID"]
    co_base = os.environ.get("AIRTABLE_COMPANY_BASE_ID", "")

    bases = [("MAIN base (metrics, contacts, deals)", main_base)]
    if co_base and co_base != main_base:
        bases.append(("COMPANY base", co_base))

    for label, base_id in bases:
        total, growth, detail = audit(label, base_id, headers)
        head = args.cap - total
        print(f"\n  cap {args.cap:,} — used {total:,} "
              f"({total / args.cap * 100:.0f}%), headroom {head:,}")
        if growth > 0:
            days = head / growth
            when = (date.today() + timedelta(days=int(days))).isoformat()
            print(f"  at {growth:.0f} rows/day that is ~{days:.0f} day(s) "
                  f"of headroom → full around {when}"
                  if head > 0 else
                  f"  ALREADY OVER — new rows are being rejected right now")
        print("\n  biggest tables:")
        for name, n, rate in sorted(detail, key=lambda d: -d[1])[:5]:
            share = n / total * 100 if total else 0
            print(f"    {name:34} {n:8,}  ({share:4.1f}% of base, "
                  f"{rate:.0f}/day)")
    return 0


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    sys.exit(main())
