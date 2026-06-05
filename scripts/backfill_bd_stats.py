"""
Backfill BD Daily Stats and Account Activity Log from a start date.

For each contact with updatedAt >= start_date, uses the CURRENT pipeline stage
(approximation — accurate for contacts not touched since their last update).

Run ONCE after "Setup BD Dashboard" to populate historical data.
"""
import argparse
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.kylas_client import KylasClient
from utils.airtable_client import AirtableClient
from utils.bd_metrics import BD_KEYS, contact_stage, classify_bd, company_info

FIELD = {
    "attempted":  "Attempted",
    "connected":  "Connected",
    "dcb":        "Discovery Calls",
    "sql":        "SQL",
    "mql":        "MQL",
    "activation": "Activation",
}
W1_FIELD  = {k: f"W1 {v}" for k, v in FIELD.items()}
ACC_FIELD = {
    "pocs":       "POCs Tapped",
    "attempted":  "Attempted POCs",
    "connected":  "Connected POCs",
    "dcb":        "DCB POCs",
    "sql":        "SQL POCs",
    "mql":        "MQL POCs",
    "activation": "Activation POCs",
}

DEFAULT_SLOT = "full_day"


def _extract_date(updated_at: str) -> str:
    """Return YYYY-MM-DD from ISO timestamp, or '' if missing."""
    if not updated_at:
        return ""
    return updated_at[:10]


def _owner_name(raw: dict, user_map: dict) -> str:
    ob = raw.get("ownedBy")
    if isinstance(ob, dict):
        name = ob.get("name") or f"{ob.get('firstName', '')} {ob.get('lastName', '')}".strip()
        if name:
            return name
    oid = raw.get("ownerId")
    if oid and user_map:
        return user_map.get(int(oid)) or user_map.get(str(oid)) or "Unassigned"
    return "Unassigned"


def run(start_date: str = "2026-06-01"):
    print(f"=== BD Stats Backfill from {start_date} ===\n")

    kylas    = KylasClient()
    user_map = {}
    try:
        user_map = kylas.get_users()
        print(f"Loaded {len(user_map)} users\n")
    except Exception as e:
        print(f"WARNING: user lookup failed ({e})\n")

    # Fetch company id_map for linking
    company_id_map = {}
    try:
        tbl_co = AirtableClient("Companies")
        tbl_co.build_cache("Kylas Company Id")
        company_id_map = {kid: rec["id"] for kid, rec in tbl_co._cache.items()}
        print(f"Loaded {len(company_id_map)} company records for linking\n")
    except Exception as e:
        print(f"WARNING: Companies table not accessible ({e}) — links will be skipped\n")

    print("Fetching all contacts from Kylas...")
    contacts = kylas.get_contacts()
    print(f"Fetched {len(contacts)} contacts\n")

    # Group by (date, owner) for BD Daily Stats
    # Group by (date, company_id) for Account Activity Log
    daily_owner   = defaultdict(lambda: defaultdict(lambda: {k: 0 for k in BD_KEYS}))
    daily_account = defaultdict(lambda: defaultdict(lambda: {"company_name": "", "pocs": 0, **{k: 0 for k in BD_KEYS}}))

    skipped = 0
    for ct in contacts:
        d = _extract_date(ct.get("updatedAt", ""))
        if not d or d < start_date:
            skipped += 1
            continue

        owner = _owner_name(ct, user_map)
        stage = contact_stage(ct)
        cats  = classify_bd(stage)

        bd = daily_owner[d][owner]
        for key in BD_KEYS:
            if cats[key]:
                bd[key] += 1

        co_id, co_name = company_info(ct)
        if co_id:
            acc = daily_account[d][co_id]
            if not acc["company_name"] and co_name:
                acc["company_name"] = co_name
            acc["pocs"] += 1
            for key in BD_KEYS:
                if cats[key]:
                    acc[key] += 1

    print(f"Skipped {skipped} contacts before {start_date}")
    print(f"Processing {len(daily_owner)} dates, {sum(len(v) for v in daily_owner.values())} owner-day rows\n")

    # ── Write BD Daily Stats ──────────────────────────────────────────────────
    print("Writing BD Daily Stats...")
    try:
        tbl_bd = AirtableClient("BD Daily Stats")
        tbl_bd.build_cache("Stat Key")
    except Exception as e:
        print(f"ERROR: BD Daily Stats not accessible ({e})")
        print("Run 'Setup BD Dashboard' workflow first, then re-run backfill.")
        return

    for d, owners in sorted(daily_owner.items()):
        for owner, metrics in owners.items():
            stat_key = f"{d}|{DEFAULT_SLOT}|{owner}"
            fields = {"Stat Key": stat_key, "Date": d, "Owner": owner, "Slot": DEFAULT_SLOT}
            for k, fname in FIELD.items():
                fields[fname] = metrics.get(k, 0)
            # Backfill: no window split, so W1 = 0 (don't overwrite live sync W1 if exists)
            tbl_bd.upsert("Stat Key", stat_key, fields, updated_at="", updated_at_field="")

    tbl_bd.flush()
    print(f"BD Daily Stats: wrote {sum(len(v) for v in daily_owner.values())} rows\n")

    # ── Write Account Activity Log ────────────────────────────────────────────
    print("Writing Account Activity Log...")
    try:
        tbl_acc = AirtableClient("Account Activity Log")
        tbl_acc.build_cache("Stat Key")
    except Exception as e:
        print(f"ERROR: Account Activity Log not accessible ({e})")
        print("Run 'Setup BD Dashboard' workflow first, then re-run backfill.")
        return

    total_acc = 0
    for d, companies in sorted(daily_account.items()):
        for co_id, data in companies.items():
            stat_key = f"{d}|{co_id}"
            fields = {
                "Stat Key":        stat_key,
                "Date":            d,
                "Company Name":    data["company_name"],
                "Kylas Company Id": co_id,
            }
            for k, fname in ACC_FIELD.items():
                fields[fname] = data.get(k, 0)
            at_id = company_id_map.get(co_id, "")
            if at_id:
                fields["Company"] = [at_id]
            tbl_acc.upsert("Stat Key", stat_key, fields, updated_at="", updated_at_field="")
            total_acc += 1

    tbl_acc.flush()
    print(f"Account Activity Log: wrote {total_acc} rows\n")
    print("=== Backfill complete ===")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill BD stats from a start date")
    parser.add_argument("--start-date", default="2026-06-01",
                        help="ISO date (YYYY-MM-DD) to start backfill from (default: 2026-06-01)")
    args = parser.parse_args()

    from dotenv import load_dotenv; load_dotenv()
    run(start_date=args.start_date)
