"""
BD Trends rollup — aggregates "BD Daily Stats" (Slot=full_day rows) into the
persistent "BD Trends" table: one row per (grain x period x owner) for
Day / Week / Month / Quarter. Feeds an Airtable Interface dashboard, so unlike
BD Daily Stats (90-day retention) most of this table is meant to persist.

Period definitions
───────────────────
  Day     period = the row's Date (YYYY-MM-DD)     period_start = that date
  Week    period = "<iso_year>-W<iso_week>"          period_start = Monday of that ISO week
  Month   period = Date[:7]  (YYYY-MM)               period_start = 1st of that month
  Quarter period = "<year>-Q<1-4>"                    period_start = 1st of Jan/Apr/Jul/Oct

Completeness guard
───────────────────
BD Daily Stats keeps a rolling ~90-day window (see modules/05_bd_stats.py
RETENTION_DAYS). If we aggregated a Week/Month/Quarter whose window is only
PARTIALLY covered by the rows still in BD Daily Stats (e.g. the oldest week of
the window is aging out day by day), the rolled-up total would silently
shrink each run as history disappears — instead of just staying frozen once
complete. To avoid that, let min_date = the earliest Date among the fetched
daily rows. A Week/Month/Quarter period is only emitted if its period_start
>= min_date, i.e. the ENTIRE period is still inside the retained window; the
oldest, now-partial period is skipped rather than under-written. Day grain has
no such risk (each day is a complete, atomic unit) so every day is emitted.

Upsert / prune
───────────────
Rows are upserted by "Key" = "<Grain>|<Period>|<Owner>" and always refreshed
(no staleness field to compare — the numbers can grow as more daily rows
land), mirroring scripts/bd_monthly_matrix.py. Day rows older than 100 days
are pruned each run (10 days past BD Daily Stats' own 90-day retention, so a
Day row is never pruned before its source data could too); Week/Month/Quarter
rows are kept forever.

Run:  python scripts/build_bd_trends.py
Env:  AIRTABLE_PAT, AIRTABLE_BASE_ID
"""
import os
import sys
import time
from collections import defaultdict
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.airtable_client import AirtableClient
from utils.bd_metrics import BD_KEYS

SRC_TABLE = "BD Daily Stats"
DST_TABLE = "BD Trends"

GRAINS = ("Day", "Week", "Month", "Quarter")

# Same column names 05_bd_stats.py writes to BD Daily Stats; BD Trends reuses
# them verbatim so a metric copies straight across with no re-mapping.
FIELD = {
    "attempted":  "Attempted",
    "connected":  "Connected",
    "dcb":        "Discovery Calls",
    "sql":        "SQL",
    "mql":        "MQL",
    "activation": "Activation",
}

DAY_PRUNE_DAYS = 100   # Day-grain rows older than this are deleted; Week/Month/Quarter kept forever


# ──────────────────────────────────────────────────────────────────────────
# Period math
# ──────────────────────────────────────────────────────────────────────────

def _week_start(d: date) -> date:
    """Monday of d's ISO week (date.isoweekday(): Mon=1 .. Sun=7)."""
    return d - timedelta(days=d.isoweekday() - 1)


def _quarter_start(d: date) -> date:
    q_month = ((d.month - 1) // 3) * 3 + 1   # 1, 4, 7, or 10
    return date(d.year, q_month, 1)


def _periods_for(d_str: str, d: date) -> dict:
    """{grain: (period_label, period_start_date)} for one BD Daily Stats row's date."""
    iso_year, iso_week, _ = d.isocalendar()
    q = (d.month - 1) // 3 + 1
    return {
        "Day":     (d_str,                        d),
        "Week":    (f"{iso_year}-W{iso_week:02d}", _week_start(d)),
        "Month":   (d_str[:7],                     date(d.year, d.month, 1)),
        "Quarter": (f"{d.year}-Q{q}",               _quarter_start(d)),
    }


# ──────────────────────────────────────────────────────────────────────────
# Read + aggregate
# ──────────────────────────────────────────────────────────────────────────

def read_daily_rows() -> list:
    """Fetch full_day rows from BD Daily Stats -> [(date_str, owner, {key: value})]."""
    tbl = AirtableClient(SRC_TABLE)
    records = tbl.table.all(formula="{Slot}='full_day'")
    rows = []
    for rec in records:
        f = rec["fields"]
        d_str = str(f.get("Date", "")).strip()
        owner = str(f.get("Owner", "")).strip()
        if not d_str or not owner:
            continue
        metrics = {k: int(f.get(col, 0) or 0) for k, col in FIELD.items()}
        rows.append((d_str, owner, metrics))
    return rows


def aggregate(rows: list) -> tuple:
    """rows: [(date_str, owner, {key: value})].

    Returns (agg, min_date):
      agg[grain][(period, owner)] = {"period_start": date, "metrics": {key: total}}
      min_date = earliest Date among rows (date object), or None if rows is empty.
    """
    parsed = []
    for d_str, owner, metrics in rows:
        try:
            d = date.fromisoformat(d_str)
        except ValueError:
            continue
        parsed.append((d_str, d, owner, metrics))

    if not parsed:
        return {g: {} for g in GRAINS}, None

    min_date = min(d for _, d, _, _ in parsed)

    agg = {g: {} for g in GRAINS}
    for d_str, d, owner, metrics in parsed:
        for grain, (period, period_start) in _periods_for(d_str, d).items():
            if grain != "Day" and period_start < min_date:
                # Completeness guard — the oldest partial Week/Month/Quarter
                # is skipped rather than under-written. Day is always atomic.
                continue
            bucket = agg[grain].setdefault(
                (period, owner),
                {"period_start": period_start, "metrics": {k: 0 for k in BD_KEYS}},
            )
            for k in BD_KEYS:
                bucket["metrics"][k] += metrics.get(k, 0)

    return agg, min_date


# ──────────────────────────────────────────────────────────────────────────
# Write
# ──────────────────────────────────────────────────────────────────────────

def push_to_airtable(agg: dict) -> dict:
    """Upsert every (grain, period, owner) row. Returns {grain: {"created": n, "updated": n}}."""
    tbl = AirtableClient(DST_TABLE)
    n = tbl.build_cache("Key")
    print(f"[bd-trends] {n} existing row(s) in {DST_TABLE!r}")

    tally = {g: defaultdict(int) for g in GRAINS}
    for grain in GRAINS:
        for (period, owner), data in sorted(agg[grain].items()):
            key = f"{grain}|{period}|{owner}"
            fields = {
                "Key":          key,
                "Grain":        grain,
                "Period":       period,
                "Period Start": data["period_start"].isoformat(),
                "Owner":        owner,
            }
            for k, col in FIELD.items():
                fields[col] = data["metrics"][k]
            # Always refresh: no staleness field to compare against, and
            # Week/Month/Quarter totals legitimately grow as more days land.
            action, _ = tbl.upsert("Key", key, fields, "", updated_at_field="")
            tally[grain][action] += 1

    tbl.flush()
    return tally, tbl


def prune_old_day_rows(tbl: AirtableClient, retention_days: int = DAY_PRUNE_DAYS) -> int:
    """Delete Grain=='Day' rows whose Period Start is older than retention_days."""
    cutoff = (date.today() - timedelta(days=retention_days)).isoformat()
    old_ids = [
        r["id"] for r in tbl._cache.values()
        if r["fields"].get("Grain") == "Day"
        and str(r["fields"].get("Period Start", "9999-12-31")) < cutoff
    ]
    if not old_ids:
        return 0
    for i in range(0, len(old_ids), 10):
        time.sleep(0.2)
        tbl.table.batch_delete(old_ids[i:i + 10])
    print(f"[bd-trends] pruned {len(old_ids)} Day row(s) older than {retention_days} days")
    return len(old_ids)


# ──────────────────────────────────────────────────────────────────────────

def main():
    from dotenv import load_dotenv
    load_dotenv()

    print(f"[bd-trends] Reading {SRC_TABLE!r} (Slot=full_day)...")
    rows = read_daily_rows()
    print(f"[bd-trends] {len(rows)} daily row(s) scanned")

    agg, min_date = aggregate(rows)
    if min_date is None:
        print("[bd-trends] No BD Daily Stats rows found — nothing to do")
        return
    print(f"[bd-trends] Oldest retained date: {min_date.isoformat()}")
    for grain in GRAINS:
        print(f"[bd-trends]   {grain:<7} {len(agg[grain])} period x owner row(s) to upsert")

    tally, tbl = push_to_airtable(agg)
    pruned = prune_old_day_rows(tbl)

    print("\n[bd-trends] === Summary ===")
    print(f"[bd-trends] rows scanned (BD Daily Stats, full_day): {len(rows)}")
    total_created = total_updated = 0
    for grain in GRAINS:
        c, u = tally[grain]["created"], tally[grain]["updated"]
        total_created += c
        total_updated += u
        print(f"[bd-trends]   {grain:<7} created={c:<4} updated={u:<4}")
    print(f"[bd-trends] total created={total_created} updated={total_updated} pruned={pruned}")


if __name__ == "__main__":
    main()
