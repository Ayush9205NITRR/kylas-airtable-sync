"""
push_account_status.py — Push Account Status + Last Called AT-Date to Kylas companies.

Fetches all Kylas contacts once, runs compute_health(), then writes two custom
fields back to each company record via GET + PUT (skip-if-same built in).

Modes:
  --test [N]  : Process first N companies only (default 5). Good for validation.
  --all       : Full backfill — process every company with health data.
  --since DATE: Incremental — only companies where last_called >= DATE.
                Daily runs use this with yesterday's date to narrow scope.
  --list-fields: Print all discoverable company custom field keys and exit.
  --dry-run   : Show what would be written; no Kylas API writes.

Field key overrides (use if auto-discovery fails):
  --status-key cfXxx  : Kylas internal key for the "Account Status" field.
  --lc-key     cfXxx  : Kylas internal key for the "Last Called AT - Date" field.

Examples:
  python scripts/push_account_status.py --list-fields
  python scripts/push_account_status.py --test --dry-run
  python scripts/push_account_status.py --test 10 --status-key cfAccountStatus --lc-key cfLastCalledAtDate
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


def _guess_cf_key(display_name: str) -> str:
    """Convention-based cf key guess: 'Account Status' → 'cfAccountStatus'."""
    import re
    words = re.split(r"[\s\-_/]+", display_name.strip())
    if not words:
        return ""
    camel = words[0].lower() + "".join(w.capitalize() for w in words[1:])
    camel = re.sub(r"[^a-zA-Z0-9]", "", camel)
    return "cf" + camel[0].upper() + camel[1:] if camel else ""


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
    mode.add_argument("--list-fields", action="store_true",
                      help="List all discoverable company cf keys and exit")
    parser.add_argument("--status-key", metavar="cfXxx",
                        help="Kylas cf key for 'Account Status' (overrides auto-discovery)")
    parser.add_argument("--lc-key", metavar="cfXxx",
                        help="Kylas cf key for 'Last Called AT - Date' (overrides auto-discovery)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print actions without making any Kylas API writes")
    args = parser.parse_args()

    if (args.test is None and not args.all and args.since is None
            and not args.list_fields):
        parser.print_help()
        print("\nERROR: specify --test, --all, --since DATE, or --list-fields")
        sys.exit(1)

    from dotenv import load_dotenv
    load_dotenv()

    from utils.kylas_client import KylasClient

    kylas = KylasClient()

    # ── --list-fields: dump all discoverable company cf keys and exit ─────────
    if args.list_fields:
        print("[push] Fetching company custom field keys from Kylas...")

        # Raw field definitions from /entities/company/fields (no cf prefix filter)
        raw_fields = kylas.list_entity_fields("company")
        if raw_fields:
            print(f"\n[push] Raw /entities/company/fields — {len(raw_fields)} total fields:")
            for fld in raw_fields:
                fname   = (fld.get("fieldName") or fld.get("apiName") or fld.get("name")
                           or fld.get("id") or "?")
                display = fld.get("displayName") or fld.get("label") or fname
                ftype   = fld.get("type") or fld.get("fieldType") or ""
                print(f"  {fname:<45} '{display}'  [{ftype}]")
        else:
            print("[push] /entities/company/fields returned nothing")

        keys = kylas.list_custom_field_keys("company")
        defs = kylas.get_custom_field_defs("company")
        all_keys = sorted(set(list(keys.keys()) + list(defs.keys())))
        if not all_keys:
            print("[push] No custom field keys found via value-scan either.")
        else:
            print(f"\n[push] Found {len(all_keys)} company custom field key(s) via value scan:\n")
            for k in all_keys:
                display = keys.get(k) or defs.get(k, {}).get("displayName") or k
                ftype   = defs.get(k, {}).get("type", "")
                opts    = defs.get(k, {}).get("options") or {}
                opt_str = f"  options={list(opts.keys())[:5]}" if opts else ""
                print(f"  {k:<40} '{display}'  [{ftype}]{opt_str}")

        # Raw-value diagnostic: find a company with cfAccountStatus already set
        # and print the exact JSON Kylas stores so we know the correct write format.
        _PROBE_KEYS = ["cfAccountStatus", "cfLastCalledAtDate"]
        print("\n[push] Probing raw stored values for target CF keys...")
        try:
            r = kylas._request(
                "POST", "search/company",
                params={"page": 0, "size": 50, "sort": "updatedAt,desc"},
                json={"fields": ["id", "customFieldValues"], "jsonRule": None},
            )
            kylas._raise_for_status(r)
            found = {k: None for k in _PROBE_KEYS}
            for rec in r.json().get("content", []):
                co_id = rec.get("id")
                if not co_id or all(v is not None for v in found.values()):
                    break
                try:
                    detail  = kylas._get(f"companies/{co_id}")
                    company = detail.get("data", detail) if isinstance(detail, dict) else {}
                    cfv     = company.get("customFieldValues") or {}
                    for probe_key in _PROBE_KEYS:
                        if found[probe_key] is None and probe_key in cfv:
                            found[probe_key] = (co_id, cfv[probe_key])
                except Exception as exc:
                    print(f"[push]   company {co_id}: fetch error — {exc}")
            for probe_key in _PROBE_KEYS:
                if found[probe_key]:
                    co_id, raw = found[probe_key]
                    print(f"[push]   {probe_key}: company={co_id}  raw={raw!r}  type={type(raw).__name__}")
                else:
                    print(f"[push]   {probe_key}: no existing value found in last 50 companies")
        except Exception as exc:
            print(f"[push]   raw-value probe failed: {exc}")

        return

    # ── Discover Kylas custom-field keys by display name ──────────────────────
    print("[push] Discovering Kylas company custom field keys...")

    status_key = args.status_key or kylas.cf_key_for_display("company", "Account Status")
    lc_key     = args.lc_key     or kylas.cf_key_for_display("company", "Last Called AT - Date")

    # Convention-based fallback: "Account Status" → cfAccountStatus
    if not status_key:
        guess = _guess_cf_key("Account Status")
        print(f"[push]   'Account Status' not found via API — trying convention guess: {guess!r}")
        status_key = guess
    if not lc_key:
        guess = _guess_cf_key("Last Called AT - Date")
        print(f"[push]   'Last Called AT - Date' not found via API — trying convention guess: {guess!r}")
        lc_key = guess

    print(f"[push]   Account Status key   → {status_key!r}")
    print(f"[push]   Last Called Date key  → {lc_key!r}")
    print(f"[push]   (override with --status-key / --lc-key if these are wrong)")

    if not status_key and not lc_key:
        print("[push] ERROR: No field keys available. Use --list-fields to inspect Kylas fields,")
        print("       then re-run with --status-key cfXxx --lc-key cfXxx")
        sys.exit(1)

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

        print(f"[debug] Company {co_id} status={status!r} lc={lc!r} → fields={fields}")
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
