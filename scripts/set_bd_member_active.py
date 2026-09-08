#!/usr/bin/env python3
"""
Flip the Active checkbox on specific "BD Members" Airtable rows, by email.

Active is the single source of truth for the BD roster: bd_company_funnel.
bd_roster() reads it to decide who counts as active, and bd_metrics_long.
send_team_digest() reads the same roster for who receives the digest. A rep
who has left but is still ticked Active keeps showing up as an all-zero row
in the digest AND stays on the recipient list — this script is how that gets
corrected, from either direction (deactivate a leaver, reactivate a rejoin).

Matched on EMAIL, not the Name column: BD Members stores short first names
("Gaurav") while Kylas resolves full ones ("Gaurav Kumar"), so name matching
is ambiguous or wrong far more often than email is.

    python scripts/set_bd_member_active.py --inactive a@x b@x   # dry run
    python scripts/set_bd_member_active.py --inactive a@x b@x --apply
    python scripts/set_bd_member_active.py --active a@x --apply
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.airtable_client import AirtableClient   # noqa: E402

TABLE = "BD Members"


def build_plan(at: AirtableClient, set_inactive: set, set_active: set) -> tuple:
    """
    Returns (changes, not_found).
    changes: [{"record_id", "name", "email", "from", "to"}] — only rows whose
    Active value actually needs to flip.
    not_found: emails passed in that don't match any row in the table.
    """
    rows = at.table.all()
    by_email = {}
    for r in rows:
        email = str(r["fields"].get("Email", "")).strip().lower()
        if email:
            by_email[email] = r

    changes = []
    for email, want in [(e, False) for e in set_inactive] + [(e, True) for e in set_active]:
        row = by_email.get(email.strip().lower())
        if row is None:
            continue
        current = bool(row["fields"].get("Active", True))
        if current != want:
            changes.append({
                "record_id": row["id"], "name": row["fields"].get("Name", ""),
                "email": email, "from": current, "to": want,
            })

    requested = {e.strip().lower() for e in set_inactive | set_active}
    not_found = requested - set(by_email)
    return changes, not_found


def apply_changes(at: AirtableClient, changes: list) -> int:
    ok = 0
    for c in changes:
        at.table.update(c["record_id"], {"Active": c["to"]})
        ok += 1
        print(f"  [OK] {c['name']!r} ({c['email']}) — Active: "
              f"{c['from']} -> {c['to']}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inactive", nargs="+", default=[], metavar="EMAIL",
                    help="email(s) to set Active=False")
    ap.add_argument("--active", nargs="+", default=[], metavar="EMAIL",
                    help="email(s) to set Active=True")
    ap.add_argument("--apply", action="store_true",
                    help="actually write. Without this: report only (the default).")
    args = ap.parse_args()

    if not args.inactive and not args.active:
        print("[members] Nothing to do — pass --inactive and/or --active with email(s).")
        return 0

    at = AirtableClient(TABLE)
    changes, not_found = build_plan(at, set(args.inactive), set(args.active))

    if not_found:
        print(f"[members] WARNING: no {TABLE!r} row matches: {sorted(not_found)}")

    if not changes:
        print(f"[members] Nothing to change — every requested email already "
              f"has the Active value asked for.")
        return 0

    print(f"\n[members] {len(changes)} row(s) would change:")
    for c in changes:
        print(f"  {c['name']!r} ({c['email']}) — Active: {c['from']} -> {c['to']}")

    if not args.apply:
        print("\n[members] DRY RUN — nothing written. Re-run with --apply to write.")
        return 0

    print()
    ok = apply_changes(at, changes)
    print(f"\n[members] Done — {ok} row(s) updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
