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
    args = parser.parse_args()

    from dotenv import load_dotenv
    load_dotenv()

    if args.dry_run:
        print("[DRY RUN] Modules that would run:")
        for m in ["01_company_sync.py", "02_contact_sync.py", "03_deal_sync.py", "04_email_alert.py"]:
            print(f"  - {m}")
        return

    sys.path.insert(0, os.path.dirname(__file__))
    from utils.logger import SyncLogger
    logger = SyncLogger()
    print(f"[run_sync] Run ID={logger.run_id}  slot={args.slot}\n")

    if args.test:
        print("[TEST MODE] Only first 5 records per module will be processed.\n")

    stats = {}

    print("=" * 40 + "\nMODULE 1: Companies\n" + "=" * 40)
    stats["companies"] = _load("01_company_sync.py").run(test_mode=args.test, logger=logger)

    print("\n" + "=" * 40 + "\nMODULE 2: Contacts\n" + "=" * 40)
    stats["contacts"] = _load("02_contact_sync.py").run(test_mode=args.test, logger=logger)

    print("\n" + "=" * 40 + "\nMODULE 3: Deals\n" + "=" * 40)
    stats["deals"] = _load("03_deal_sync.py").run(test_mode=args.test, logger=logger)

    print("\n" + "=" * 40 + "\nMODULE 4: Email Alert\n" + "=" * 40)
    _load("04_email_alert.py").send_alert(stats, args.slot)

    print("\n[run_sync] All modules complete.")


if __name__ == "__main__":
    main()
