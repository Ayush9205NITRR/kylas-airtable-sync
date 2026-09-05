#!/usr/bin/env python3
"""
Contact owner = company owner. Once, then kept in sync.

The rule: whoever owns a company in Kylas should own every contact under that
company too. Today ownership drifts independently — a contact can sit with a
different owner than its company for no reason other than history.

This is a two-purpose script, not two scripts:
  1. RUN ONCE to backfill every existing mismatch.
  2. RUN AGAIN ANYTIME (safe, idempotent) to catch new contacts, newly
     reassigned companies, or anything the daily sync doesn't touch — an
     unchanged contact is a no-op, never a re-write.

The source of truth is Kylas's OWN company ownership (`ownerId`), not
Airtable — this is a Kylas-internal consistency rule, unrelated to (and does
not touch) scripts/assign_from_airtable.py, which pushes owner assignments
FROM Airtable instead.

SAFETY: this writes contact ownership in PRODUCTION Kylas, for potentially
thousands of contacts at once — hard to reverse quickly and consequential
(affects attribution, notifications, whatever else keys off ownership in your
tenant). Defaults to a dry run: nothing is written unless --apply is passed
explicitly. Always run without --apply first and read the summary.

    python scripts/assign_contacts_to_company_owner.py            # dry run — reports only
    python scripts/assign_contacts_to_company_owner.py --apply    # actually reassign
    python scripts/assign_contacts_to_company_owner.py --apply --limit 50   # first 50 moves only
"""
import argparse
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.kylas_client import KylasClient   # noqa: E402


def _company_id(ct: dict) -> str:
    """Kylas returns 'company' as a bare int on search results, a nested
    object on detail reads. Both shapes must be handled."""
    co = ct.get("company")
    if isinstance(co, (int, float)):
        return str(int(co))
    if isinstance(co, dict):
        return str(co.get("id", "")) or ""
    return ""


def build_plan(kylas: KylasClient) -> tuple:
    """
    Returns (moves, stats).
    moves: [{"contact_id", "contact_name", "company_id", "from_owner",
             "to_owner"}] — from_owner/to_owner are Kylas user ids (int or None).
    A contact is a "move" only when it HAS a company, that company HAS a known
    owner, and the contact's current owner differs from it.
    """
    print("[assign] Fetching companies from Kylas...")
    companies = kylas.get_companies()
    company_owner = {}
    for co in companies:
        oid = co.get("ownerId")
        if oid:
            company_owner[str(co["id"])] = int(oid)
    print(f"[assign] {len(companies)} companies fetched, "
          f"{len(company_owner)} have a known owner")

    print("[assign] Fetching contacts from Kylas...")
    contacts = kylas.get_contacts()
    print(f"[assign] {len(contacts)} contacts fetched")

    moves = []
    no_company = no_company_owner = already_correct = 0
    for ct in contacts:
        cid = _company_id(ct)
        if not cid:
            no_company += 1
            continue
        target = company_owner.get(cid)
        if target is None:
            no_company_owner += 1
            continue
        current = ct.get("ownerId")
        current = int(current) if current else None
        if current == target:
            already_correct += 1
            continue
        moves.append({
            "contact_id": ct["id"], "contact_name": ct.get("name") or "",
            "company_id": cid, "from_owner": current, "to_owner": target,
        })

    stats = {"companies": len(companies), "companies_with_owner": len(company_owner),
             "contacts": len(contacts), "no_company": no_company,
             "no_company_owner": no_company_owner,
             "already_correct": already_correct, "moves": len(moves)}
    return moves, stats


def print_summary(moves: list, stats: dict, user_names: dict) -> None:
    print(f"\n{stats['contacts']} contacts total")
    print(f"  {stats['no_company']:>6}  no company at all")
    print(f"  {stats['no_company_owner']:>6}  company has no owner in Kylas")
    print(f"  {stats['already_correct']:>6}  already match their company's owner")
    print(f"  {stats['moves']:>6}  WOULD BE REASSIGNED")

    if not moves:
        print("\nNothing to do.")
        return

    by_target = defaultdict(int)
    for m in moves:
        by_target[m["to_owner"]] += 1
    print("\nReassignments by new owner:")
    for uid, n in sorted(by_target.items(), key=lambda kv: -kv[1]):
        print(f"  {user_names.get(uid, f'user #{uid}'):<30} +{n} contact(s)")


def apply_moves(kylas: KylasClient, moves: list, limit: int = None) -> dict:
    tally = {"ok": 0, "failed": 0}
    for m in moves[:limit] if limit else moves:
        ok = kylas.update_contact_owner(m["contact_id"], m["to_owner"])
        tally["ok" if ok else "failed"] += 1
        if not ok:
            print(f"  [FAILED] contact {m['contact_id']} "
                  f"({m['contact_name']!r}) -> owner {m['to_owner']}")
    return tally


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Actually reassign contacts. Without this: report only, "
                         "write nothing (the default, on purpose).")
    ap.add_argument("--limit", type=int, default=None,
                    help="Only apply the first N moves (for a cautious first real run).")
    args = ap.parse_args()

    kylas = KylasClient()
    moves, stats = build_plan(kylas)

    user_names = {}
    try:
        user_names = kylas.get_users()
    except Exception as exc:
        print(f"[assign] WARNING: could not resolve user names ({exc}) — "
              f"summary will show raw ids")

    print_summary(moves, stats, user_names)

    if not args.apply:
        print("\n[assign] DRY RUN — nothing written. Re-run with --apply to reassign.")
        return 0

    if not moves:
        return 0

    n = len(moves) if args.limit is None else min(args.limit, len(moves))
    print(f"\n[assign] Applying {n} of {len(moves)} reassignment(s)...")
    tally = apply_moves(kylas, moves, args.limit)
    print(f"[assign] Done — {tally['ok']} reassigned, {tally['failed']} failed")
    return 1 if tally["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
