"""
BD Monthly Matrix — per-rep, per-month funnel counts, written to Airtable.

One Airtable row per (BD rep × month) so the numbers can be filtered by month
in Airtable directly. Creates the table on first run.

Columns
───────
  Call Attempted        — bd_metrics: any stage except "Yet to Be Mined"/blank
  Call Connected        — bd_metrics.CONNECTED_STAGES
  Discovery Call Booked — SQL, Discovery Call Booked, Offsite Delayed,
                          Discovery Call No-Show, Reschedule Pending,
                          Closing Loops - Low Value
  Meeting Happened      — SQL, Closing Loops - Low Value, Offsite Delayed,
                          Discovery Call Done - Awaiting Client Inputs
  SQL                   — SQL (Sales Qualified Lead)

Attempted/Connected reuse the existing shared definitions in utils/bd_metrics
so this table can never drift from the daily BD emails. The two middle buckets
are spelled out above because they are specific to this report.

Month attribution
─────────────────
month = the month of the contact's "Last Called At" date. Contacts with an
EMPTY last-called date are skipped entirely (they have no month to sit in),
which is what makes the Month column safe to filter on.

READ THIS BEFORE TRUSTING TRENDS
────────────────────────────────
A Kylas contact carries ONE current stage and ONE last-called date, so this is
a SNAPSHOT attributed to the month of the most recent call — not a historical
activity log. Consequences:
  * A contact called in June and again in July counts ONLY in July.
  * A contact's CURRENT stage is counted, even if it reached that stage in a
    later month than the call. E.g. called in June, became SQL in August →
    counted as an SQL in June.
So a month's row changes retroactively as its contacts are re-called or moved
on. It answers "of the contacts last touched in month M, where do they stand
now?" — not "what happened during month M". True per-month history would need
stage-change timestamps, which Kylas does not expose on the contact record.

Run:  python scripts/bd_monthly_matrix.py            (write to Airtable)
      python scripts/bd_monthly_matrix.py --dry-run  (print table only)
"""
import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

TABLE_NAME = "BD Monthly Matrix"
META = "https://api.airtable.com/v0/meta/bases"

# Stage sets specific to this report. Kylas stores a plain hyphen "-"; the
# spec was written with an en-dash "–", so all comparisons run through
# _norm() which folds dash variants and case. Without that these silently
# match nothing and every count reads 0.
DCB_STAGES = {
    "SQL (Sales Qualified Lead)",
    "Discovery Call Booked",
    "Offsite Delayed",
    "Discovery Call No-Show",
    "Reschedule Pending",
    "Closing Loops - Low Value",
}
MEETING_STAGES = {
    "SQL (Sales Qualified Lead)",
    "Closing Loops - Low Value",
    "Offsite Delayed",
    "Discovery Call Done - Awaiting Client Inputs",
}

COLUMNS = ["Call Attempted", "Call Connected", "Discovery Call Booked",
           "Meeting Happened", "SQL"]


def _norm(s: str) -> str:
    """Fold dash variants + case so en-dash/em-dash spellings still match."""
    return (str(s or "").strip().lower()
            .replace("–", "-").replace("—", "-").replace("−", "-"))


_DCB_N     = {_norm(s) for s in DCB_STAGES}
_MEETING_N = {_norm(s) for s in MEETING_STAGES}


def _parse_lc(raw: str) -> str:
    """cfLastCalledAt → 'YYYY-MM-DD'. '' when absent/unparseable.

    Mirrors modules/06_account_health.py: Kylas returns either an ISO string
    or a display form like 'Jun 22, 2026 at 05:22 PM'.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    if raw[0].isdigit():
        return raw[:10]
    try:
        return datetime.strptime(raw.split(" at ")[0], "%b %d, %Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _owner_name(ct: dict) -> str:
    ob = ct.get("ownedBy") or {}
    if isinstance(ob, dict):
        return (ob.get("name")
                or f"{ob.get('firstName', '')} {ob.get('lastName', '')}".strip()
                or "Unassigned")
    return str(ob) if ob else "Unassigned"


def build_matrix(kylas) -> tuple:
    """Return ({(rep, month): {col: count}}, stats)."""
    from utils.bd_metrics import (refresh_stage_map, contact_stage,
                                  CONNECTED_STAGES, ATTEMPTED_EXCLUDE)
    refresh_stage_map(kylas)   # bare option ids must resolve to real labels

    connected_n = {_norm(s) for s in CONNECTED_STAGES}
    exclude_n   = {_norm(s) for s in ATTEMPTED_EXCLUDE}

    print("[matrix] Fetching contacts from Kylas...")
    contacts = kylas._search_all(
        "contact",
        fields=["id", "ownedBy", "updatedAt", "customFieldValues"],
    )
    print(f"[matrix] {len(contacts)} contacts fetched")

    grid = defaultdict(lambda: dict.fromkeys(COLUMNS, 0))
    no_lc = 0
    for ct in contacts:
        cf = ct.get("customFieldValues") or {}
        lc = _parse_lc(cf.get("cfLastCalledAt", ""))
        if not lc:
            no_lc += 1          # no month to attribute → excluded by design
            continue
        month = lc[:7]                     # YYYY-MM
        stage = _norm(contact_stage(ct))
        cell  = grid[(_owner_name(ct), month)]

        if stage and stage not in exclude_n:
            cell["Call Attempted"] += 1
        if stage in connected_n:
            cell["Call Connected"] += 1
        if stage in _DCB_N:
            cell["Discovery Call Booked"] += 1
        if stage in _MEETING_N:
            cell["Meeting Happened"] += 1
        if stage == _norm("SQL (Sales Qualified Lead)"):
            cell["SQL"] += 1

    stats = {"contacts": len(contacts), "no_last_called": no_lc,
             "counted": len(contacts) - no_lc, "rows": len(grid)}
    print(f"[matrix] {stats['counted']} contacts have a Last Called date "
          f"({no_lc} skipped as empty) → {len(grid)} rep×month rows")
    return grid, stats


def print_table(grid: dict) -> None:
    months = sorted({m for _, m in grid}, reverse=True)
    reps   = sorted({r for r, _ in grid})
    w = max([len(r) for r in reps] + [10])
    print(f"\n{'MONTH':<9} {'BD REP':<{w}} " + " ".join(f"{c:>21}" for c in COLUMNS))
    for m in months:
        tot = dict.fromkeys(COLUMNS, 0)
        for r in reps:
            if (r, m) not in grid:
                continue
            c = grid[(r, m)]
            print(f"{m:<9} {r:<{w}} " + " ".join(f"{c[k]:>21}" for k in COLUMNS))
            for k in COLUMNS:
                tot[k] += c[k]
        print(f"{'':<9} {'TOTAL':<{w}} " + " ".join(f"{tot[k]:>21}" for k in COLUMNS))
        print()


def ensure_table(base_id: str, headers: dict) -> bool:
    r = requests.get(f"{META}/{base_id}/tables", headers=headers, timeout=30)
    r.raise_for_status()
    if any(t["name"] == TABLE_NAME for t in r.json().get("tables", [])):
        print(f"[matrix] Airtable table {TABLE_NAME!r} already exists")
        return True
    defn = {
        "name": TABLE_NAME,
        "description": ("Per BD rep, per month funnel counts. Month = month of the "
                        "contact's Last Called date; contacts with no last-called "
                        "date are excluded. Snapshot of CURRENT stage, not history "
                        "— see scripts/bd_monthly_matrix.py."),
        "fields": [
            {"name": "Key",    "type": "singleLineText"},   # "<rep> | YYYY-MM"
            {"name": "BD Rep", "type": "singleLineText"},
            {"name": "Month",  "type": "singleLineText"},   # YYYY-MM, sorts + filters
        ] + [{"name": c, "type": "number", "options": {"precision": 0}}
             for c in COLUMNS]
          + [{"name": "Updated At", "type": "singleLineText"}],
    }
    resp = requests.post(f"{META}/{base_id}/tables", json=defn, headers=headers, timeout=30)
    if resp.status_code in (200, 201):
        print(f"[matrix] Created Airtable table {TABLE_NAME!r}")
        return True
    print(f"[matrix] ERROR creating {TABLE_NAME!r}: {resp.status_code} {resp.text[:300]}")
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
    print(f"[matrix] {n} existing row(s) in {TABLE_NAME!r}")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    tally = defaultdict(int)
    for (rep, month), counts in sorted(grid.items()):
        key = f"{rep} | {month}"
        action, _ = at.upsert(
            "Key", key,
            {"Key": key, "BD Rep": rep, "Month": month,
             **{c: counts[c] for c in COLUMNS}, "Updated At": stamp},
            stamp, updated_at_field="")   # always refresh: counts move over time
        tally[action] += 1
    at.flush()   # returns None; counts come from the upsert() actions above
    print(f"[matrix] Airtable: created={tally['created']} updated={tally['updated']} "
          f"skipped={tally['skipped']}")


def main():
    ap = argparse.ArgumentParser(description="Build the BD monthly matrix in Airtable")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the table only; write nothing to Airtable")
    args = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv()
    from utils.kylas_client import KylasClient

    grid, _ = build_matrix(KylasClient())
    print_table(grid)
    if args.dry_run:
        print("[matrix] DRY RUN — nothing written to Airtable")
        return
    push_to_airtable(grid)


if __name__ == "__main__":
    main()
