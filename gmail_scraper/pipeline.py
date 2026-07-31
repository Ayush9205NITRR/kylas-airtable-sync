"""
Gmail -> Airtable thread scraper.

For every category in config/email_categories.json, run its Gmail search, pull
each matching thread, flatten it (subject / sender / CC / first + last date /
attachments) and upsert into Airtable keyed on Thread ID.

    python -m gmail_scraper.pipeline                       # all categories, default lookback
    python -m gmail_scraper.pipeline --category "Offsite DMC"
    python -m gmail_scraper.pipeline --query 'Offsite DMC in:anywhere' --category-name "Offsite DMC"
    python -m gmail_scraper.pipeline --since 2025-01-01 --dry-run
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

from gmail_scraper import airtable_store, config, gmail_client, parse


def build_categories(args) -> list:
    """Resolve the run's categories from --query, --category or the JSON file."""
    if args.query:
        return [{"name": args.category_name or "Ad hoc", "query": args.query}]

    categories = config.load_categories()
    if not categories:
        raise RuntimeError(f"No categories defined in {config.CATEGORIES_FILE}")
    if args.category:
        wanted = {c.lower() for c in args.category}
        categories = [c for c in categories if c["name"].lower() in wanted]
        if not categories:
            raise RuntimeError(
                f"No category matched {args.category!r}. Available: "
                + ", ".join(c["name"] for c in config.load_categories())
            )
    return categories


def collect(categories: list, since: str, limit: int, mailbox: str) -> list:
    """Search each category and build one record per unique thread.

    A thread matching several categories is filed under the first one (config
    order = priority) but keeps the full list in `All Categories`.
    """
    date_clause = config.since_clause(since)
    matches = {}   # thread_id -> [category names, in priority order]
    order = []     # thread ids, first-seen order

    for cat in categories:
        query = f"{cat['query']} {date_clause}".strip()
        ids = gmail_client.search_thread_ids(query)
        print(f"[search] {cat['name']:<16} {query!r} -> {len(ids)} thread(s)")
        for tid in ids:
            if tid not in matches:
                matches[tid] = []
                order.append(tid)
            if cat["name"] not in matches[tid]:
                matches[tid].append(cat["name"])

    if limit:
        order = order[:limit]

    records = []
    for n, tid in enumerate(order, 1):
        thread = gmail_client.get_thread(tid)
        record = parse.thread_to_record(
            thread,
            category=matches[tid][0],
            all_categories=matches[tid],
            mailbox=mailbox,
        )
        if not record:
            continue
        records.append(record)
        if n % 25 == 0 or n == len(order):
            print(f"[fetch] {n}/{len(order)} threads")
    return records


def main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description="Scrape Gmail threads into Airtable")
    ap.add_argument("--category", action="append",
                    help="Only this category (repeatable). Default: all in the JSON config.")
    ap.add_argument("--query",
                    help="Raw Gmail search query, bypassing the category config.")
    ap.add_argument("--category-name",
                    help="Category label to file --query results under.")
    ap.add_argument("--since", default="",
                    help="30d / 6m / 1y / YYYY-MM-DD / all. "
                         f"Default: last {config.DEFAULT_LOOKBACK_DAYS} days.")
    ap.add_argument("--limit", type=int, default=0,
                    help="Process at most N threads (after dedup).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would be written; touch nothing in Airtable.")
    ap.add_argument("--json", dest="json_out",
                    help="Also dump the collected records to this JSON file.")
    args = ap.parse_args()

    categories = build_categories(args)

    mailbox = gmail_client.whoami()
    print(f"[auth] mailbox: {mailbox}")
    if config.GMAIL_USER and mailbox.lower() != config.GMAIL_USER.lower():
        print(f"[auth] WARNING: GMAIL_USER is {config.GMAIL_USER!r} but the "
              f"credentials resolve to {mailbox!r} — scraping {mailbox!r}.")

    records = collect(categories, args.since, args.limit, mailbox)
    print(f"[collect] {len(records)} unique thread(s)")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(records, fh, indent=2, ensure_ascii=False)
        print(f"[json] wrote {args.json_out}")

    if args.dry_run:
        for r in records[:20]:
            print(f"  [{r['Category']}] {r['First Email Date'][:10]} -> "
                  f"{r['Last Email Date'][:10]}  {r['Sender Email']:<30} "
                  f"{r['Subject'][:60]}"
                  + (f"  ({r['Attachment Count']} att)" if r["Attachment Count"] else ""))
        if len(records) > 20:
            print(f"  ... and {len(records) - 20} more")
        print("[dry-run] nothing written to Airtable")
        return 0

    if not records:
        print("[airtable] nothing to write")
        return 0

    result = airtable_store.upsert(records)
    print(f"[airtable] created {result['created']}, updated {result['updated']} "
          f"in {config.TABLE_NAME}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
