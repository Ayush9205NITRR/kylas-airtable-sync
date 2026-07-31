"""
Run the Gmail -> Airtable scraper locally, end to end, in one command.

    python run_gmail_local.py

It walks three steps and stops at the first one that fails, so you always know
exactly what's broken:

    1. CHECK   auth + credentials (read-only, touches nothing)
    2. SCHEMA  create/verify the Airtable tables and fields (only ever adds)
    3. SCRAPE  search the keywords, show what was found, then write

By default it searches **proposal** and **cost sheet**, takes the first 5
threads, shows you the table, and asks before writing anything.

Common runs:

    python run_gmail_local.py                          # the default 5-thread test
    python run_gmail_local.py --dry-run                # look, never write
    python run_gmail_local.py --keywords "proposal,cost sheet,quotation"
    python run_gmail_local.py --limit 20 --since 1y --yes
    python run_gmail_local.py --check-only             # just step 1
    python run_gmail_local.py --use-airtable-terms     # terms from the Airtable table

Setup, once:

    pip install -r gmail_scraper/requirements.txt
    cp .env.example .env          # then fill in the Gmail + Airtable values
    python -m gmail_scraper.auth_setup --client-secret client_secret.json
"""
import argparse
import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DEFAULT_KEYWORDS = ["proposal", "cost sheet"]


def rule(title: str):
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def step_check() -> bool:
    """Step 1 — credentials, Airtable reach, Gmail auth. Writes nothing."""
    rule("STEP 1/3  Checking credentials and auth")
    from gmail_scraper import doctor
    return doctor.main() == 0


def step_schema(skip: bool) -> bool:
    """Step 2 — make sure the tables and fields exist. Only ever adds."""
    rule("STEP 2/3  Airtable schema")
    if skip:
        print("Skipped (--no-schema).")
        return True
    import runpy
    from gmail_scraper import config
    print(f"Base {config.AIRTABLE_BASE_ID}, table {config.TABLE_NAME}.")
    print("This only ADDS missing tables/fields — nothing existing is changed.\n")
    try:
        runpy.run_path(
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "scripts", "setup_gmail_airtable.py"),
            run_name="__main__")
    except SystemExit as exc:
        return (exc.code or 0) == 0
    return True


def preview(records: list):
    """Print the collected threads as a table so you can eyeball them."""
    if not records:
        print("  (nothing matched)")
        return
    print(f"\n  {'CATEGORY':<14} {'FIRST':<11} {'LAST':<11} {'FROM':<28} "
          f"{'ATT':>3}  SUBJECT")
    print(f"  {'-' * 14} {'-' * 11} {'-' * 11} {'-' * 28} {'-' * 3}  {'-' * 34}")
    for r in records:
        print(f"  {r['Category'][:14]:<14} {r['First Email Date'][:10]:<11} "
              f"{r['Last Email Date'][:10]:<11} {r['Sender Email'][:28]:<28} "
              f"{r['Attachment Count']:>3}  {r['Subject'][:34]}")
    total_files = sum(r["Attachment Count"] for r in records)
    print(f"\n  {len(records)} thread(s), {total_files} attachment(s).")


def uniqueness_report(records: list, raw_hits: int):
    """Show that dedup is working, at both levels it happens.

    Two different things get deduped, and it's worth seeing them separately:
      * within a run — one thread matching both "proposal" and "cost sheet"
        is collected once, not twice
      * across runs — a thread already in Airtable is updated, not re-added
    """
    from gmail_scraper import config
    keys = [r[config.KEY_FIELD] for r in records]
    unique = set(keys)

    print("\n  Uniqueness")
    print(f"    search hits (before dedup) : {raw_hits}")
    print(f"    unique threads collected   : {len(unique)}")
    if raw_hits > len(unique):
        print(f"    → {raw_hits - len(unique)} hit(s) were the same thread "
              "matching more than one keyword")
    if len(keys) != len(unique):     # should never happen; loud if it does
        print(f"    !! {len(keys) - len(unique)} DUPLICATE key(s) in one batch — "
              "please report this")

    multi = [r for r in records if "," in r.get("All Categories", "")]
    if multi:
        print(f"    {len(multi)} thread(s) matched multiple keywords, e.g. "
              f"{multi[0]['Subject'][:40]!r} -> {multi[0]['All Categories']}")


def step_scrape(args) -> bool:
    """Step 3 — search, preview, confirm, write."""
    rule("STEP 3/3  Scraping Gmail")
    from gmail_scraper import (airtable_store, config, gmail_client, pipeline,
                               search_terms)

    if args.use_airtable_terms:
        terms = search_terms.load("airtable")
    else:
        terms = [{"name": k.strip().title(), "query": config.build_query(k.strip())}
                 for k in args.keywords.split(",") if k.strip()]
    if not terms:
        print("No keywords to search. Pass --keywords 'proposal,cost sheet'.")
        return False

    print("Searching for:")
    for t in terms:
        print(f"  {t['name']:<20} {t['query']}")
    print()

    mailbox = gmail_client.whoami()
    stats = {}
    items = pipeline.collect(terms, args.since, args.limit, mailbox, stats)
    records = [i["record"] for i in items]
    preview(records)
    uniqueness_report(records, stats.get("raw_hits", len(records)))

    if not records:
        print("\nNothing to write. Try --since all, or different keywords.")
        return True

    if args.dry_run:
        print("\n--dry-run: nothing written to Airtable.")
        return True

    if not args.yes:
        answer = input(f"\nWrite these {len(records)} thread(s) to Airtable? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Cancelled — nothing written.")
            return True

    result = airtable_store.upsert(records)
    print(f"\n[airtable] created {result['created']} new row(s), "
          f"updated {result['updated']} existing row(s)")
    if result["updated"]:
        print("           (updates = threads already scraped before; matched on "
              "Thread ID, so no duplicates were added)")

    if args.no_attachments:
        print("[attach] skipped (--no-attachments)")
    else:
        uploaded = pipeline.push_attachments(items, result["by_key"])
        print(f"[attach] {uploaded} file(s) uploaded to "
              f"'{config.ATTACHMENT_FIELD}'")

    print(f"\nDone. Open the base to see the rows:")
    print(f"  https://airtable.com/{config.AIRTABLE_BASE_ID}")
    return True


def main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(
        description="Run the Gmail -> Airtable scraper locally, end to end.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keywords", default=",".join(DEFAULT_KEYWORDS),
                    help="Comma-separated names to search. "
                         f"Default: {', '.join(DEFAULT_KEYWORDS)}")
    ap.add_argument("--use-airtable-terms", action="store_true",
                    help="Use the Airtable `Search Terms` table instead of --keywords.")
    ap.add_argument("--limit", type=int, default=5,
                    help="Max threads to process. Default: 5.")
    ap.add_argument("--since", default="",
                    help="30d / 6m / 1y / YYYY-MM-DD / all. Default: last 180 days.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what was found; write nothing.")
    ap.add_argument("--yes", "-y", action="store_true",
                    help="Don't ask before writing.")
    ap.add_argument("--no-attachments", action="store_true",
                    help="Don't upload attachment files (names still recorded).")
    ap.add_argument("--check-only", action="store_true",
                    help="Run step 1 and stop.")
    ap.add_argument("--no-schema", action="store_true",
                    help="Skip step 2 (the tables already exist).")
    args = ap.parse_args()

    if not step_check():
        print("\nStopped: fix the failures above, then re-run.")
        print("Missing GMAIL_REFRESH_TOKEN? Mint one with:")
        print("  python -m gmail_scraper.auth_setup --client-secret client_secret.json")
        return 1
    if args.check_only:
        print("\n--check-only: stopping here. Everything above passed.")
        return 0

    if not step_schema(args.no_schema):
        print("\nStopped: the Airtable schema step failed. The PAT needs "
              "schema.bases:write on this base.")
        return 1

    return 0 if step_scrape(args) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted — nothing further was written.")
        sys.exit(130)
