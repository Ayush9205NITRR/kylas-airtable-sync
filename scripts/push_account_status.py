"""
push_account_status.py — Push Account Status + Last Called AT-Date to Kylas companies.

Fetches all Kylas contacts once, runs compute_health(), then writes two custom
fields back to each company record via GET + PUT (skip-if-same built in).

Modes:
  --test [N]  : Process first N companies only (default 5). Good for validation.
  --all       : Full backfill — process every company with health data.
  --since DATE: Incremental — only companies where last_called >= DATE.
                Daily runs use this with yesterday's date to narrow scope.
  --dry-run   : Show what would be written; no Kylas API writes.

Examples:
  python scripts/push_account_status.py --test
  python scripts/push_account_status.py --test 10 --dry-run
  python scripts/push_account_status.py --since 2026-06-01
  python scripts/push_account_status.py --all
"""
import argparse
import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_module(filename: str):
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "modules", filename,
    )
    spec = importlib.util.spec_from_file_location(filename, path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    parser = argparse.ArgumentParser(
        description="Push Account Status + Last Called Date to Kylas companies",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--test",  type=int, metavar="N", nargs="?", const=5,
                      help="Process first N companies (default 5)")
    mode.add_argument("--all",   action="store_true",
                      help="Process all companies (full backfill)")
    mode.add_argument("--since", metavar="DATE",
                      help="Only companies where last_called >= DATE (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print actions without making any Kylas API writes")
    args = parser.parse_args()

    if args.test is None and not args.all and args.since is None:
        parser.print_help()
        print("\nERROR: specify --test, --all, or --since DATE")
        sys.exit(1)

    from dotenv import load_dotenv
    load_dotenv()

    from utils.kylas_client import KylasClient

    kylas = KylasClient()

    # ── Discover Kylas custom-field keys by display name ──────────────────────
    print("[push] Discovering Kylas company custom field keys...")
    status_key = kylas.cf_key_for_display("company", "Account Status")
    lc_key     = kylas.cf_key_for_display("company", "Last Called AT - Date")
    print(f"[push]   'Account Status'        → {status_key!r}")
    print(f"[push]   'Last Called AT - Date' → {lc_key!r}")

    if not status_key and not lc_key:
        print("[push] ERROR: Neither Kylas field found — "
              "check that the custom fields exist in Kylas (Company entity)")
        sys.exit(1)
    if not status_key:
        print("[push] WARNING: 'Account Status' not found in Kylas — that field will be skipped")
    if not lc_key:
        print("[push] WARNING: 'Last Called AT - Date' not found in Kylas — that field will be skipped")

    # ── Fetch contacts + compute health ───────────────────────────────────────
    print("[push] Fetching all contacts from Kylas...")
    contacts = kylas._search_all(
        "contact",
        fields=["id", "company", "ownedBy", "updatedAt", "customFieldValues"],
    )
    print(f"[push] {len(contacts)} contacts fetched")

    ah     = _load_module("06_account_health.py")
    health = ah.compute_health(contacts)
    print(f"[push] {len(health)} companies with computed health data")

    # ── Apply filters ─────────────────────────────────────────────────────────
    if args.since:
        since_date = args.since.strip()[:10]
        before = len(health)
        health = {cid: e for cid, e in health.items()
                  if e["last_called"] >= since_date}
        print(f"[push] --since {since_date}: {len(health)} of {before} "
              f"companies have a call on/after that date")

    company_ids = list(health.keys())
    if args.test is not None:
        n = args.test
        company_ids = company_ids[:n]
        print(f"[push] --test {n}: will process {len(company_ids)} companies")

    mode_tag = "DRY RUN" if args.dry_run else "LIVE"
    print(f"\n[push] Processing {len(company_ids)} companies ({mode_tag})")
    if args.dry_run:
        print("[push] (no Kylas writes — remove --dry-run to apply)")

    # ── Push to Kylas ─────────────────────────────────────────────────────────
    pushed = unchanged = failed = 0

    for i, co_id in enumerate(company_ids, 1):
        e      = health[co_id]
        status = e["status"]
        lc     = e["last_called"]

        fields = {}
        if status_key:
            fields[status_key] = status
        if lc_key and lc:
            fields[lc_key] = lc

        if not fields:
            unchanged += 1
            continue

        try:
            result = kylas.update_company_fields(int(co_id), fields, dry_run=args.dry_run)
        except Exception as exc:
            failed += 1
            print(f"  [{i:>4}/{len(company_ids)}] company {co_id}: ERROR — {exc}")
            continue

        if result == "updated":
            pushed += 1
            print(f"  [{i:>4}/{len(company_ids)}] company {co_id}: "
                  f"PUSHED  status={status!r}  last_called={lc!r}")
        elif result == "unchanged":
            unchanged += 1
            if args.dry_run:
                print(f"  [{i:>4}/{len(company_ids)}] company {co_id}: "
                      f"no change  status={status!r}  last_called={lc!r}")
        else:
            failed += 1

    print(f"\n[push] Done: pushed={pushed}  unchanged={unchanged}  failed={failed}")


if __name__ == "__main__":
    main()
