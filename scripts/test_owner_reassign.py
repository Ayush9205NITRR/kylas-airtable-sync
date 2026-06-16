"""
Owner-reassignment self-test.

Proves whether owner changes actually REFLECT in Kylas by reassigning a single
company (and its first contact) and re-reading the record afterwards. If the
primary endpoint doesn't take, it probes alternative endpoint/body shapes and
reports exactly which one works — so we stop guessing.

Usage:
    python scripts/test_owner_reassign.py --company 1775810
    python scripts/test_owner_reassign.py --company 1775810 --owner gurnoor@enout.in
    python scripts/test_owner_reassign.py --company 1775810 --revert

If --owner is omitted, the test auto-picks a target user different from the
record's current owner so the change is always detectable. With --revert the
original owner is restored at the end (non-destructive).
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from utils.kylas_client import KylasClient

BASE = "https://api.kylas.io/v1"


def _owner(obj: dict):
    """Return (owner_id, owner_name) from a company/contact object."""
    ob = obj.get("ownedBy")
    if isinstance(ob, dict):
        return ob.get("id"), ob.get("name")
    return obj.get("ownerId"), None


def _get(session, path: str) -> dict:
    r = session.get(f"{BASE}/{path}", timeout=30)
    r.raise_for_status()
    j = r.json() if r.content else {}
    return j.get("data", j)


def _probe(session, label: str, attempts: list, verify_fn, target_id: int):
    """
    Try each (method, path, body) attempt; after each, re-read via verify_fn
    and report whether the owner became target_id. Returns the first attempt
    that actually reflected, or None.
    """
    for method, path, body in attempts:
        try:
            time.sleep(0.15)
            r = session.request(method, f"{BASE}/{path}", json=body, timeout=30)
            status = r.status_code
        except Exception as e:
            print(f"    [{label}] {method} {path}  body={body} -> ERROR {e}")
            continue
        time.sleep(0.5)  # let Kylas persist
        now_id, now_name = _owner(verify_fn())
        ok = str(now_id) == str(target_id)
        flag = "✅ REFLECTED" if ok else "—"
        print(f"    [{label}] {method} /{path}  body={body} -> HTTP {status}  "
              f"owner now: {now_id} ({now_name})  {flag}")
        if ok:
            return (method, path, body)
    return None


def run(company_id: int, owner_email: str = None, revert: bool = False):
    client = KylasClient()
    session = client.session

    print("=" * 64)
    print(f"  OWNER-REASSIGN SELF-TEST — company {company_id}")
    print("=" * 64)

    # Resolve the available users (email -> id).
    email_to_id = client.get_users_by_email()
    print(f"\nResolved {len(email_to_id)} Kylas users")

    # ── Company ───────────────────────────────────────────────────────────
    company = _get(session, f"companies/{company_id}")
    cur_id, cur_name = _owner(company)
    print(f"\nCompany '{company.get('name')}'  current owner: {cur_id} ({cur_name})")

    # Pick a target user_id.
    if owner_email:
        target_id = email_to_id.get(owner_email.strip().lower())
        if not target_id:
            target_id = client.find_user_id_by_email(owner_email)
        if not target_id:
            print(f"  [ABORT] owner '{owner_email}' not found in Kylas")
            return
    else:
        target_id = next((uid for uid in email_to_id.values()
                          if str(uid) != str(cur_id)), None)
        if not target_id:
            print("  [ABORT] could not pick a target user different from current")
            return
    print(f"Target owner user_id: {target_id}\n")

    print("Probing COMPANY reassignment endpoints:")
    co_attempts = [
        ("PUT",   f"companies/{company_id}/owner", {"ownerId": target_id}),
        ("PATCH", f"companies/{company_id}/owner", {"ownerId": target_id}),
        ("PUT",   f"companies/{company_id}/owner", {"ownedBy": {"id": target_id}}),
    ]
    co_win = _probe(session, "company", co_attempts,
                    lambda: _get(session, f"companies/{company_id}"), target_id)
    if not co_win:
        print("    → dedicated endpoint did not reflect; testing client.update_company_owner()")
        client.update_company_owner(company_id, target_id)
        time.sleep(0.5)
        nid, nname = _owner(_get(session, f"companies/{company_id}"))
        print(f"    client method → owner now: {nid} ({nname})  "
              f"{'✅ REFLECTED' if str(nid)==str(target_id) else '❌ STILL NOT REFLECTING'}")

    # ── Contact (first one linked to the company) ────────────────────────
    print("\nFetching contacts for the company:")
    contacts = client.get_contacts_by_company(company_id)
    method_used = getattr(client, "_contact_method", None)
    print(f"  → {len(contacts)} contacts found "
          f"(via: {'list endpoint' if method_used == 'list' else method_used})")

    if contacts:
        ct = contacts[0]
        ct_id = ct.get("id")
        ct_obj = _get(session, f"contacts/{ct_id}")
        ccur_id, ccur_name = _owner(ct_obj)
        print(f"\nContact {ct_id} '{ct_obj.get('firstName','')} {ct_obj.get('lastName','')}'"
              f"  current owner: {ccur_id} ({ccur_name})")

        print("Probing CONTACT reassignment endpoints:")
        ct_attempts = [
            ("PUT",   f"contacts/{ct_id}/owner", {"ownerId": target_id}),
            ("PATCH", f"contacts/{ct_id}/owner", {"ownerId": target_id}),
            ("PUT",   f"contacts/{ct_id}/owner", {"ownedBy": {"id": target_id}}),
        ]
        ct_win = _probe(session, "contact", ct_attempts,
                        lambda: _get(session, f"contacts/{ct_id}"), target_id)
        if not ct_win:
            print("    → dedicated endpoint did not reflect; testing client.update_contact_owner()")
            client.update_contact_owner(ct_id, target_id)
            time.sleep(0.5)
            nid, nname = _owner(_get(session, f"contacts/{ct_id}"))
            print(f"    client method → owner now: {nid} ({nname})  "
                  f"{'✅ REFLECTED' if str(nid)==str(target_id) else '❌ STILL NOT REFLECTING'}")
    else:
        print("  (no contacts to test)")

    # ── Optional revert ──────────────────────────────────────────────────
    if revert and cur_id:
        print(f"\nReverting company owner back to {cur_id}...")
        client.update_company_owner(company_id, cur_id)

    print("\n" + "=" * 64)
    print("  DONE — look for ✅ REFLECTED above to see which endpoint works")
    print("=" * 64)


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--company", required=True, type=int, help="Kylas company id to test")
    parser.add_argument("--owner",   default=None, help="Target owner email (optional)")
    parser.add_argument("--revert",  action="store_true", help="Restore original owner after test")
    args = parser.parse_args()
    run(company_id=args.company, owner_email=args.owner, revert=args.revert)
