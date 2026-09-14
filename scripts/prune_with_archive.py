#!/usr/bin/env python3
"""
Archive rows older than a retention window to CSV in this repo, then delete
them from Airtable.

WHY ARCHIVE FIRST
The Airtable cap is a hard ceiling (50,000/base), and three tables grow every
working day: BD Metrics Daily writes one row per rep per metric per day,
Account Activity Log one per company worked, Sync Log one per run. On
2026-09-13 the main base hit 50,018 and started rejecting writes outright --
127 stage changes could not be logged and both daily digests froze on
identical numbers.

Deleting to make room is only safe if the rows survive somewhere, so every row
is written to CSV under state/archive/ and committed to git BEFORE it is
deleted. Git is already this repo's system of record for the stage and account
snapshots, it has no row cap, and it costs nothing. Airtable then holds the
working window; git holds the full history.

Nothing reportable is lost either way: Contact Matrix and Company Matrix
already carry the monthly rollup of BD Metrics Daily, which is the grain every
report older than the retention window reads at anyway.

REFUSES TO DELETE WHAT IT COULD NOT ARCHIVE. If the CSV write fails, the
delete does not happen -- the cap is an inconvenience, losing the only copy of
a measured day is not.

    python scripts/prune_with_archive.py                      # dry run, all tables
    python scripts/prune_with_archive.py --table "Sync Log"
    python scripts/prune_with_archive.py --apply
"""
import argparse
import csv
import os
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.airtable_client import AirtableClient   # noqa: E402

ARCHIVE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "state", "archive")

# table -> (retention_days, date_field)
#
# Windows are set from the 2026-09-14 audit (BD Metrics Daily 9,630 rows at
# ~79/day, Account Activity Log 6,752 at ~72/day, Sync Log 953) to land the
# main base near 26k of its 50k cap. BD Stage Changes keeps a long window on
# purpose: at 842 rows it is not the problem, and it is the evidence table the
# metrics are derived from.
RETENTION = {
    "BD Metrics Daily":     (90,  "Date"),
    "Account Activity Log": (60,  "Date"),
    "Sync Log":             (30,  "Started At"),
    "BD Daily Stats":       (90,  "Date"),
    "BD Stage Changes":     (400, "Date"),
}


def row_date(fields: dict, date_field: str) -> str:
    return str(fields.get(date_field, "") or "")[:10]


def expired(rows: list, date_field: str, cutoff: str) -> tuple:
    """(to_delete, undated) split. A row with no usable date is NEVER deleted:
    an unparseable date is not evidence that a row is old."""
    to_delete, undated = [], 0
    for r in rows:
        d = row_date(r.get("fields", {}), date_field)
        if len(d) == 10 and d[4] == "-":
            if d < cutoff:
                to_delete.append(r)
        else:
            undated += 1
    return to_delete, undated


def archive(table: str, rows: list, date_field: str) -> list:
    """Write rows to state/archive/<table>/<YYYY-MM>.csv, one file per month.

    Returns the files written. Appends to an existing month rather than
    overwriting it, so a second prune of the same month does not discard what
    the first one saved.
    """
    if not rows:
        return []
    safe = table.lower().replace(" ", "_")
    by_month = {}
    for r in rows:
        month = row_date(r.get("fields", {}), date_field)[:7] or "undated"
        by_month.setdefault(month, []).append(r)

    written = []
    out_dir = os.path.join(ARCHIVE_DIR, safe)
    os.makedirs(out_dir, exist_ok=True)
    for month, batch in sorted(by_month.items()):
        path = os.path.join(out_dir, f"{month}.csv")
        new_rows = [{"_airtable_id": r["id"], **r.get("fields", {})} for r in batch]

        # Rewrite the whole month rather than appending. A field that only
        # shows up on a later prune would otherwise be written against the
        # header already on disk, which does not declare it -- the value is
        # then unreadable, silently, in the file that exists so nothing is
        # lost. Monthly CSVs are small; correctness is worth the rewrite.
        existing_rows, existing_cols = [], []
        if os.path.exists(path):
            with open(path, newline="", encoding="utf-8") as fh:
                rdr = csv.DictReader(fh)
                existing_cols = list(rdr.fieldnames or [])
                existing_rows = list(rdr)

        cols = [c for c in existing_cols if c != "_airtable_id"]
        for r in new_rows:
            for k in r:
                if k != "_airtable_id" and k not in cols:
                    cols.append(k)

        # Idempotent on the Airtable record id: archiving runs separately from
        # deleting (archive -> commit -> delete, so a failed push can never
        # strand rows that are already gone from Airtable), which means the
        # same rows get archived twice whenever that sequence is retried.
        merged, seen = [], set()
        for r in existing_rows + new_rows:
            rid = r.get("_airtable_id")
            if rid in seen:
                continue
            seen.add(rid)
            merged.append(r)

        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["_airtable_id"] + cols,
                               extrasaction="ignore")
            w.writeheader()
            for r in merged:
                w.writerow(r)
        written.append(path)
        print(f"    archived {len(batch):5,} row(s) → "
              f"{os.path.relpath(path, os.path.dirname(ARCHIVE_DIR))}")
    return written


def prune_table(table: str, days: int, date_field: str, apply: bool,
                archive_only: bool = False, purge: bool = False) -> dict:
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    print(f"\n  {table}  " + ("(PURGE — entire contents)" if purge
                              else f"(keep {days}d, older than {cutoff})"))
    try:
        at = AirtableClient(table)
        rows = at.table.all()
    except Exception as exc:
        print(f"    ERROR: cannot read — {exc}")
        return {"table": table, "error": str(exc)}

    if purge:
        # Retiring a table, not trimming one: take every row, including any
        # whose date will not parse. The date guard below exists to stop a
        # retention window eating rows it cannot prove are old — irrelevant
        # when the whole table is going.
        to_delete, undated = list(rows), 0
        print(f"    {len(rows):6,} row(s) | {len(to_delete):6,} to purge")
    else:
        to_delete, undated = expired(rows, date_field, cutoff)
        print(f"    {len(rows):6,} row(s) | {len(to_delete):6,} older than cutoff"
              + (f" | {undated:,} undated (kept)" if undated else ""))
    if not to_delete:
        return {"table": table, "total": len(rows), "deleted": 0}

    if not apply and not archive_only:
        print(f"    DRY RUN — would archive then delete {len(to_delete):,}")
        return {"table": table, "total": len(rows), "would_delete": len(to_delete)}

    try:
        archive(table, to_delete, date_field)
    except Exception as exc:
        # The whole point of archiving first: a failed archive must abort the
        # delete, never proceed with it.
        print(f"    ABORTED: archive failed, nothing deleted — {exc}")
        return {"table": table, "error": f"archive failed: {exc}"}

    if archive_only:
        print(f"    archived only — {len(to_delete):,} row(s) still in Airtable")
        return {"table": table, "total": len(rows), "archived": len(to_delete)}

    deleted = 0
    ids = [r["id"] for r in to_delete]
    for i in range(0, len(ids), 10):            # Airtable deletes 10 at a time
        try:
            at.table.batch_delete(ids[i:i + 10])
            deleted += len(ids[i:i + 10])
        except Exception as exc:
            print(f"    [FAIL] batch at {i}: {exc}")
    print(f"    deleted {deleted:,} row(s)")
    return {"table": table, "total": len(rows), "deleted": deleted}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Archive expired Airtable rows to CSV in git, then delete them")
    ap.add_argument("--apply", action="store_true",
                    help="Actually archive and delete (default is a dry run)")
    ap.add_argument("--archive-only", action="store_true",
                    help="Write the CSVs but delete nothing. Run this FIRST, "
                         "commit the archive, then re-run with --apply: a push "
                         "that fails after the delete would strand rows that no "
                         "longer exist anywhere else.")
    ap.add_argument("--table", default="", metavar="NAME",
                    help="Only this table (default: every table in RETENTION)")
    ap.add_argument("--purge", action="store_true",
                    help="With --table: archive and delete the table's ENTIRE "
                         "contents, not just rows past a retention window. For "
                         "retiring a table nothing writes or reads any more. "
                         "Airtable's API cannot drop the table itself, so the "
                         "empty shell is left for you to remove by hand.")
    ap.add_argument("--date-field", default="Date", metavar="NAME",
                    help="Date column to group the archive by (purge only)")
    args = ap.parse_args()

    if args.purge and not args.table:
        print("ERROR: --purge requires --table. Refusing to empty every table.")
        return 1

    if args.purge:
        tables = {args.table: (0, args.date_field)}
    else:
        tables = ({args.table: RETENTION[args.table]} if args.table in RETENTION
                  else RETENTION if not args.table else None)
    if tables is None:
        print(f"ERROR: {args.table!r} has no retention window. "
              f"Known: {', '.join(sorted(RETENTION))}. "
              f"To retire a table entirely, use --purge.")
        return 1

    mode = ("ARCHIVE ONLY" if args.archive_only else
            "APPLY" if args.apply else "DRY RUN")
    print(f"[prune] {mode}{' PURGE' if args.purge else ''} — "
          f"{len(tables)} table(s)")
    results = [prune_table(t, d, f, args.apply, args.archive_only, args.purge)
               for t, (d, f) in sorted(tables.items())]

    freed = sum(r.get("deleted", 0) or r.get("archived", 0)
                or r.get("would_delete", 0) for r in results)
    errors = [r for r in results if r.get("error")]
    verb = ("archived" if args.archive_only else
            "freed" if args.apply else "would free")
    print(f"\n[prune] {verb}: {freed:,} row(s)")
    if errors:
        for r in errors:
            print(f"[prune] ERROR {r['table']}: {r['error']}")
        return 1
    if not args.apply:
        print("[prune] nothing written. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    sys.exit(main())
