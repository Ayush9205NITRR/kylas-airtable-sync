"""
Production calendar-invite sender: scans contacts updated in the last N hours
and sends iCal invites for those whose cfNextCallDate >= today.

Usage:
    python scripts/send_call_invites.py
    python scripts/send_call_invites.py --hours 4       # scan last 4h (default: 2)
    python scripts/send_call_invites.py --dry-run       # print only, no email
    python scripts/send_call_invites.py --id 12345      # specific contact ID
"""
import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

from utils.kylas_client import KylasClient
from utils.calendar_invite import send_invite

KYLAS_CONTACT_URL = "https://app.kylas.io/sales/contacts/details/{contact_id}"


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
    parser = argparse.ArgumentParser(description="Send calendar invites for scheduled calls.")
    parser.add_argument("--hours", type=int, default=26,
                        help="Scan contacts updated in the last N hours (default: 26)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print details but do not send email")
    parser.add_argument("--id", type=int, dest="contact_id",
                        help="Process a specific Kylas contact ID instead of scanning")
    args = parser.parse_args()

    kylas = KylasClient()

    # Build email map: prefer team.json, then live Kylas lookup
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
    print(f"Today: {today}")

    # Fetch contacts
    if args.contact_id:
        contacts = [kylas.get_contact(args.contact_id)]
        print(f"Fetched contact {args.contact_id}")
    else:
        since = (datetime.now(timezone.utc) - timedelta(hours=args.hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"Scanning contacts updated since {since} (last {args.hours}h)...")
        contacts = kylas.get_contacts(since=since)
        print(f"Fetched {len(contacts)} contacts")

    sent = 0
    failed = 0
    skipped = 0

    for ct in contacts:
        cf = ct.get("customFieldValues") or {}
        raw_nc = cf.get("cfNextCallDate") or ""
        nc_date = _parse_date(raw_nc)

        if not nc_date or nc_date < today:
            skipped += 1
            continue

        contact_id = str(ct["id"])
        name       = ct.get("name") or f"Contact {contact_id}"
        co         = ct.get("company")
        co_name    = co.get("name", "") if isinstance(co, dict) else ""
        emails     = ct.get("emails") or []
        phones     = ct.get("phoneNumbers") or []
        ob         = ct.get("ownedBy") or {}

        owner_em = ""
        if isinstance(ob, dict):
            owner_em = (ob.get("email") or ob.get("emailId") or "").strip().lower()
        if not owner_em:
            o_name = ob.get("name", "") if isinstance(ob, dict) else ""
            owner_em = user_email_map.get(o_name, "")

        kylas_url = KYLAS_CONTACT_URL.format(contact_id=contact_id)

        print(f"\n  Contact : {name}  (ID {contact_id})")
        print(f"  Company : {co_name or '—'}")
        print(f"  Date    : {nc_date}")
        print(f"  Owner   : {owner_em or '(no email found)'}")
        print(f"  URL     : {kylas_url}")

        if args.dry_run:
            print("  [DRY RUN] Would send invite — skipping")
            continue

        ok = send_invite(
            contact_id=contact_id,
            contact_name=name,
            contact_email=emails[0].get("value", "") if emails else "",
            contact_phone=phones[0].get("value", "") if phones else "",
            company_name=co_name,
            remarks=cf.get("cfRemarks") or "",
            call_date=date.fromisoformat(nc_date),
            owner_email=owner_em,
            kylas_url=kylas_url,
        )
        if ok:
            sent += 1
        else:
            failed += 1

    print(f"\nSent: {sent} | Failed: {failed} | Skipped (no date): {skipped}")


if __name__ == "__main__":
    main()
