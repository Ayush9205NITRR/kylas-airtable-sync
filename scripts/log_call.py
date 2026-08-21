"""
Log a call against a Kylas deal, and list the calls already on it.

    python scripts/log_call.py --deal 4383813 --by Hritik \
        --duration 270 --note "Nov offsite discussed, POC wants pricing deck"

    python scripts/log_call.py --deal 4383813 --show
    python scripts/log_call.py --deal 4383813 --by Hritik --dry-run

The rep named by --by is written into the call's note. Kylas stamps
createdBy/owner from the API key and will not accept an override -- verified
against call log 43843294, where PATCHing every owner shape left updatedAt
untouched -- so the UI's "Logged By" reads as the key's user regardless. The
note is the only place the real caller can live until per-rep API keys exist.

Contacts default to the deal's own associated contacts, so the call links to
the same people the deal does without having to name them.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

from utils.kylas_client import KylasClient


def _deal_contacts(client, deal_id):
    """(contact_ids, first phone number) from the deal's associated contacts."""
    try:
        deal = client.get_deal(int(deal_id))
    except Exception as exc:
        print(f"  [WARN] could not read deal {deal_id}: {exc}")
        return [], ""
    assoc = deal.get("associatedContacts") or []
    ids = []
    for c in assoc:
        cid = c.get("id") if isinstance(c, dict) else c
        if cid:
            ids.append(int(cid))
    phone = ""
    for cid in ids:
        try:
            contact = client.get_contact(cid)
        except Exception:
            continue
        for pn in contact.get("phoneNumbers") or []:
            if isinstance(pn, dict):
                phone = pn.get("value") or pn.get("number") or ""
            elif pn:
                phone = str(pn)
            if phone:
                break
        if phone:
            break
    return ids, phone


def _show(client, deal_id):
    rows = client.get_call_logs(deal_id, "deal")
    print(f"\n{len(rows)} call log(s) on deal {deal_id}:")
    for r in rows:
        who = (r.get("createdBy") or {}).get("name") or "?"
        notes = r.get("notes") or []
        first = (notes[0].get("description") if notes and isinstance(notes[0], dict) else "") or ""
        print(f"  [{r.get('id')}] {r.get('startTime')}  {r.get('callType')}/"
              f"{r.get('outcome')}  {r.get('duration')}s  logged-by={who}")
        if first:
            print(f"        note: {first[:160]}")
    return rows


def main():
    p = argparse.ArgumentParser(description="Log a call against a Kylas deal.")
    p.add_argument("--deal", required=True, help="Kylas deal id")
    p.add_argument("--by", help="Rep who made the call (written into the note)")
    p.add_argument("--duration", type=int, default=0, help="Seconds; connected calls only")
    p.add_argument("--outcome", default="connected",
                   choices=list(KylasClient.CALL_OUTCOMES))
    p.add_argument("--type", dest="call_type", default="outgoing",
                   choices=["outgoing", "incoming"])
    p.add_argument("--phone", default="", help="Override; defaults to the contact's number")
    p.add_argument("--contact", action="append", type=int, default=None,
                   help="Contact id to associate (repeatable); defaults to the deal's")
    p.add_argument("--note", default="", help="What the call was about")
    p.add_argument("--recording", default="", help="URL of the call recording")
    p.add_argument("--dry-run", action="store_true", help="Print the payload, write nothing")
    p.add_argument("--show", action="store_true", help="List existing call logs and exit")
    args = p.parse_args()

    load_dotenv()
    client = KylasClient()

    if args.show:
        _show(client, args.deal)
        return 0

    if not args.by:
        p.error("--by is required (the rep who made the call)")

    contacts = args.contact
    phone = args.phone
    if contacts is None or not phone:
        found_ids, found_phone = _deal_contacts(client, args.deal)
        contacts = contacts if contacts is not None else found_ids
        phone = phone or found_phone

    print(f"Deal {args.deal}  by={args.by}  outcome={args.outcome}  "
          f"duration={args.duration}s  contacts={contacts}  "
          f"phone={'***' + phone[-3:] if phone else '(none)'}")

    res = client.create_call_log(
        deal_id=args.deal, made_by=args.by, duration_seconds=args.duration,
        outcome=args.outcome, call_type=args.call_type, phone_number=phone,
        contact_ids=contacts, note=args.note, recording_url=args.recording,
        dry_run=args.dry_run,
    )

    if args.dry_run:
        import json
        print("\n[DRY RUN] payload that would be POSTed to /v1/call-logs/:")
        print(json.dumps(res.get("payload"), indent=2))
        return 0

    if not res["ok"]:
        print(f"\nFAILED: HTTP {res['status']} {res['error']}")
        return 1

    print(f"\nCreated call log {res['id']} (HTTP {res['status']})")
    _show(client, args.deal)
    print("\nNote: 'logged-by' above is the API key's user, not --by. The rep is")
    print("named in the note text; Kylas does not allow overriding the creator.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
