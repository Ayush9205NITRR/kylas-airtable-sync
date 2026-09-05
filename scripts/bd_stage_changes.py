#!/usr/bin/env python3
"""
BD Stage Changes — one Airtable row per contact stage move, from → to.

Kylas keeps only a contact's CURRENT stage and no change timestamps, so the
only way to know a stage moved is to remember what it was and compare. This
diffs today's read against state/contact_stage.json (kept in git) and appends
a row for every contact whose stage differs.

    Date        Contact      BD Associate   Previous Stage        Current Stage
    2026-09-02  Priya Sharma Anjali Athya   CNC (Could Not…) - 1  Follow-up (1)

Two tables come out of one pass:

  BD Stage Changes        the change log — the audit trail, one row per move
  BD Stage Change Daily   per associate per day: how many moves, and how many
                          were forward vs backward through the pipeline order

WHAT THIS COUNTS, AND WHAT IT DOES NOT
────────────────────────────────────────────────────────────────────────────
It counts PIPELINE PROGRESSION, not calls. The distinction matters because the
two diverge in both directions:

  * A call that leaves the stage alone is invisible here. Ringing someone who
    stays at "CNC - 1" is real work that produces no row.
  * A stage can move without a call — a bulk edit or an import does it too.
  * Resolution is one RUN, not one call: two moves between runs collapse into
    a single row showing the net start -> end.

Real call volume is in Kylas /call-logs, which carries the caller, timestamp
and duration. Do not relabel these columns "calls".

Forward/backward is judged by config/account_pipeline_order.json (rank 1 best),
the same order the Account Pipeline Stage rollup and the funnel thresholds use.

    python scripts/bd_stage_changes.py             # diff, write, save snapshot
    python scripts/bd_stage_changes.py --dry-run   # report only, touch nothing
"""
import argparse
import importlib.util
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.kylas_client import KylasClient                     # noqa: E402
from utils.account_pipeline import load_order, _company_id      # noqa: E402
from utils import stage_history                                 # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name.replace(".py", ""), os.path.join(_HERE, name))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


funnel = _load("bd_company_funnel.py")

META        = "https://api.airtable.com/v0/meta/bases"
LOG_TABLE   = "BD Stage Changes"
DAILY_TABLE = "BD Stage Change Daily"

HISTORY_DAYS = funnel.DAILY_HISTORY_DAYS

DAILY_METRICS = ["Stage Changes", "Forward Moves", "Backward Moves"]


def read_contacts(kylas, all_owners: bool = False) -> dict:
    """{contact_id: {stage, owner, email, company, name}} for roster owners."""
    from utils.bd_metrics import refresh_stage_map, contact_stage
    refresh_stage_map(kylas)      # bare option ids must resolve to real labels

    user_map = funnel._build_user_map(kylas)
    roster   = set() if all_owners else funnel.bd_roster()

    print("[stage] Fetching contacts from Kylas...")
    contacts = kylas._search_all(
        "contact",
        fields=["id", "name", "company", "ownedBy", "ownerId", "updatedAt",
                "customFieldValues"],
    )
    print(f"[stage] {len(contacts)} contacts fetched")

    out, off_roster = {}, 0
    for ct in contacts:
        stage = contact_stage(ct)
        if not stage:
            continue
        name, email = funnel._owner(ct, user_map)
        if roster and email.strip().lower() not in roster:
            off_roster += 1
            continue
        co = ct.get("company")
        out[str(ct["id"])] = {
            "stage": stage,
            "owner": name,
            "email": email,
            "company": (co.get("name", "") if isinstance(co, dict) else ""),
            "name": ct.get("name") or "",
        }
    if off_roster:
        print(f"[stage] {off_roster} contact(s) skipped — owner off-roster")
    return out


def summarise(changes: list, order) -> dict:
    """(owner, email, date) -> {metric: n}, split forward vs backward."""
    grid = defaultdict(lambda: dict.fromkeys(DAILY_METRICS, 0))
    for c in changes:
        cell = grid[(c["owner"], c["email"], c["date"])]
        cell["Stage Changes"] += 1
        r_from, r_to = order.rank_of(c["from"]), order.rank_of(c["to"])
        # rank 1 is best, so a SMALLER rank is forward progress. A move
        # involving an unranked stage counts as a change but takes no side.
        if r_from and r_to:
            if r_to < r_from:
                cell["Forward Moves"] += 1
            elif r_to > r_from:
                cell["Backward Moves"] += 1
    return grid


def _ensure(base_id: str, headers: dict, name: str, fields: list) -> bool:
    r = requests.get(f"{META}/{base_id}/tables", headers=headers, timeout=30)
    r.raise_for_status()
    if any(t["name"] == name for t in r.json().get("tables", [])):
        print(f"[stage] Airtable table {name!r} already exists")
        return True
    resp = requests.post(f"{META}/{base_id}/tables",
                         json={"name": name, "fields": fields},
                         headers=headers, timeout=30)
    if resp.status_code in (200, 201):
        print(f"[stage] Created Airtable table {name!r}")
        return True
    print(f"[stage] ERROR creating {name!r}: {resp.status_code} {resp.text[:300]}")
    return False


def push(changes: list, grid: dict) -> None:
    from utils.airtable_client import AirtableClient
    base_id = os.environ["AIRTABLE_BASE_ID"]
    headers = {"Authorization": f"Bearer {os.environ['AIRTABLE_PAT']}",
               "Content-Type": "application/json"}
    stamp  = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    today  = datetime.now(timezone.utc).date().isoformat()
    cutoff = (datetime.now(timezone.utc).date()
              - timedelta(days=HISTORY_DAYS)).isoformat()

    text = "singleLineText"
    log_fields = [{"name": n, "type": text} for n in
                  ("Key", "Date", "Week", "Month", "Contact Id", "Contact",
                   "Company", "BD Associate", "BD Email",
                   "Previous Stage", "Current Stage", "Direction", "Updated At")]
    if _ensure(base_id, headers, LOG_TABLE, log_fields) and changes:
        at = AirtableClient(LOG_TABLE)
        n  = at.build_cache("Key")
        print(f"[stage] {n} existing row(s) in {LOG_TABLE!r}")
        order = load_order()
        tally = defaultdict(int)
        for c in changes:
            # One row per contact per day: a second move on the same day
            # overwrites, since only the net from -> to is knowable anyway.
            key = f"{c['contact_id']} | {c['date']}"
            r_from, r_to = order.rank_of(c["from"]), order.rank_of(c["to"])
            direction = ("Forward" if r_from and r_to and r_to < r_from else
                         "Backward" if r_from and r_to and r_to > r_from else "—")
            action, _ = at.upsert(
                "Key", key,
                {"Key": key, "Date": c["date"], "Week": funnel._iso_week(c["date"]),
                 "Month": c["date"][:7], "Contact Id": c["contact_id"],
                 "Contact": c["name"], "Company": c["company"],
                 "BD Associate": c["owner"], "BD Email": c["email"],
                 "Previous Stage": c["from"], "Current Stage": c["to"],
                 "Direction": direction, "Updated At": stamp},
                stamp, updated_at_field="")
            tally[action] += 1
        at.flush()
        print(f"[stage] {LOG_TABLE}: created={tally['created']} "
              f"updated={tally['updated']} skipped={tally['skipped']}")
        funnel.prune_expired(LOG_TABLE, cutoff)

    daily_fields = [{"name": n, "type": text} for n in
                    ("Key", "Date", "Week", "Month", "BD Associate", "BD Email",
                     "Updated At")] + \
                   [{"name": m, "type": "number", "options": {"precision": 0}}
                    for m in DAILY_METRICS]
    if _ensure(base_id, headers, DAILY_TABLE, daily_fields) and grid:
        at = AirtableClient(DAILY_TABLE)
        at.build_cache("Key")
        tally, frozen = defaultdict(int), 0
        for (rep, email, day), counts in sorted(grid.items()):
            key = f"{rep} | {day}"
            # Closed days are a record, not a projection — see
            # bd_company_funnel._is_closed.
            if funnel._is_closed(day, today) and key in at._cache:
                frozen += 1
                continue
            action, _ = at.upsert(
                "Key", key,
                {"Key": key, "Date": day, "Week": funnel._iso_week(day),
                 "Month": day[:7], "BD Associate": rep, "BD Email": email,
                 **counts, "Updated At": stamp},
                stamp, updated_at_field="")
            tally[action] += 1
        at.flush()
        print(f"[stage] {DAILY_TABLE}: created={tally['created']} "
              f"updated={tally['updated']} frozen={frozen}")
        funnel.prune_expired(DAILY_TABLE, cutoff)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="report the diff, write nothing and save no snapshot")
    ap.add_argument("--all-owners", action="store_true",
                    help="include owners outside the BD roster (diagnostic)")
    args = ap.parse_args()

    today   = datetime.now(timezone.utc).date().isoformat()
    order   = load_order()
    current = read_contacts(KylasClient(), all_owners=args.all_owners)
    prev    = stage_history.load()

    snapshot, changes, stats = stage_history.diff(prev, current, today)

    if not prev:
        print(f"[stage] BASELINE established for {stats['new']} contact(s) — "
              f"no changes to report on a first run. Movement is detected from "
              f"the next run onward.")
    else:
        print(f"[stage] {stats['changed']} stage change(s), "
              f"{stats['unchanged']} unchanged, {stats['new']} new contact(s), "
              f"{stats['carried']} carried")
        grid = summarise(changes, order)
        for (rep, _e, day), c in sorted(grid.items()):
            print(f"[stage]   {day}  {rep:<20} {c['Stage Changes']:>3} moves "
                  f"({c['Forward Moves']} fwd, {c['Backward Moves']} back)")

    if args.dry_run:
        print("[stage] dry run — nothing written, snapshot not saved")
        return 0

    push(changes, summarise(changes, order) if prev else {})
    stage_history.save(snapshot, today=today)
    print(f"[stage] snapshot saved: {len(snapshot)} contact(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
