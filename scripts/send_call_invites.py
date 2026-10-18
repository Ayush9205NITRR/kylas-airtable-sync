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
import time
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

from utils.kylas_client import KylasClient
from utils.calendar_invite import IST, parse_kylas_datetime, send_invite

KYLAS_CONTACT_URL = "https://app.kylas.io/sales/contacts/details/{contact_id}"
SEND_PACE_SECONDS = 2.0


def _parse_dt(raw: str):
    """Return (date_str, time_str) in IST from the cfNextCallDateCallLater value.
    time_str is 'HH:MM:SS' if present, else ''. Kylas returns this field as UTC;
    the conversion lives in utils.calendar_invite.parse_kylas_datetime.
    """
    return parse_kylas_datetime(raw)


def main():
    parser = argparse.ArgumentParser(description="Send calendar invites for scheduled calls.")
    parser.add_argument("--hours", type=int, default=26,
                        help="Scan contacts updated in the last N hours (default: 26)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print details but do not send email")
    parser.add_argument("--id", type=int, dest="contact_id",
                        help="Process a specific Kylas contact ID instead of scanning")
    parser.add_argument("--ids", default="", metavar="ID,ID,...",
                        help="Process exactly these Kylas contact IDs (comma-separated) "
                             "instead of scanning. Use it to re-send only the invites "
                             "a previous run reported as failed.")
    args = parser.parse_args()

    kylas = KylasClient()

    # Build name→email and id→email maps from team.json + live Kylas
    user_email_map = {}
    user_id_email_map = {}
    try:
        _tp = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "team.json")
        with open(_tp) as f:
            _td = json.load(f)
            user_email_map = _td.get("kylas_user_emails", {})
            for uid, uname in _td.get("kylas_users", {}).items():
                em = user_email_map.get(uname, "")
                if em:
                    user_id_email_map[str(uid)] = em
    except Exception:
        pass
    try:
        user_email_map.update(kylas.get_user_emails())
    except Exception as e:
        print(f"[WARNING] Could not fetch user emails from Kylas: {e}")

    # IST, not the runner's UTC clock: after 18:30 UTC it is already tomorrow
    # in India, and the call dates being compared are IST dates.
    today = datetime.now(IST).date().isoformat()
    print(f"Today: {today}")

    # Fetch contacts
    if args.ids.strip():
        wanted = [int(x) for x in args.ids.replace(" ", "").split(",") if x]
        contacts = [kylas.get_contact(i) for i in wanted]
        print(f"Fetched {len(contacts)} contact(s) by ID")
    elif args.contact_id:
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
    failed_ids = []

    for ct in contacts:
        cf = ct.get("customFieldValues") or {}
        raw_nc = cf.get("cfNextCallDateCallLater") or ""
        nc_date, nc_time = _parse_dt(raw_nc)

        if not nc_date or nc_date < today:
            skipped += 1
            continue

        contact_id = str(ct["id"])
        first = (ct.get("firstName") or "").strip()
        last  = (ct.get("lastName") or "").strip()
        name  = ct.get("name") or f"{first} {last}".strip() or f"Contact {contact_id}"
        co    = ct.get("company")
        if isinstance(co, dict):
            co_name = co.get("name", "")
        elif isinstance(co, int) and co:
            try:
                co_name = kylas.get_company(co).get("name", "")
            except Exception:
                co_name = ""
        else:
            co_name = ""
        emails     = ct.get("emails") or []
        phones     = ct.get("phoneNumbers") or []
        ob         = ct.get("ownedBy") or {}

        owner_em = ""
        if isinstance(ob, dict):
            owner_em = (ob.get("email") or ob.get("emailId") or "").strip().lower()
        if not owner_em:
            o_name = ob.get("name", "") if isinstance(ob, dict) else ""
            owner_em = user_email_map.get(o_name, "")
        if not owner_em:
            o_id = str(ct.get("ownerId") or "")
            if o_id:
                owner_em = user_id_email_map.get(o_id, "")

        kylas_url = KYLAS_CONTACT_URL.format(contact_id=contact_id)

        print(f"\n  Contact : {name}  (ID {contact_id})")
        print(f"  Company : {co_name or '—'}")
        print(f"  Date    : {nc_date}")
        print(f"  Time    : {nc_time or '(all-day)'}")
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
            call_time=nc_time,
        )
        if ok:
            sent += 1
        else:
            failed += 1
            failed_ids.append(contact_id)
        # Each invite opens its own Gmail connection; sent back to back, Gmail
        # starts dropping them. A short gap keeps a large catch-up under that.
        time.sleep(SEND_PACE_SECONDS)

    print(f"\nSent: {sent} | Failed: {failed} | Skipped (no date): {skipped}")
    if failed_ids:
        # Copy-pasteable into --ids / the workflow's contact_id input, so a
        # partial run can be finished without re-sending what already went out.
        print(f"Failed contact IDs: {','.join(failed_ids)}")
        # Non-zero so the workflow turns red. It used to exit 0 with 73 of 156
        # invites lost, which looked identical to a clean run.
        sys.exit(1)


if __name__ == "__main__":
    main()
