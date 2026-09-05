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


def find_companies_owned_by(kylas: KylasClient, owner_id: int) -> list:
    """Companies whose current Kylas owner is exactly owner_id.

    Diagnostic only — reads companies, writes nothing. For spot-checking a
    suspicious owner (e.g. a shared/admin/service account) before deciding
    whether to exclude its companies from the reassignment cascade: if a
    company is already misowned by that account, cascading it onto every
    contact under it would spread the same bad ownership further.
    """
    companies = kylas.get_companies()
    return [co for co in companies
            if co.get("ownerId") and int(co["ownerId"]) == owner_id]


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


def _current_owner_id(kylas: KylasClient, contact_id) -> int:
    """Read a contact's owner straight back from Kylas, ignoring nothing."""
    ct = kylas.get_contact(contact_id)
    ob = ct.get("ownedBy")
    if isinstance(ob, dict) and ob.get("id"):
        return int(ob["id"])
    oid = ct.get("ownerId")
    return int(oid) if oid else None


def apply_moves(kylas: KylasClient, moves: list, limit: int = None) -> dict:
    """
    Reassign, then VERIFY by reading each contact back — never trust
    update_contact_owner()'s return value alone.

    Its fallback path does a full PUT with `ownedBy` set, but Kylas ignores
    `ownedBy` on that endpoint (its own docstring says so) and the fallback
    also strips `ownerId` from the body before sending. That PUT changes
    nothing about ownership, Kylas is then free to leave (or default) the
    owner to whatever the API key's own account is, and the function still
    returns True — a silent no-op reported as success. This is almost
    certainly the source of contacts turning up owned by the API's own
    service account after a reassignment. Reading back and comparing is the
    only way to know a move actually landed, since the call succeeding
    proves nothing here.
    """
    tally = {"ok": 0, "failed": 0, "unverified": 0}
    for m in moves[:limit] if limit else moves:
        called_ok = kylas.update_contact_owner(m["contact_id"], m["to_owner"])
        if not called_ok:
            tally["failed"] += 1
            print(f"  [FAILED] contact {m['contact_id']} "
                  f"({m['contact_name']!r}) -> owner {m['to_owner']} — API call failed")
            continue
        try:
            actual = _current_owner_id(kylas, m["contact_id"])
        except Exception as exc:
            tally["unverified"] += 1
            print(f"  [UNVERIFIED] contact {m['contact_id']} "
                  f"({m['contact_name']!r}) — call reported success but the "
                  f"read-back to confirm it failed ({exc})")
            continue
        if actual == m["to_owner"]:
            tally["ok"] += 1
        else:
            tally["failed"] += 1
            print(f"  [FAILED] contact {m['contact_id']} ({m['contact_name']!r}) "
                  f"-> wanted owner {m['to_owner']}, Kylas now shows {actual} — "
                  f"the write silently did not take (see docstring above)")
    return tally


def resolve_user_names(kylas: KylasClient) -> dict:
    """
    {user_id (int): name}, team.json as the base with the live API only
    filling gaps.

    kylas.get_users() calls GET /users with no page/size params, so it
    silently returns just the first page — sync_team.py's working fetch
    loops with page/size:100 until it runs out. team.json is kept in sync
    daily by sync_team.yml and already has the full roster, so it is the
    more complete source here; the live call is a bonus for anyone new
    enough not to be in team.json yet.
    """
    names = {}
    tp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "config", "team.json")
    try:
        import json
        with open(tp) as fh:
            for uid, name in (json.load(fh).get("kylas_users") or {}).items():
                names[int(uid)] = name
    except Exception as exc:
        print(f"[assign] WARNING: team.json unreadable ({exc})")
    try:
        for uid, name in (kylas.get_users() or {}).items():
            names.setdefault(int(uid), name)
    except Exception as exc:
        print(f"[assign] WARNING: live user list unavailable ({exc})")
    return names


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Actually reassign contacts. Without this: report only, "
                         "write nothing (the default, on purpose).")
    ap.add_argument("--limit", type=int, default=None,
                    help="Only apply the first N moves (for a cautious first real run).")
    ap.add_argument("--sample-owner-id", type=int, default=None,
                    help="Diagnostic only, writes nothing: list companies currently "
                         "owned by this Kylas user id, e.g. to spot-check a suspicious "
                         "owner in Kylas before deciding whether to exclude it. Skips "
                         "planning/apply entirely.")
    ap.add_argument("--sample-n", type=int, default=3,
                    help="How many sample companies to print with --sample-owner-id "
                         "(default 3).")
    args = ap.parse_args()

    kylas = KylasClient()

    if args.sample_owner_id is not None:
        owned = find_companies_owned_by(kylas, args.sample_owner_id)
        plural = "y" if len(owned) == 1 else "ies"
        print(f"\n[assign] {len(owned)} compan{plural} currently owned by "
              f"Kylas user #{args.sample_owner_id}")
        for co in owned[:args.sample_n]:
            print(f"  company id {co['id']:<10} {co.get('name') or '(no name)'}")
        return 0

    moves, stats = build_plan(kylas)

    user_names = resolve_user_names(kylas)
    print_summary(moves, stats, user_names)

    if not args.apply:
        print("\n[assign] DRY RUN — nothing written. Re-run with --apply to reassign.")
        return 0

    if not moves:
        return 0

    n = len(moves) if args.limit is None else min(args.limit, len(moves))
    print(f"\n[assign] Applying {n} of {len(moves)} reassignment(s)...")
    tally = apply_moves(kylas, moves, args.limit)
    print(f"[assign] Done — {tally['ok']} confirmed reassigned, "
          f"{tally['failed']} failed, {tally['unverified']} could not be checked")
    return 1 if (tally["failed"] or tally["unverified"]) else 0


if __name__ == "__main__":
    sys.exit(main())
