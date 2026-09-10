#!/usr/bin/env python3
"""
Re-attribute BD Stage Changes rows that were logged with a blank BD Associate.

WHY THESE ROWS EXIST
06_account_health.py's full-coverage stage diff passed a hardcoded empty owner
name into every change it detected (fixed 2026-09-10). push() dutifully wrote
each one with a blank "BD Associate", and bd_metrics_long.read_stage_changes()
drops any row it cannot attribute -- silently, until the same fix made that
warn. Meanwhile the snapshot had already advanced past those moves, so they
could not be re-detected: stage_history.diff() is first-write-wins and Kylas
keeps no stage history to re-read.

WHY THEY ARE STILL RECOVERABLE
The rows themselves were written, just unattributed, and each one is keyed
"<contact_id> | <date>" with the contact id also in its own column. The stage
snapshot (state/contact_stage.json) carries owner and email per contact id --
those fields are only ever rewritten on a first sighting, so they survived the
blank-owner writes intact. Joining the two puts the name back.

This does NOT invent attribution: a row is only touched when the snapshot has
a non-blank owner for that exact contact id. Anything unresolvable is reported
and left alone.

    python scripts/backfill_stage_change_owners.py              # dry run
    python scripts/backfill_stage_change_owners.py --apply
    python scripts/backfill_stage_change_owners.py --apply --date 2026-09-10
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.airtable_client import AirtableClient   # noqa: E402
from utils import stage_history                    # noqa: E402

TABLE = "BD Stage Changes"


def contact_id_of(fields: dict, key: str) -> str:
    """The row's contact id, from its own column or the "<id> | <date>" Key."""
    cid = str(fields.get("Contact Id", "") or "").strip()
    if cid:
        return cid
    return key.split("|")[0].strip() if "|" in key else ""


def plan(rows: list, snapshot: dict, only_date: str = "") -> tuple:
    """(fixable, unresolved) for rows whose BD Associate is blank.

    fixable:    [(record_id, contact_id, date, owner, email), ...]
    unresolved: [(record_id, contact_id, date), ...] -- no owner in the
                snapshot, so there is nothing to attribute them to.
    """
    fixable, unresolved = [], []
    for rec in rows:
        f = rec.get("fields", {})
        if str(f.get("BD Associate", "") or "").strip():
            continue
        date = str(f.get("Date", "") or "").strip()
        if only_date and date != only_date:
            continue
        cid = contact_id_of(f, str(f.get("Key", "") or ""))
        snap = snapshot.get(cid) or {}
        owner = str(snap.get("owner", "") or "").strip()
        if cid and owner:
            fixable.append((rec["id"], cid, date, owner,
                            str(snap.get("email", "") or "").strip()))
        else:
            unresolved.append((rec["id"], cid, date))
    return fixable, unresolved


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Re-attribute BD Stage Changes rows logged with a blank BD Associate")
    ap.add_argument("--apply", action="store_true",
                    help="Write the repaired rows (default is a dry run)")
    ap.add_argument("--date", default="", metavar="YYYY-MM-DD",
                    help="Only touch rows for this date")
    args = ap.parse_args()

    snapshot = (stage_history.load() or {})
    print(f"[backfill] snapshot: {len(snapshot)} contact(s)")

    at = AirtableClient(TABLE)
    rows = at.table.all()
    print(f"[backfill] {len(rows)} row(s) in {TABLE!r}")

    fixable, unresolved = plan(rows, snapshot, args.date)
    print(f"[backfill] blank-owner rows: {len(fixable) + len(unresolved)} "
          f"(repairable: {len(fixable)}, unresolvable: {len(unresolved)})")

    if not fixable:
        print("[backfill] nothing to repair")
        return 0

    by_owner = {}
    for _rid, _cid, date, owner, _em in fixable:
        by_owner.setdefault((owner, date), 0)
        by_owner[(owner, date)] += 1
    print("\n  rows to re-attribute (owner | date | n):")
    for (owner, date), n in sorted(by_owner.items(), key=lambda kv: -kv[1]):
        print(f"    {owner:28} {date}  {n}")

    if not args.apply:
        print("\n[backfill] DRY RUN — nothing written. Re-run with --apply.")
        return 0

    ok = failed = 0
    for rid, _cid, _date, owner, email in fixable:
        patch = {"BD Associate": owner}
        if email:
            patch["BD Email"] = email
        try:
            at.table.update(rid, patch)
            ok += 1
        except Exception as exc:
            failed += 1
            print(f"  [FAIL] {rid}: {exc}")

    print(f"\n[backfill] repaired={ok} failed={failed} "
          f"unresolvable={len(unresolved)}")
    return 1 if failed else 0


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    sys.exit(main())
