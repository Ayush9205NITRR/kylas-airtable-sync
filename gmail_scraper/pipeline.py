"""
Gmail -> Airtable thread scraper.

Search terms come from the `Search Terms` table in Airtable — add a row, type a
name, and the next run picks it up. For each term, run the Gmail search, pull
each matching thread, flatten it (subject / sender / CC / first + last date /
attachments) and upsert into Airtable keyed on Thread ID. Attachments are
uploaded as real files so they're downloadable from the record.

    python -m gmail_scraper.pipeline --limit 5 --dry-run   # look first
    python -m gmail_scraper.pipeline --limit 5             # first 5, for real
    python -m gmail_scraper.pipeline                        # everything
    python -m gmail_scraper.pipeline --term "Acme Travels" --since all
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

from gmail_scraper import (airtable_store, attachments, config, gmail_client,
                           parse, search_terms)


def build_terms(args) -> list:
    """Resolve this run's search terms from --term, --query, or the sources."""
    if args.query:
        return [{"name": args.category_name or "Ad hoc", "query": args.query}]

    if args.term:
        return [{"name": args.category_name or t, "query": config.build_query(t)}
                for t in args.term]

    terms = search_terms.load(args.terms_from)
    if not terms:
        raise RuntimeError(
            "No search terms found. Add rows to the Airtable "
            f"'{config.SEARCH_TERMS_TABLE}' table, or edit {config.CATEGORIES_FILE}."
        )
    if args.category:
        wanted = {c.lower() for c in args.category}
        filtered = [t for t in terms if t["name"].lower() in wanted]
        if not filtered:
            raise RuntimeError(
                f"No term matched {args.category!r}. Available: "
                + ", ".join(t["name"] for t in terms))
        return filtered
    return terms


def collect(terms: list, since: str, limit: int, mailbox: str,
            stats: dict = None) -> list:
    """Search each term and build one entry per unique thread.

    Uniqueness is the Gmail thread id: a conversation that matches several
    terms is collected **once**, filed under the first term (list order =
    priority), with every match listed in `All Categories`. Pass `stats` to
    receive {'raw_hits', 'unique'} for reporting.
    """
    date_clause = config.since_clause(since)
    matches = {}   # thread_id -> [term names, in priority order]
    order = []     # thread ids, first-seen order
    raw_hits = 0

    for term in terms:
        query = f"{term['query']} {date_clause}".strip()
        ids = gmail_client.search_thread_ids(query)
        raw_hits += len(ids)
        print(f"[search] {term['name']:<24} {query!r} -> {len(ids)} thread(s)")
        for tid in ids:
            if tid not in matches:
                matches[tid] = []
                order.append(tid)
            if term["name"] not in matches[tid]:
                matches[tid].append(term["name"])

    if raw_hits > len(order):
        print(f"[dedup] {raw_hits} hit(s) -> {len(order)} unique thread(s) "
              f"({raw_hits - len(order)} matched more than one keyword)")
    if stats is not None:
        stats["raw_hits"] = raw_hits
        stats["unique"] = len(order)

    if limit:
        if len(order) > limit:
            print(f"[limit] {len(order)} threads matched — taking the first {limit}")
        order = order[:limit]

    out = []
    for n, tid in enumerate(order, 1):
        thread = gmail_client.get_thread(tid)
        record = parse.thread_to_record(
            thread, category=matches[tid][0], all_categories=matches[tid],
            mailbox=mailbox)
        if not record:
            continue
        out.append({"record": record, "refs": parse.attachment_refs(thread)})
        if n % 25 == 0 or n == len(order):
            print(f"[fetch] {n}/{len(order)} threads")
    return out


def push_attachments(items: list, by_key: dict) -> int:
    """Upload each thread's attachments onto its (now existing) Airtable record."""
    total = 0
    for item in items:
        refs = item["refs"]
        if not refs:
            continue
        record = by_key.get(item["record"][config.KEY_FIELD])
        if not record:
            print(f"[attach]   ! no record id for thread "
                  f"{item['record'][config.KEY_FIELD]} — skipping its files")
            continue
        # Attachments accumulate in the field, so on a re-run only send what
        # isn't already there.
        already = attachments.existing_filenames(record)
        pending = [r for r in refs if r["filename"] not in already]
        if not pending:
            continue
        result = attachments.upload_for_thread(
            record["id"], pending, subject=item["record"].get("Subject", ""))
        total += result["uploaded"]
    return total


def main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description="Scrape Gmail threads into Airtable")
    ap.add_argument("--term", action="append",
                    help="Search this name directly, skipping the term tables "
                         "(repeatable).")
    ap.add_argument("--category", action="append",
                    help="Only run this term/category from the configured list "
                         "(repeatable).")
    ap.add_argument("--query",
                    help="Raw Gmail search query, used verbatim.")
    ap.add_argument("--category-name",
                    help="Category label to file --query/--term results under.")
    ap.add_argument("--terms-from", choices=["auto", "airtable", "config"],
                    default="auto",
                    help="Where search terms come from. Default: Airtable, "
                         "falling back to the JSON file.")
    ap.add_argument("--since", default="",
                    help="30d / 6m / 1y / YYYY-MM-DD / all. "
                         f"Default: last {config.DEFAULT_LOOKBACK_DAYS} days.")
    ap.add_argument("--limit", type=int, default=0,
                    help="Process at most N threads (after dedup). Use --limit 5 "
                         "for a quick test run.")
    ap.add_argument("--no-attachments", action="store_true",
                    help="Skip uploading attachment files (names still recorded).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would be written; touch nothing in Airtable.")
    ap.add_argument("--json", dest="json_out",
                    help="Also dump the collected records to this JSON file.")
    args = ap.parse_args()

    terms = build_terms(args)

    mailbox = gmail_client.whoami()
    print(f"[auth] mailbox: {mailbox}")
    if config.GMAIL_USER and mailbox.lower() != config.GMAIL_USER.lower():
        print(f"[auth] WARNING: GMAIL_USER is {config.GMAIL_USER!r} but the "
              f"credentials resolve to {mailbox!r} — scraping {mailbox!r}.")

    items = collect(terms, args.since, args.limit, mailbox)
    records = [i["record"] for i in items]
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

    if args.no_attachments:
        print("[attach] skipped (--no-attachments)")
    else:
        uploaded = push_attachments(items, result["by_key"])
        print(f"[attach] {uploaded} file(s) uploaded to "
              f"'{config.ATTACHMENT_FIELD}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
