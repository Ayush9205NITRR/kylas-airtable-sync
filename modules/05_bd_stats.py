"""
Persist BD daily metrics to Airtable.

Tables written:
  BD Daily Stats       — per owner per slot per day (drives daily email)
  Account Activity Log — per company per day (drives account consumption dashboard)
"""
import argparse
import os
import sys
import time
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.airtable_client import AirtableClient
from utils.logger import SyncLogger
from utils.bd_metrics import BD_KEYS

# Rows older than this are deleted automatically on each run (keeps tables small)
RETENTION_DAYS = 90

FIELD = {
    "attempted":  "Attempted",
    "connected":  "Connected",
    "dcb":        "Discovery Calls",
    "sql":        "SQL",
    "mql":        "MQL",
    "activation": "Activation",
}
W1_FIELD = {k: f"W1 {v}" for k, v in FIELD.items()}

ACC_FIELD = {
    "pocs":       "POCs Tapped",
    "attempted":  "Attempted POCs",
    "connected":  "Connected POCs",
    "dcb":        "DCB POCs",
    "sql":        "SQL POCs",
    "mql":        "MQL POCs",
    "activation": "Activation POCs",
}

_ZERO = lambda: {k: 0 for k in BD_KEYS}


def _prune_old(tbl: AirtableClient, label: str, retention_days: int = RETENTION_DAYS) -> int:
    """Delete rows whose Date field is older than retention_days. tbl._cache must be loaded."""
    cutoff = (date.today() - timedelta(days=retention_days)).isoformat()
    old_ids = [
        r["id"] for r in tbl._cache.values()
        if str(r["fields"].get("Date", "9999-12-31")) < cutoff
    ]
    if not old_ids:
        return 0
    for i in range(0, len(old_ids), 10):
        time.sleep(0.2)
        tbl.table.batch_delete(old_ids[i:i + 10])
    print(f"[BD Stats] {label}: pruned {len(old_ids)} rows older than {retention_days} days")
    return len(old_ids)


def _write_bd_daily(bd_metrics: dict, slot: str, today: str) -> dict:
    """Write per-owner BD stats; return enriched dict with W1/W2 breakdown.

    bd_metrics is a full daily SNAPSHOT — every contact whose "Last Called At"
    is today (worked by its owner). Each sync recomputes the whole snapshot, so
    we OVERWRITE today's row with the latest count rather than accumulating.
    To stay safe against a re-run with a narrower fetch window, we never let the
    stored number regress: total = max(this run's snapshot, what's already
    stored today / the morning W1 snapshot).
    """
    try:
        tbl    = AirtableClient("BD Daily Stats")
        cached = tbl.build_cache("Stat Key")
        print(f"[BD Stats] BD Daily Stats cache: {cached} rows")
    except Exception as e:
        print(f"[BD Stats] WARNING: BD Daily Stats not accessible ({e})")
        print("[BD Stats] Run 'Setup BD Dashboard' workflow first")
        return {owner: {**m, "w1": _ZERO(), "w2": dict(m)} for owner, m in bd_metrics.items()}

    # Frozen morning snapshot (W1) from the first_half row(s) written at 1:30 PM
    w1_stored = {}
    # Totals already stored for today in THIS slot's row — the accumulator base
    stored = {}
    for rec in tbl._cache.values():
        f = rec["fields"]
        if f.get("Date") != today:
            continue
        owner = f.get("Owner")
        if not owner:
            continue
        if f.get("Slot") == "first_half":
            w1_stored[owner] = {k: int(f.get(W1_FIELD[k], 0) or 0) for k in BD_KEYS}
        if f.get("Slot") == slot:
            stored[owner] = {k: int(f.get(FIELD[k], 0) or 0) for k in BD_KEYS}

    # Process everyone with activity today: detected this run, already stored,
    # or present in the morning snapshot. This guarantees a member who worked
    # earlier still gets emailed even when this run's delta is empty.
    all_owners = set(bd_metrics) | set(stored) | set(w1_stored)

    result = {}
    for owner in sorted(all_owners):
        snapshot = bd_metrics.get(owner, _ZERO())   # full snapshot from THIS run
        w1       = w1_stored.get(owner, _ZERO())
        # Floor = the highest already recorded today for this slot (or the
        # morning W1 snapshot when seeding a full_day row). Snapshot overwrites,
        # but never regresses below the floor — so a late/narrow re-run can't
        # zero out numbers, and a normal re-run just refreshes without doubling.
        floor = stored.get(owner) or (w1 if slot == "full_day" else _ZERO())
        total = {k: max(snapshot.get(k, 0), floor.get(k, 0)) for k in BD_KEYS}

        stat_key = f"{today}|{slot}|{owner}"
        fields = {"Stat Key": stat_key, "Date": today, "Owner": owner, "Slot": slot}
        for k, fname in FIELD.items():
            fields[fname] = total[k]
        if slot == "first_half":
            for k, fname in W1_FIELD.items():
                fields[fname] = total[k]
        tbl.upsert("Stat Key", stat_key, fields, updated_at="", updated_at_field="")
        print(f"[BD Stats] {owner}: attempted={total['attempted']}"
              f"  connected={total['connected']}  dcb={total['dcb']}")

        w2 = {k: max(0, total[k] - w1.get(k, 0)) for k in BD_KEYS}
        result[owner] = {**total, "w1": w1, "w2": w2}

    tbl.flush()
    _prune_old(tbl, "BD Daily Stats")
    return result


def _write_account_activity(account_activity: dict, company_id_map: dict, today: str):
    """Write one row per company to Account Activity Log using pre-computed account_activity."""
    try:
        tbl    = AirtableClient("Account Activity Log")
        cached = tbl.build_cache("Stat Key")
        print(f"[BD Stats] Account Activity Log cache: {cached} rows")
    except Exception as e:
        print(f"[BD Stats] WARNING: Account Activity Log not accessible ({e})")
        return

    for co_id, data in account_activity.items():
        stat_key = f"{today}|{co_id}"
        existing_f = (tbl._cache.get(stat_key) or {}).get("fields", {})
        # Merge owners from any prior sync today
        prior_owners = set(filter(None, (existing_f.get("BD Owners") or "").split(", ")))
        all_owners = prior_owners | data.get("owners", set())
        owners_str = ", ".join(sorted(all_owners))
        fields = {
            "Stat Key":          stat_key,
            "Date":              today,
            "Company Name":      data["company_name"],
            "Kylas Company Id":  co_id,
            "BD Owners":         owners_str,
        }
        for k, fname in ACC_FIELD.items():
            prior_val = int(existing_f.get(fname, 0) or 0)
            fields[fname] = prior_val + data.get(k, 0)
        airtable_id = company_id_map.get(co_id, "")
        if airtable_id:
            fields["Company"] = [airtable_id]
        tbl.upsert("Stat Key", stat_key, fields, updated_at="", updated_at_field="")

    tbl.flush()
    _prune_old(tbl, "Account Activity Log")
    print(f"[BD Stats] Account Activity Log: {len(account_activity)} companies written for {today}")


def run(bd_metrics: dict, account_activity: dict, company_id_map: dict,
        slot: str, logger: SyncLogger = None) -> dict:
    """
    Write BD Daily Stats + Account Activity Log for today's sync run.

    Returns enriched bd_metrics dict with w1/w2 window breakdown.
    """
    today = date.today().isoformat()

    bd_enriched = _write_bd_daily(bd_metrics, slot, today)
    _write_account_activity(account_activity, company_id_map, today)

    print(f"[BD Stats] Done — slot={slot}  date={today}")
    return bd_enriched


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot", default="first_half")
    args = parser.parse_args()
    from dotenv import load_dotenv; load_dotenv()

    test_metrics = {
        "Ayush":   {"attempted": 15, "connected": 8, "dcb": 2, "sql": 1, "mql": 3, "activation": 1},
        "Bhaumik": {"attempted": 10, "connected": 5, "dcb": 1, "sql": 0, "mql": 2, "activation": 1},
    }
    print(run(test_metrics, [], {}, args.slot))
