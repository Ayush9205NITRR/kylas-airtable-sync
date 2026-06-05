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
from utils.bd_metrics import BD_KEYS, contact_stage, classify_bd, company_info

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


def _write_account_activity(contacts: list, company_id_map: dict, today: str):
    """
    Write one row per (date, company) to Account Activity Log.
    Counts unique contacts (POCs) at each company updated on `today`.
    """
    try:
        tbl    = AirtableClient("Account Activity Log")
        cached = tbl.build_cache("Stat Key")
        print(f"[BD Stats] Account Activity Log cache: {cached} rows")
    except Exception as e:
        print(f"[BD Stats] WARNING: Account Activity Log not accessible ({e})")
        return

    # Group contacts updated on `today` by company
    accounts = {}
    for ct in contacts:
        if not (ct.get("updatedAt") or "").startswith(today):
            continue
        co_id, co_name = company_info(ct)
        if not co_id:
            continue
        stage = contact_stage(ct)
        cats  = classify_bd(stage)

        acc = accounts.setdefault(co_id, {
            "company_name": co_name,
            "pocs":         0,
            **{k: 0 for k in BD_KEYS},
        })
        acc["pocs"] += 1
        for key in BD_KEYS:
            if cats[key]:
                acc[key] += 1

    for co_id, data in accounts.items():
        stat_key = f"{today}|{co_id}"
        fields = {
            "Stat Key":       stat_key,
            "Date":           today,
            "Company Name":   data["company_name"],
            "Kylas Company Id": co_id,
        }
        for k, fname in ACC_FIELD.items():
            fields[fname] = data.get(k, 0)
        airtable_id = company_id_map.get(co_id, "")
        if airtable_id:
            fields["Company"] = [airtable_id]
        tbl.upsert("Stat Key", stat_key, fields, updated_at="", updated_at_field="")

    tbl.flush()
    print(f"[BD Stats] Account Activity Log: {len(accounts)} companies written for {today}")


def run(bd_metrics: dict, contacts: list, company_id_map: dict,
        slot: str, logger: SyncLogger = None) -> dict:
    """
    Write BD Daily Stats + Account Activity Log for today's sync run.

    Returns enriched bd_metrics dict with w1/w2 window breakdown.
    """
    today = date.today().isoformat()

    bd_enriched = _write_bd_daily(bd_metrics, slot, today)
    _write_account_activity(contacts, company_id_map, today)

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
