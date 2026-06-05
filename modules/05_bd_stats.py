"""
Persist BD daily metrics to Airtable.

Tables written:
  BD Daily Stats       — per owner per slot per day (drives daily email)
  Account Activity Log — per company per day (drives account consumption dashboard)
"""
import argparse
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.airtable_client import AirtableClient
from utils.logger import SyncLogger
from utils.bd_metrics import BD_KEYS

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


def _write_bd_daily(bd_metrics: dict, slot: str, today: str) -> dict:
    """Write per-owner BD stats; return enriched dict with W1/W2 breakdown."""
    try:
        tbl    = AirtableClient("BD Daily Stats")
        cached = tbl.build_cache("Stat Key")
        print(f"[BD Stats] BD Daily Stats cache: {cached} rows")
    except Exception as e:
        print(f"[BD Stats] WARNING: BD Daily Stats not accessible ({e})")
        print("[BD Stats] Run 'Setup BD Dashboard' workflow first")
        return {owner: {**m, "w1": _ZERO(), "w2": dict(m)} for owner, m in bd_metrics.items()}

    # On full_day run: read W1 snapshot written during first_half run
    w1_stored = {}
    if slot == "full_day":
        for owner in bd_metrics:
            rec = tbl._cache.get(f"{today}|first_half|{owner}")
            if rec:
                w1_stored[owner] = {k: rec["fields"].get(W1_FIELD[k], 0) for k in BD_KEYS}

    for owner, metrics in bd_metrics.items():
        stat_key = f"{today}|{slot}|{owner}"
        fields = {"Stat Key": stat_key, "Date": today, "Owner": owner, "Slot": slot}
        for k, fname in FIELD.items():
            fields[fname] = metrics.get(k, 0)
        if slot == "first_half":
            for k, fname in W1_FIELD.items():
                fields[fname] = metrics.get(k, 0)
        tbl.upsert("Stat Key", stat_key, fields, updated_at="", updated_at_field="")
        print(f"[BD Stats] {owner}: attempted={metrics.get('attempted',0)}"
              f"  connected={metrics.get('connected',0)}  dcb={metrics.get('dcb',0)}")

    tbl.flush()

    result = {}
    for owner, metrics in bd_metrics.items():
        w1 = w1_stored.get(owner, _ZERO())
        w2 = {k: max(0, metrics.get(k, 0) - w1.get(k, 0)) for k in BD_KEYS}
        result[owner] = {**metrics, "w1": w1, "w2": w2}
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
        owners_str = ", ".join(sorted(data.get("owners", set())))
        fields = {
            "Stat Key":          stat_key,
            "Date":              today,
            "Company Name":      data["company_name"],
            "Kylas Company Id":  co_id,
            "BD Owners":         owners_str,
        }
        for k, fname in ACC_FIELD.items():
            fields[fname] = data.get(k, 0)
        airtable_id = company_id_map.get(co_id, "")
        if airtable_id:
            fields["Company"] = [airtable_id]
        tbl.upsert("Stat Key", stat_key, fields, updated_at="", updated_at_field="")

    tbl.flush()
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
