"""
Test calendar invite: find contacts with a future cfNextCallDate and send
a test invite to verify the SMTP + iCal flow end-to-end.

Usage:
    python scripts/test_calendar_invite.py
    python scripts/test_calendar_invite.py --dry-run   # show what would be sent, no email
    python scripts/test_calendar_invite.py --id 12345  # test with a specific Kylas contact ID
"""
import argparse
import json
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

from utils.kylas_client import KylasClient
from utils.calendar_invite import send_invite


def _parse_date(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    if raw[0].isdigit():
        return raw[:10]
    try:
        return datetime.strptime(raw.split(" at ")[0].strip(), "%b %d, %Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print details but don't send email")
    parser.add_argument("--id", type=int, dest="contact_id", help="Test with a specific Kylas contact ID")
    args = parser.parse_args()

    kylas = KylasClient()

    # Build email map
    user_email_map = {}
    try:
        _tp = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "team.json")
        with open(_tp) as f:
            user_email_map = json.load(f).get("kylas_user_emails", {})
    except Exception:
        pass
    try:
        user_email_map.update(kylas.get_user_emails())
    except Exception as e:
        print(f"[WARNING] Could not fetch user emails from Kylas: {e}")

    today = date.today().isoformat()
    print(f"Today: {today}\n")

    # Fetch contacts to find ones with future cfNextCallDate
    if args.contact_id:
        contacts = [kylas.get_contact(args.contact_id)]
        print(f"Fetched contact {args.contact_id}")
    else:
        print("Fetching recent contacts (last 72h)...")
        from datetime import timezone, timedelta
        since = (datetime.now(timezone.utc) - timedelta(hours=72)).strftime("%Y-%m-%dT%H:%M:%SZ")
        contacts = kylas.get_contacts(since=since)
        print(f"Fetched {len(contacts)} contacts")

    found = []
    for ct in contacts:
        cf = ct.get("customFieldValues") or {}
        raw_nc = cf.get("cfNextCallDate") or ""
        nc_date = _parse_date(raw_nc)
        if nc_date and nc_date >= today:
            found.append((ct, nc_date))

    if not found:
        print("\nNo contacts with a future cfNextCallDate found in this batch.")
        print("Tips:")
        print("  • Use --id <contact_id> to test with a specific contact")
        print("  • Make sure the contact has cfNextCallDate set in Kylas")
        return

    print(f"\nFound {len(found)} contact(s) with future next call date:\n")
    for ct, nc_date in found[:5]:  # show at most 5
        cf       = ct.get("customFieldValues") or {}
        name     = ct.get("name") or f"Contact {ct['id']}"
        co       = ct.get("company")
        co_name  = co.get("name", "") if isinstance(co, dict) else ""
        emails   = ct.get("emails") or []
        phones   = ct.get("phoneNumbers") or []
        ob       = ct.get("ownedBy") or {}
        owner_em = ""
        if isinstance(ob, dict):
            owner_em = (ob.get("email") or ob.get("emailId") or "").strip().lower()
        if not owner_em:
            o_name = ob.get("name") or "" if isinstance(ob, dict) else ""
            owner_em = user_email_map.get(o_name, "")

        print(f"  Contact : {name}  (ID {ct['id']})")
        print(f"  Company : {co_name or '—'}")
        print(f"  Date    : {nc_date}")
        print(f"  Owner   : {owner_em or '(no email found)'}")
        print(f"  Raw cfNextCallDate: {cf.get('cfNextCallDate', '(empty)')}")

        if args.dry_run:
            print("  [DRY RUN] Would send invite — skipping\n")
            continue

        print(f"  Sending invite...")
        ok = send_invite(
            contact_id=str(ct["id"]),
            contact_name=name,
            contact_email=emails[0].get("value", "") if emails else "",
            contact_phone=phones[0].get("value", "") if phones else "",
            company_name=co_name,
            remarks=cf.get("cfRemarks") or "",
            call_date=date.fromisoformat(nc_date),
            owner_email=owner_em,
        )
        print(f"  {'✓ Sent' if ok else '✗ Failed'}\n")


if __name__ == "__main__":
    main()
