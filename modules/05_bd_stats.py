"""Persist BD daily metrics per owner to Airtable BD Daily Stats table."""
import argparse
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.airtable_client import AirtableClient
from utils.logger import SyncLogger

METRICS = ["attempted", "connected", "dcb", "sql", "mql", "activation"]

FIELD = {
    "attempted":  "Attempted",
    "connected":  "Connected",
    "dcb":        "Discovery Calls",
    "sql":        "SQL",
    "mql":        "MQL",
    "activation": "Activation",
}

W1_FIELD = {k: f"W1 {v}" for k, v in FIELD.items()}

_ZERO = lambda: {k: 0 for k in METRICS}


def run(bd_metrics: dict, slot: str, logger: SyncLogger = None) -> dict:
    """
    Write BD daily stats to Airtable and return enriched metrics with W1/W2 breakdown.

    Args:
        bd_metrics : {owner: {attempted, connected, dcb, sql, mql, activation}}
        slot       : "first_half" or "full_day"

    Returns:
        {owner: {<same metrics>, w1: {<metrics>}, w2: {<metrics>}}}
        w1 = first_half counts (from stored row, if full_day run)
        w2 = full_day counts minus w1
    """
    today = date.today().isoformat()

    try:
        tbl    = AirtableClient("BD Daily Stats")
        cached = tbl.build_cache("Stat Key")
        print(f"[BD Stats] Cache: {cached} existing rows")
    except Exception as e:
        print(f"[BD Stats] WARNING: BD Daily Stats not accessible ({e})")
        print("[BD Stats] Run 'Setup BD Dashboard' workflow first")
        return {owner: {**m, "w1": _ZERO(), "w2": dict(m)} for owner, m in bd_metrics.items()}

    # On full_day run, read stored W1 fields (written during first_half run)
    w1_stored = {}
    if slot == "full_day":
        for owner in bd_metrics:
            w1_key = f"{today}|first_half|{owner}"
            rec    = tbl._cache.get(w1_key)
            if rec:
                w1_stored[owner] = {k: rec["fields"].get(W1_FIELD[k], 0) for k in METRICS}

    # Write / update today's row for each owner
    for owner, metrics in bd_metrics.items():
        stat_key = f"{today}|{slot}|{owner}"
        fields = {
            "Stat Key": stat_key,
            "Date":     today,
            "Owner":    owner,
            "Slot":     slot,
        }
        for k, fname in FIELD.items():
            fields[fname] = metrics.get(k, 0)
        # Snapshot W1 fields on first_half run so full_day can compute W2
        if slot == "first_half":
            for k, fname in W1_FIELD.items():
                fields[fname] = metrics.get(k, 0)

        tbl.upsert("Stat Key", stat_key, fields, updated_at="", updated_at_field="")
        print(
            f"[BD Stats] {owner}: attempted={metrics.get('attempted',0)}"
            f"  connected={metrics.get('connected',0)}"
            f"  dcb={metrics.get('dcb',0)}"
        )

    tbl.flush()
    print(f"[BD Stats] Done — {len(bd_metrics)} owner(s) written for slot={slot}")

    # Return enriched dict with W1 / W2 breakdown
    result = {}
    for owner, metrics in bd_metrics.items():
        w1 = w1_stored.get(owner, _ZERO())
        w2 = {k: max(0, metrics.get(k, 0) - w1.get(k, 0)) for k in METRICS}
        result[owner] = {**metrics, "w1": w1, "w2": w2}
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot", default="first_half")
    args = parser.parse_args()
    from dotenv import load_dotenv; load_dotenv()

    test_metrics = {
        "Ayush": {"attempted": 15, "connected": 8, "dcb": 2, "sql": 1, "mql": 3, "activation": 1},
        "Bhaumik": {"attempted": 10, "connected": 5, "dcb": 1, "sql": 0, "mql": 2, "activation": 1},
    }
    print(run(test_metrics, args.slot))
