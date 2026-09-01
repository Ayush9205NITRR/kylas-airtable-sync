#!/usr/bin/env python3
"""
BD Metrics Daily — the same numbers, reshaped so metric NAMES can go on an axis.

Airtable (and every other charting tool) plots the VALUES of one column on the
X-axis. The funnel tables keep each metric in its own column, so those can only
ever be six separate Y-series — there is no way to get "Call Attempted, Call
Connected, Meeting Booked, …" along the X-axis from that shape.

This writes the same data in LONG form: one row per associate per day per
metric, with the metric name in a `Metric` column and the number in `Value`.
Chart it as X = Metric, Y = Sum of Value, and filter by BD Associate / Week /
Month. One chart definition then serves the team view and every individual.

Two metric families live here, tagged by `Metric Group` so a chart can show
either without mixing grains:

  Contact  — Call Attempted, Call Connected, Meeting Booked, Meeting Done,
             SQL, MQL. Counted in CONTACTS. Definitions are imported from
             bd_monthly_matrix.py rather than restated, so the daily view and
             the monthly matrix can never drift apart.
  Company  — Companies Worked, Companies Reached, Requirements Stated, Handoff
             Calls Held, SQLs Accepted, SQLs Rejected. Counted in COMPANIES,
             each at its best stage that day. Imported from bd_company_funnel.py.

Never put both groups in one chart: one counts people, the other accounts.

Day attribution is cfLastCalledAt, matching both source scripts. Closed days are
frozen on first write — see bd_company_funnel._is_closed for why re-deriving
history silently shrinks it.

    python scripts/bd_metrics_long.py              # build + push
    python scripts/bd_metrics_long.py --dry-run    # print a summary only
"""
import argparse
import importlib.util
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.kylas_client import KylasClient                      # noqa: E402
from utils.account_pipeline import load_order, _company_id       # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name: str):
    """Import a sibling script by path — they start with digits/underscores and
    are not importable as normal modules."""
    spec = importlib.util.spec_from_file_location(
        name.replace(".py", ""), os.path.join(_HERE, name))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


matrix = _load("bd_monthly_matrix.py")
funnel = _load("bd_company_funnel.py")

META       = "https://api.airtable.com/v0/meta/bases"
TABLE_NAME = "BD Metrics Daily"

CONTACT_METRICS = ["Call Attempted", "Call Connected", "Meeting Booked",
                   "Meeting Done", "SQL", "MQL"]
COMPANY_METRICS = funnel.COLUMNS

HISTORY_DAYS = funnel.DAILY_HISTORY_DAYS


# bd_monthly_matrix imports this inside its build function, so it is not a
# module attribute there — take it from the shared source both of them use.
from utils.bd_metrics import ATTEMPTED_EXCLUDE                   # noqa: E402

_ATTEMPTED_EXCLUDE_N = None    # built lazily: _norm lives on the matrix module


def _contact_counts(stage_n: str, order) -> dict:
    """The six contact-level metrics for ONE contact's normalised stage."""
    global _ATTEMPTED_EXCLUDE_N
    if _ATTEMPTED_EXCLUDE_N is None:
        _ATTEMPTED_EXCLUDE_N = {matrix._norm(s) for s in ATTEMPTED_EXCLUDE}
    attempted = bool(stage_n) and stage_n not in _ATTEMPTED_EXCLUDE_N
    return {
        "Call Attempted":  int(attempted),
        "Call Connected":  int(attempted and stage_n not in matrix._CNC_EXCLUDE_N),
        "Meeting Booked":  int(stage_n in matrix._MEETING_BOOKED_N),
        "Meeting Done":    int(stage_n in matrix._MEETING_DONE_N),
        "SQL":             int(stage_n == matrix._norm(funnel.SQL_STAGE)),
        "MQL":             int(stage_n in matrix._MQL_N),
    }


def build_long(kylas, all_owners: bool = False) -> tuple:
    """Return ({(rep, email, day, group, metric): value}, stats)."""
    from utils.bd_metrics import refresh_stage_map, contact_stage
    refresh_stage_map(kylas)

    order    = load_order()
    user_map = funnel._build_user_map(kylas)
    roster   = set() if all_owners else funnel.bd_roster()

    print("[long] Fetching contacts from Kylas...")
    contacts = kylas._search_all(
        "contact",
        fields=["id", "company", "ownedBy", "ownerId", "updatedAt",
                "customFieldValues"],
    )
    print(f"[long] {len(contacts)} contacts fetched")

    # Contact metrics accumulate directly; company metrics need the per-day
    # best-rank pass first, since an account counts once at its best stage.
    contact_cells = defaultdict(lambda: defaultdict(int))   # (rep,email,day) -> metric -> n
    company_best  = defaultdict(dict)                       # (rep,email,day) -> cid -> rank
    skipped = 0

    for ct in contacts:
        cf = ct.get("customFieldValues") or {}
        day = funnel._parse_lc(cf.get("cfLastCalledAt", ""))
        if not day:
            skipped += 1
            continue
        stage = contact_stage(ct)
        if not stage:
            skipped += 1
            continue
        name, email = funnel._owner(ct, user_map)
        if roster and email.strip().lower() not in roster:
            skipped += 1
            continue

        key = (name, email, day)
        for metric, n in _contact_counts(matrix._norm(stage), order).items():
            if n:
                contact_cells[key][metric] += n

        cid = _company_id(ct)
        if cid:
            rank = order.rank_of(stage)
            if rank:
                cur = company_best[key].get(cid)
                if cur is None or rank < cur:
                    company_best[key][cid] = rank

    long_rows = {}
    for key, metrics in contact_cells.items():
        for metric in CONTACT_METRICS:
            long_rows[(*key, "Contact", metric)] = metrics.get(metric, 0)
    for key, companies in company_best.items():
        counts = funnel._counts(list(companies.values()), order)
        for metric in COMPANY_METRICS:
            long_rows[(*key, "Company", metric)] = counts[metric]

    stats = {"contacts": len(contacts), "skipped": skipped, "rows": len(long_rows)}
    print(f"[long] {skipped} contact(s) skipped (no call date, blank stage, "
          f"or off-roster) → {len(long_rows)} long row(s)")
    return long_rows, stats


def ensure_table(base_id: str, headers: dict) -> bool:
    r = requests.get(f"{META}/{base_id}/tables", headers=headers, timeout=30)
    r.raise_for_status()
    if any(t["name"] == TABLE_NAME for t in r.json().get("tables", [])):
        print(f"[long] Airtable table {TABLE_NAME!r} already exists")
        return True
    defn = {
        "name": TABLE_NAME,
        "description": ("Long/tidy form of the BD metrics: one row per "
                        "associate per day per metric. Chart X = Metric, "
                        "Y = Sum of Value. Filter Metric Group to 'Contact' or "
                        "'Company' — never chart both together, one counts "
                        "people and the other accounts. "
                        "Built by scripts/bd_metrics_long.py."),
        "fields": [
            {"name": "Key",          "type": "singleLineText"},
            {"name": "Date",         "type": "singleLineText"},
            {"name": "Week",         "type": "singleLineText"},
            {"name": "Month",        "type": "singleLineText"},
            {"name": "BD Email",     "type": "singleLineText"},
            {"name": "BD Associate", "type": "singleLineText"},
            {"name": "Metric Group", "type": "singleLineText"},
            {"name": "Metric",       "type": "singleLineText"},
            {"name": "Value",        "type": "number", "options": {"precision": 0}},
            {"name": "Updated At",   "type": "singleLineText"},
        ],
    }
    resp = requests.post(f"{META}/{base_id}/tables", json=defn, headers=headers, timeout=30)
    if resp.status_code in (200, 201):
        print(f"[long] Created Airtable table {TABLE_NAME!r}")
        return True
    print(f"[long] ERROR creating {TABLE_NAME!r}: {resp.status_code} {resp.text[:300]}")
    return False


def push(long_rows: dict) -> None:
    from utils.airtable_client import AirtableClient
    base_id = os.environ["AIRTABLE_BASE_ID"]
    headers = {"Authorization": f"Bearer {os.environ['AIRTABLE_PAT']}",
               "Content-Type": "application/json"}
    if not ensure_table(base_id, headers):
        return

    today  = datetime.now(timezone.utc).date().isoformat()
    cutoff = (datetime.now(timezone.utc).date()
              - timedelta(days=HISTORY_DAYS)).isoformat()
    rows = {k: v for k, v in long_rows.items() if k[2] >= cutoff}

    at = AirtableClient(TABLE_NAME)
    n  = at.build_cache("Key")
    print(f"[long] {n} existing row(s) in {TABLE_NAME!r}")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    tally, frozen = defaultdict(int), 0
    for (rep, email, day, group, metric), value in sorted(rows.items()):
        key = f"{rep} | {day} | {group} | {metric}"
        # Closed days are a record, not a projection — see
        # bd_company_funnel._is_closed.
        if funnel._is_closed(day, today) and key in at._cache:
            frozen += 1
            continue
        action, _ = at.upsert(
            "Key", key,
            {"Key": key, "Date": day, "Week": funnel._iso_week(day),
             "Month": day[:7], "BD Email": email, "BD Associate": rep,
             "Metric Group": group, "Metric": metric, "Value": value,
             "Updated At": stamp},
            stamp, updated_at_field="")
        tally[action] += 1
    at.flush()
    print(f"[long] {TABLE_NAME}: created={tally['created']} "
          f"updated={tally['updated']} skipped={tally['skipped']} frozen={frozen}")

    funnel.prune_expired(TABLE_NAME, cutoff)


def summarise(long_rows: dict) -> None:
    """Totals per metric for the most recent month present — a sanity read."""
    if not long_rows:
        return
    month = max(k[2][:7] for k in long_rows)
    tot = defaultdict(int)
    for (_r, _e, day, group, metric), v in long_rows.items():
        if day[:7] == month:
            tot[(group, metric)] += v
    print(f"\n{month} totals")
    print("-" * 46)
    for group in ("Contact", "Company"):
        for metric in (CONTACT_METRICS if group == "Contact" else COMPANY_METRICS):
            print(f"  {group:<8} {metric:<22} {tot[(group, metric)]:>7}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="print a summary, write nothing to Airtable")
    ap.add_argument("--all-owners", action="store_true",
                    help="include owners outside the BD roster (diagnostic)")
    args = ap.parse_args()

    long_rows, _ = build_long(KylasClient(), all_owners=args.all_owners)
    summarise(long_rows)
    if args.dry_run:
        print(f"[long] dry run — nothing written ({len(long_rows)} rows)")
        return 0
    push(long_rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
