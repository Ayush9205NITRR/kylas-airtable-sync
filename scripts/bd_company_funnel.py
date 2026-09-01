#!/usr/bin/env python3
"""
BD Company Funnel — per BD associate, per month, counted in COMPANIES.

Sibling of bd_monthly_matrix.py, which counts CONTACTS. This one answers "how
many ACCOUNTS did this rep move, and how far", so a rep who called six people
at one company scores that company once, not six times.

Columns
───────────────────────────────────────────────────────────────────────────────
  Companies Worked     the rep called someone there this month and that contact
                       has a pipeline stage set (a blank stage is not "worked")
  Companies Reached    Companies Worked minus accounts stuck at CNC 1/2/3 —
                       somebody actually got through
  Requirements Stated  the account reached rank 1-12, i.e. Activation or better
  Handoff Calls Held   the account reached rank 1-3: SQL, Discovery Call Done -
                       Awaiting Client Inputs, or Closing Loops - Low Value
  SQLs Accepted        the account reached SQL (Sales Qualified Lead)
  SQLs Rejected        the account landed at Closing Loops - Low Value

An account is counted at its BEST stage for that rep that month — the same
best-of-contacts rule the Account Pipeline Stage rollup uses, scoped to one
rep and one month rather than all time.

Rank thresholds come from config/account_pipeline_order.json, so the funnel and
the Account Pipeline Stage column can never drift apart. test_bd_company_funnel
pins the two boundaries (rank 12 = Activation, rank 3 = Closing Loops - Low
Value) so a reorder of that config fails loudly instead of silently changing
what these columns mean.

Month attribution is cfLastCalledAt, matching bd_monthly_matrix.py. Deliberately
NOT the max(createdAt, updatedAt, cfLastCalledAt) composite used by account
health: updatedAt is bumped by our own owner/field pushes, which would drag every
contact into the current month and empty out the historical ones.

Two tables are written from ONE Kylas fetch:

  BD Company Funnel        one row per rep per MONTH. Distinct accounts, so an
                           account worked on ten days still counts once.
  BD Company Funnel Daily  one row per rep per DAY, carrying Week and Month
                           labels for grouping. Rows SUM cleanly over any date
                           range, which is what makes week-on-week and
                           month-on-month charts work in a BI tool.

They deliberately disagree: summing daily rows over a month gives a HIGHER
number than the monthly row, because an account worked on three separate days
appears on three daily rows. Daily answers "how much activity", monthly answers
"how many distinct accounts". Use the monthly table for distinct counts.

    python scripts/bd_company_funnel.py              # build + push to Airtable
    python scripts/bd_company_funnel.py --dry-run    # print the table only
"""
import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.kylas_client import KylasClient          # noqa: E402
from utils.account_pipeline import load_order, _norm, _company_id  # noqa: E402

META       = "https://api.airtable.com/v0/meta/bases"
TABLE_NAME       = "BD Company Funnel"
DAILY_TABLE_NAME = "BD Company Funnel Daily"

# Bound how much daily history is pushed. ~16 reps x ~22 working days is ~350
# rows a month; 400 days keeps year-on-year possible without unbounded growth.
DAILY_HISTORY_DAYS = 400

COLUMNS = ["Companies Worked", "Companies Reached", "Requirements Stated",
           "Handoff Calls Held", "SQLs Accepted", "SQLs Rejected"]

# Rank boundaries in config/account_pipeline_order.json (1 is best).
REQUIREMENTS_MAX_RANK = 12   # ... through "Activation"
HANDOFF_MAX_RANK      = 3    # SQL / Discovery Call Done - Awaiting / Closing Loops

SQL_STAGE      = "SQL (Sales Qualified Lead)"
REJECTED_STAGE = "Closing Loops - Low Value"

# "Reached" means somebody actually got through — only the three hard
# could-not-connect stages are excluded.
#
# "Followup - CNC" is deliberately NOT here, though bd_monthly_matrix.py's
# CNC_EXCLUDE_STAGES does include it. It sits at rank 10, inside the rank 1-12
# band that defines Requirements Stated, so excluding it here would let
# Requirements Stated exceed Companies Reached and break the funnel's nesting.
# The two tables therefore disagree on this one stage, on purpose.
NOT_REACHED_STAGES = {
    "CNC (Could Not Connect) - 1",
    "CNC (Could Not Connect) - 2",
    "CNC (Could Not Connect) - 3",
}


def _parse_lc(raw: str) -> str:
    """cfLastCalledAt → 'YYYY-MM-DD'. '' when absent/unparseable."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if raw[0].isdigit():
        return raw[:10]
    try:
        return datetime.strptime(raw.split(" at ")[0], "%b %d, %Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _owner(ct: dict, user_map: dict) -> tuple:
    """(name, email) for a contact's owner. Mirrors bd_monthly_matrix._owner_name:
    the search API usually returns a bare ownerId, not a populated ownedBy."""
    ob = ct.get("ownedBy")
    if isinstance(ob, dict):
        name = (ob.get("name")
                or f"{ob.get('firstName', '')} {ob.get('lastName', '')}".strip())
        email = (ob.get("email") or ob.get("emailId") or "").strip()
        if name:
            return name, (email or user_map.get(f"email:{name}", ""))
    oid = ct.get("ownerId") or (ob if isinstance(ob, (int, float)) else None)
    if oid:
        key = str(int(oid))
        name = user_map.get(key, "")
        if name:
            return name, user_map.get(f"email:{name}", "")
    return "Unassigned", ""


def _build_user_map(kylas) -> dict:
    """{str(uid): name} plus {'email:<name>': email} in one flat dict."""
    umap = {}
    tp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "config", "team.json")
    try:
        import json
        with open(tp) as fh:
            cfg = json.load(fh)
        for uid, name in (cfg.get("kylas_users") or {}).items():
            umap[str(uid)] = name
        for name, email in (cfg.get("kylas_user_emails") or {}).items():
            umap[f"email:{name}"] = email
    except Exception as exc:
        print(f"[funnel] WARN: team.json unusable ({exc})")
    try:
        for uid, name in (kylas.get_users() or {}).items():
            umap.setdefault(str(uid), name)
    except Exception as exc:
        print(f"[funnel] WARN: live user list unavailable ({exc})")
    try:
        for name, email in (kylas.get_user_emails() or {}).items():
            umap.setdefault(f"email:{name}", email)
    except Exception as exc:
        print(f"[funnel] WARN: live user emails unavailable ({exc})")
    return umap


def _counts(ranks: list, order) -> dict:
    """Turn a list of per-company best ranks into the six funnel columns."""
    cnc      = {order.rank_of(x) for x in NOT_REACHED_STAGES} - {0}
    sql_rank = order.rank_of(SQL_STAGE)
    rej_rank = order.rank_of(REJECTED_STAGE)
    return {
        "Companies Worked":    len(ranks),
        "Companies Reached":   sum(1 for r in ranks if r not in cnc),
        "Requirements Stated": sum(1 for r in ranks if r <= REQUIREMENTS_MAX_RANK),
        "Handoff Calls Held":  sum(1 for r in ranks if r <= HANDOFF_MAX_RANK),
        "SQLs Accepted":       sum(1 for r in ranks if r == sql_rank),
        "SQLs Rejected":       sum(1 for r in ranks if r == rej_rank),
    }


def _iso_week(day: str) -> str:
    """'2026-09-01' -> '2026-W36'. Precomputed so a BI tool can group by week
    without needing date functions over a text column."""
    try:
        y, w, _ = datetime.strptime(day, "%Y-%m-%d").date().isocalendar()
        return f"{y}-W{w:02d}"
    except ValueError:
        return ""


def build_funnel(kylas) -> tuple:
    """
    One Kylas fetch, two grains.

    Returns (month_grid, day_grid, stats), each keyed
    (rep, email, period) -> {column: count}.

    The month grid is NOT the sum of the day grid: each is accumulated
    separately so a company worked on several days is one account for the month
    but appears on each of those days.
    """
    from utils.bd_metrics import refresh_stage_map, contact_stage
    refresh_stage_map(kylas)     # bare option ids must resolve to real labels

    order    = load_order()
    user_map = _build_user_map(kylas)

    print("[funnel] Fetching contacts from Kylas...")
    # ownerId and updatedAt are both REQUIRED — see bd_monthly_matrix.py:
    # without ownerId every row collapses to "Unassigned", and without
    # updatedAt _search_all silently truncates at the ~10k search cap.
    contacts = kylas._search_all(
        "contact",
        fields=["id", "company", "ownedBy", "ownerId", "updatedAt",
                "customFieldValues"],
    )
    print(f"[funnel] {len(contacts)} contacts fetched")

    by_month = defaultdict(dict)     # (rep, email, 'YYYY-MM') -> {cid: best_rank}
    by_day   = defaultdict(dict)     # (rep, email, 'YYYY-MM-DD') -> {cid: best_rank}
    no_lc = no_stage = no_company = 0

    for ct in contacts:
        cf = ct.get("customFieldValues") or {}
        lc = _parse_lc(cf.get("cfLastCalledAt", ""))
        if not lc:
            no_lc += 1              # no date to attribute to → excluded by design
            continue
        stage = contact_stage(ct)
        if not stage:
            no_stage += 1           # "last contact stage is not empty"
            continue
        cid = _company_id(ct)
        if not cid:
            no_company += 1         # a company-level metric needs a company
            continue

        rank = order.rank_of(stage)
        if not rank:
            continue                # unrecognised — surfaced by report_unranked
        name, email = _owner(ct, user_map)
        for bucket, period in ((by_month, lc[:7]), (by_day, lc)):
            cell = bucket[(name, email, period)]
            cur  = cell.get(cid)
            if cur is None or rank < cur:
                cell[cid] = rank

    order.report_unranked()

    month_grid = {k: _counts(list(v.values()), order) for k, v in by_month.items()}
    day_grid   = {k: _counts(list(v.values()), order) for k, v in by_day.items()}

    stats = {"contacts": len(contacts), "no_last_called": no_lc,
             "no_stage": no_stage, "no_company": no_company,
             "month_rows": len(month_grid), "day_rows": len(day_grid)}
    print(f"[funnel] skipped: {no_lc} without a Last Called date, "
          f"{no_stage} with a blank stage, {no_company} without a company")
    print(f"[funnel] → {len(month_grid)} rep×month rows, {len(day_grid)} rep×day rows")
    return month_grid, day_grid, stats


def print_table(grid: dict) -> None:
    months = sorted({m for _, _, m in grid}, reverse=True)
    reps   = sorted({r for r, _, _ in grid})
    w = max([len(r) for r in reps] + [14])
    head = f"{'MONTH':<9} {'BD ASSOCIATE':<{w}} " + " ".join(f"{c:>19}" for c in COLUMNS)
    print("\n" + head)
    print("-" * len(head))
    for m in months:
        tot = dict.fromkeys(COLUMNS, 0)
        for r in reps:
            k = next((k for k in grid if k[0] == r and k[2] == m), None)
            if k is None:
                continue
            c = grid[k]
            print(f"{m:<9} {r:<{w}} " + " ".join(f"{c[x]:>19}" for x in COLUMNS))
            for x in COLUMNS:
                tot[x] += c[x]
        print(f"{'':<9} {'TOTAL':<{w}} " + " ".join(f"{tot[x]:>19}" for x in COLUMNS))
        print()


def ensure_table(base_id: str, headers: dict) -> bool:
    r = requests.get(f"{META}/{base_id}/tables", headers=headers, timeout=30)
    r.raise_for_status()
    existing = next((t for t in r.json().get("tables", []) if t["name"] == TABLE_NAME), None)
    if existing:
        have = {f["name"] for f in existing.get("fields", [])}
        for col in COLUMNS:
            if col in have:
                continue
            resp = requests.post(
                f"{META}/{base_id}/tables/{existing['id']}/fields",
                json={"name": col, "type": "number", "options": {"precision": 0}},
                headers=headers, timeout=30)
            print(f"[funnel]   {'+ added' if resp.status_code in (200,201) else '! failed'} {col!r}")
        print(f"[funnel] Airtable table {TABLE_NAME!r} already exists")
        return True

    defn = {
        "name": TABLE_NAME,
        "description": ("Per BD associate per month, counted in COMPANIES not "
                        "contacts. Built by scripts/bd_company_funnel.py."),
        "fields": [
            {"name": "Key",          "type": "singleLineText"},  # "<rep> | YYYY-MM"
            {"name": "Month",        "type": "singleLineText"},
            {"name": "BD Email",     "type": "singleLineText"},
            {"name": "BD Associate", "type": "singleLineText"},
        ] + [{"name": c, "type": "number", "options": {"precision": 0}}
             for c in COLUMNS]
          + [{"name": "Updated At", "type": "singleLineText"}],
    }
    resp = requests.post(f"{META}/{base_id}/tables", json=defn, headers=headers, timeout=30)
    if resp.status_code in (200, 201):
        print(f"[funnel] Created Airtable table {TABLE_NAME!r}")
        return True
    print(f"[funnel] ERROR creating {TABLE_NAME!r}: {resp.status_code} {resp.text[:300]}")
    return False


def push_to_airtable(grid: dict) -> None:
    from utils.airtable_client import AirtableClient
    base_id = os.environ["AIRTABLE_BASE_ID"]
    headers = {"Authorization": f"Bearer {os.environ['AIRTABLE_PAT']}",
               "Content-Type": "application/json"}
    if not ensure_table(base_id, headers):
        return

    at = AirtableClient(TABLE_NAME)
    n  = at.build_cache("Key")
    print(f"[funnel] {n} existing row(s) in {TABLE_NAME!r}")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    tally = defaultdict(int)
    for (rep, email, month), counts in sorted(grid.items()):
        key = f"{rep} | {month}"
        action, _ = at.upsert(
            "Key", key,
            {"Key": key, "Month": month, "BD Email": email, "BD Associate": rep,
             **{c: counts[c] for c in COLUMNS}, "Updated At": stamp},
            stamp, updated_at_field="")   # always refresh: counts move over time
        tally[action] += 1
    at.flush()
    print(f"[funnel] Airtable: created={tally['created']} updated={tally['updated']} "
          f"skipped={tally['skipped']}")


def ensure_daily_table(base_id: str, headers: dict) -> bool:
    r = requests.get(f"{META}/{base_id}/tables", headers=headers, timeout=30)
    r.raise_for_status()
    if any(t["name"] == DAILY_TABLE_NAME for t in r.json().get("tables", [])):
        print(f"[funnel] Airtable table {DAILY_TABLE_NAME!r} already exists")
        return True
    defn = {
        "name": DAILY_TABLE_NAME,
        "description": ("One row per BD associate per DAY. Rows sum over any "
                        "date range — use this for week-on-week and "
                        "month-on-month charts. For DISTINCT account counts in "
                        "a month use 'BD Company Funnel' instead, which "
                        "deliberately gives a lower number. Built by "
                        "scripts/bd_company_funnel.py."),
        "fields": [
            {"name": "Key",          "type": "singleLineText"},  # "<rep> | YYYY-MM-DD"
            {"name": "Date",         "type": "singleLineText"},  # YYYY-MM-DD, sorts as text
            {"name": "Week",         "type": "singleLineText"},  # YYYY-Www, for WoW grouping
            {"name": "Month",        "type": "singleLineText"},  # YYYY-MM, for MoM grouping
            {"name": "BD Email",     "type": "singleLineText"},
            {"name": "BD Associate", "type": "singleLineText"},
        ] + [{"name": c, "type": "number", "options": {"precision": 0}}
             for c in COLUMNS]
          + [{"name": "Updated At", "type": "singleLineText"}],
    }
    resp = requests.post(f"{META}/{base_id}/tables", json=defn, headers=headers, timeout=30)
    if resp.status_code in (200, 201):
        print(f"[funnel] Created Airtable table {DAILY_TABLE_NAME!r}")
        return True
    print(f"[funnel] ERROR creating {DAILY_TABLE_NAME!r}: {resp.status_code} {resp.text[:300]}")
    return False


def push_daily(day_grid: dict) -> None:
    from utils.airtable_client import AirtableClient
    base_id = os.environ["AIRTABLE_BASE_ID"]
    headers = {"Authorization": f"Bearer {os.environ['AIRTABLE_PAT']}",
               "Content-Type": "application/json"}
    if not ensure_daily_table(base_id, headers):
        return

    cutoff = (datetime.now(timezone.utc).date()
              - timedelta(days=DAILY_HISTORY_DAYS)).isoformat()
    rows = {k: v for k, v in day_grid.items() if k[2] >= cutoff}
    dropped = len(day_grid) - len(rows)

    at = AirtableClient(DAILY_TABLE_NAME)
    n  = at.build_cache("Key")
    print(f"[funnel] {n} existing row(s) in {DAILY_TABLE_NAME!r}"
          + (f" ({dropped} row(s) older than {cutoff} not pushed)" if dropped else ""))
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    tally = defaultdict(int)
    for (rep, email, day), counts in sorted(rows.items()):
        key = f"{rep} | {day}"
        action, _ = at.upsert(
            "Key", key,
            {"Key": key, "Date": day, "Week": _iso_week(day), "Month": day[:7],
             "BD Email": email, "BD Associate": rep,
             **{c: counts[c] for c in COLUMNS}, "Updated At": stamp},
            stamp, updated_at_field="")   # always refresh: a day can gain calls
        tally[action] += 1
    at.flush()
    print(f"[funnel] {DAILY_TABLE_NAME}: created={tally['created']} "
          f"updated={tally['updated']} skipped={tally['skipped']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="print the table, write nothing to Airtable")
    ap.add_argument("--skip-daily", action="store_true",
                    help="refresh the monthly table only")
    args = ap.parse_args()

    month_grid, day_grid, _ = build_funnel(KylasClient())
    print_table(month_grid)
    if args.dry_run:
        print(f"[funnel] dry run — nothing written "
              f"({len(month_grid)} month rows, {len(day_grid)} day rows)")
        return 0
    push_to_airtable(month_grid)
    if not args.skip_daily:
        push_daily(day_grid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
