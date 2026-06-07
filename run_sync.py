#!/usr/bin/env python3
import argparse
import importlib.util
import os
import sys


def _load(filename: str):
    path = os.path.join(os.path.dirname(__file__), "modules", filename)
    spec = importlib.util.spec_from_file_location(filename, path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot", choices=["first_half", "full_day"], default="full_day")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test", action="store_true", help="Process only first 5 records per module")
    parser.add_argument("--full-sync", action="store_true",
                        help="Fetch all records (no time window). Default: incremental (last 72h)")
    parser.add_argument("--since", metavar="ISO_DATE",
                        help="Fetch records updated on/after this date, e.g. 2026-06-01 or 2026-06-01T00:00:00Z "
                             "(overrides --full-sync and the default 72h window)")
    args = parser.parse_args()

    from dotenv import load_dotenv
    load_dotenv()

    if args.dry_run:
        print("[DRY RUN] Modules that would run:")
        for m in ["01_company_sync.py", "02_contact_sync.py", "03_deal_sync.py",
                  "05_bd_stats.py", "04_email_alert.py"]:
            print(f"  - {m}")
        return

    sys.path.insert(0, os.path.dirname(__file__))
    from utils.logger import SyncLogger
    from utils.kylas_client import KylasClient
    from datetime import datetime, timezone, timedelta

    if args.since:
        raw = args.since.strip()
        if "T" not in raw:
            raw += "T00:00:00Z"
        elif not raw.endswith("Z") and "+" not in raw:
            raw += "Z"
        since = raw
        mode  = f"since {since}"
    elif args.full_sync:
        since = None
        mode  = "full-sync"
    else:
        since = (datetime.now(timezone.utc) - timedelta(hours=72)).strftime("%Y-%m-%dT%H:%M:%SZ")
        mode  = f"incremental (last 72h, since {since})"

    logger = SyncLogger()
    print(f"[run_sync] Run ID={logger.run_id}  slot={args.slot}  mode={mode}\n")

    # Build user ID→name map: config (always reliable) + API (augments/overrides)
    import json as _json
    user_map = {}
    try:
        _team_path = os.path.join(os.path.dirname(__file__), "config", "team.json")
        with open(_team_path) as _f:
            _cfg_users = _json.load(_f).get("kylas_users", {})
        user_map = {int(uid): name for uid, name in _cfg_users.items()}
        print(f"[run_sync] Loaded {len(user_map)} users from config/team.json")
    except Exception as e:
        print(f"[run_sync] WARNING: could not load config user map ({e})")

    try:
        api_users = KylasClient().get_users()
        user_map.update(api_users)
        print(f"[run_sync] Merged {len(api_users)} users from Kylas API → total {len(user_map)}\n")
    except Exception as e:
        print(f"[run_sync] WARNING: Kylas user API failed ({e}) — using config map only\n")

    if args.test:
        print("[TEST MODE] Only first 5 records per module will be processed.\n")

    stats = {}

    print("=" * 40 + "\nMODULE 1: Companies\n" + "=" * 40)
    # Companies are always fully fetched (no since filter) so the id_map is
    # complete and every contact/deal can be linked to its parent company.
    company_result    = _load("01_company_sync.py").run(test_mode=args.test, logger=logger)
    stats["companies"] = company_result
    company_id_map    = company_result.get("id_map", {})
    print(f"[run_sync] {len(company_id_map)} company IDs available for linking\n")

    print("\n" + "=" * 40 + "\nMODULE 2: Contacts\n" + "=" * 40)
    contact_result  = _load("02_contact_sync.py").run(
        test_mode=args.test, logger=logger,
        user_map=user_map, company_id_map=company_id_map, since=since,
    )
    stats["contacts"]  = contact_result
    bd_daily           = contact_result.get("bd_daily", {})
    account_activity   = contact_result.get("account_activity", {})
    print(f"[run_sync] BD daily metrics for {len(bd_daily)} owner(s)\n")

    print("\n" + "=" * 40 + "\nMODULE 3: Deals\n" + "=" * 40)
    stats["deals"] = _load("03_deal_sync.py").run(
        test_mode=args.test, logger=logger, company_id_map=company_id_map, since=since,
    )

    print("\n" + "=" * 40 + "\nMODULE 5: BD Stats\n" + "=" * 40)
    bd_enriched  = _load("05_bd_stats.py").run(
        bd_daily, account_activity, company_id_map, args.slot, logger
    )
    print(f"[run_sync] BD enriched metrics for {len(bd_enriched)} owner(s)\n")

    print("\n" + "=" * 40 + "\nMODULE 4: Email Alert\n" + "=" * 40)
    _load("04_email_alert.py").send_alert(stats, args.slot, bd_enriched=bd_enriched)

    # Hot Pipeline digest → management, once a day on the EOD (6:30 PM) run
    if args.slot == "full_day":
        print("\n" + "=" * 40 + "\nMODULE 7: Hot Pipeline Digest\n" + "=" * 40)
        _load("07_hot_pipeline.py").run()

    print("\n[run_sync] All modules complete.")


if __name__ == "__main__":
    main()
