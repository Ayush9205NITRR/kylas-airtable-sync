#!/usr/bin/env python3
"""
Company Matrix / Contact Matrix — week-by-week views rolled up from the base table.

Both are DERIVED. Nothing is measured here: every number is read back out of
"BD Metrics Daily" (the base table) and re-bucketed by week of month.

    BD Stage Changes        contact moved X -> Y on this date (the evidence)
      └─ BD Metrics Daily       THE BASE TABLE, per associate per day per metric
           ├─ Contact Matrix    <- this script, Metric Group = Contact
           └─ Company Matrix    <- this script, Metric Group = Company

One row per associate per month per metric, with the weeks of that month across
the columns:

    BD Associate   Month     Metric           W1   W2   W3   W4   W5   Month Total
    Anjali Athya   2026-09   Call Attempted   12   19    8    0    -           39

WEEK OF MONTH
────────────────────────────────────────────────────────────────────────────
W1 is the calendar week containing the 1st, W2 the next, and so on — not "the
first 7 days". A month therefore spans 5 weeks more often than 4, and 6 when a
31-day month starts on a Sunday, so the table carries W1..W6 and simply leaves
the unused ones blank. Truncating at W4 would silently drop the last few days
of most months.

Weeks are cut on the same Monday boundary as the ISO week used everywhere else
in this repo, so "W3" here and the weekly digest's ISO week agree about which
days belong together.

CLOSING A MONTH
────────────────────────────────────────────────────────────────────────────
Once a month is over its rows are frozen: they are never rewritten, and the
next month simply starts new rows. That is what keeps the history clean rather
than decaying — see bd_company_funnel._is_closed for the same rule at day and
week grain, and why re-deriving a closed period silently shrinks it.

The base table already freezes closed DAYS, so a re-run mostly reproduces the
same numbers anyway; the month freeze is the belt to that pair of braces, and
the thing that makes "finalise the month" a real guarantee rather than a
coincidence.

    python scripts/bd_matrix_views.py             # build + push both matrices
    python scripts/bd_matrix_views.py --dry-run   # print them, write nothing
    python scripts/bd_matrix_views.py --month 2026-09   # just one month
"""
import argparse
import importlib.util
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name.replace(".py", ""), os.path.join(_HERE, name))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


long_mod = _load("bd_metrics_long.py")
funnel   = _load("bd_company_funnel.py")

META = "https://api.airtable.com/v0/meta/bases"

# Metric Group in the base table -> the Airtable table this view writes to.
VIEWS = {
    "Contact": "Contact Matrix",
    "Company": "Company Matrix",
}

METRICS = {
    "Contact": long_mod.CONTACT_METRICS,
    "Company": list(long_mod.COMPANY_METRICS),
}

# A month can touch six calendar weeks (a 31-day month starting on a Sunday).
MAX_WEEKS = 6
WEEK_COLS = [f"W{i}" for i in range(1, MAX_WEEKS + 1)]
TOTAL_COL = "Month Total"


def _monday(d: date) -> date:
    """The Monday of d's week — the same boundary the ISO week uses."""
    return d - timedelta(days=d.weekday())


def week_of_month(day: str) -> int:
    """
    1-based index of the calendar week within the day's month.

    W1 is the week containing the 1st, so a month beginning on a Saturday has a
    two-day W1 rather than shifting everything. Counting whole weeks from the
    1st instead ("days 1-7 are W1") would split calendar weeks across two
    columns and disagree with the weekly digest about which days go together.
    """
    d = date.fromisoformat(day)
    return ((_monday(d) - _monday(d.replace(day=1))).days // 7) + 1


def build_matrix(long_rows: dict, group: str, month: str = None) -> dict:
    """
    {(rep, email, month, metric): {"W1": n, ..., "Month Total": n}}

    long_rows is the base table's contents (see
    bd_metrics_long.read_metrics_daily) — this only re-buckets, never re-counts.
    """
    wanted = set(METRICS[group])
    out = defaultdict(lambda: defaultdict(int))

    for (rep, email, day, grp, metric), value in long_rows.items():
        if grp != group or metric not in wanted:
            continue
        mon = day[:7]
        if month and mon != month:
            continue
        try:
            wk = week_of_month(day)
        except ValueError:          # a malformed Date in the base table
            continue
        if not 1 <= wk <= MAX_WEEKS:
            continue
        cell = out[(rep, email, mon, metric)]
        cell[f"W{wk}"] += value
        cell[TOTAL_COL] += value

    return out


def _ensure(base_id: str, headers: dict, name: str) -> bool:
    r = requests.get(f"{META}/{base_id}/tables", headers=headers, timeout=30)
    r.raise_for_status()
    if any(t["name"] == name for t in r.json().get("tables", [])):
        return True
    fields = ([{"name": n, "type": "singleLineText"} for n in
               ("Key", "BD Associate", "BD Email", "Month", "Metric", "Updated At")] +
              [{"name": n, "type": "number", "options": {"precision": 0}}
               for n in WEEK_COLS + [TOTAL_COL]])
    resp = requests.post(f"{META}/{base_id}/tables",
                         json={"name": name, "fields": fields},
                         headers=headers, timeout=30)
    if resp.status_code in (200, 201):
        print(f"[matrix] Created Airtable table {name!r}")
        return True
    print(f"[matrix] ERROR creating {name!r}: {resp.status_code} {resp.text[:300]}")
    return False


def push_matrix(cells: dict, table_name: str, today: str) -> dict:
    from utils.airtable_client import AirtableClient
    base_id = os.environ["AIRTABLE_BASE_ID"]
    headers = {"Authorization": f"Bearer {os.environ['AIRTABLE_PAT']}",
               "Content-Type": "application/json"}
    if not _ensure(base_id, headers, table_name):
        return {}

    at = AirtableClient(table_name)
    at.build_cache("Key")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tally = defaultdict(int)

    for (rep, email, month, metric), weeks in sorted(cells.items()):
        key = f"{rep} | {month} | {metric}"
        # A finished month is a record, not a projection. Once it exists it is
        # never rewritten — the next month starts its own rows.
        if funnel._is_closed(month, today) and key in at._cache:
            tally["frozen"] += 1
            continue
        fields = {"Key": key, "BD Associate": rep, "BD Email": email,
                  "Month": month, "Metric": metric, "Updated At": stamp}
        # Only the weeks this month actually has — leaving the rest unset keeps
        # a 4-week month visibly blank in W5/W6 rather than falsely showing 0.
        for col in WEEK_COLS:
            if col in weeks:
                fields[col] = weeks[col]
        fields[TOTAL_COL] = weeks.get(TOTAL_COL, 0)
        action, _ = at.upsert("Key", key, fields, stamp, updated_at_field="")
        tally[action] += 1

    at.flush()
    print(f"[matrix] {table_name}: created={tally['created']} "
          f"updated={tally['updated']} frozen={tally['frozen']}")
    return tally


def render(cells: dict, table_name: str) -> None:
    """Print one matrix — used by --dry-run and for eyeballing a real run."""
    if not cells:
        print(f"\n{table_name}: (no rows)")
        return
    months = sorted({k[2] for k in cells})
    print(f"\n{table_name}")
    for month in months:
        rows = {k: v for k, v in cells.items() if k[2] == month}
        # Show every week up to the last one with data, so a week nobody
        # worked reads as 0 rather than the column vanishing from the table.
        last = max((WEEK_COLS.index(c) for v in rows.values() for c in v
                    if c in WEEK_COLS), default=0)
        used = WEEK_COLS[:last + 1]
        print(f"  {month}   {'BD Associate':22} {'Metric':22} "
              + "  ".join(f"{c:>4}" for c in used) + f"  {'TOTAL':>6}")
        for (rep, _e, _m, metric), weeks in sorted(rows.items()):
            cellstr = "  ".join(f"{weeks.get(c, 0):>4}" for c in used)
            print(f"            {rep[:22]:22} {metric[:22]:22} {cellstr} "
                  f"{weeks.get(TOTAL_COL, 0):>6}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="print both matrices, write nothing")
    ap.add_argument("--month", default="",
                    help="only this month, YYYY-MM (default: every month held "
                         "in the base table)")
    args = ap.parse_args()

    today = datetime.now(timezone.utc).date().isoformat()
    print(f"[matrix] Reading {long_mod.TABLE_NAME!r} (the base table)...")
    long_rows = long_mod.read_metrics_daily()
    print(f"[matrix] {len(long_rows)} stored row(s)")
    if not long_rows:
        print(f"[matrix] WARNING: {long_mod.TABLE_NAME!r} is empty — nothing to "
              f"roll up. The daily bd_metrics_long.py run populates it.")
        return 0

    for group, table_name in VIEWS.items():
        cells = build_matrix(long_rows, group, args.month or None)
        if args.dry_run:
            render(cells, table_name)
        else:
            push_matrix(cells, table_name, today)

    if args.dry_run:
        print("\n[matrix] dry run — nothing written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
