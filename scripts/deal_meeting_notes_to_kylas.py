#!/usr/bin/env python3
"""
Push each Airtable Deal's "Meeting Notes" field into a Note on that Deal in
Kylas. The other half of the Otter -> Zapier -> Kylas pipeline; this is the
"...-> Kylas" step.

WHY AIRTABLE IS THE MIDDLE STEP, NOT A DIRECT ZAPIER-TO-KYLAS WRITE
Zapier's native Kylas CRM app exposes only Create Contact / Create Company as
actions -- no search-by-email, no create-note. Airtable's native Zapier app
has full Find/Create/Update Record actions, and this repo already keeps
Airtable's Contacts and Deals tables synced FROM Kylas (Kylas Contact Id /
Kylas Deal Id are already on every row, via modules/02_contact_sync.py and
modules/03_deal_sync.py). So the email -> deal lookup and the write both
happen natively in Airtable, and this script -- the same route Kylas Deal
notes already flow through, see deal_remarks_to_notes.py -- carries it the
rest of the way into Kylas.

THE ZAP (built in Zapier, not this repo):
  1. Trigger:      Otter.ai "New Transcript" (Pro plan and up includes Zapier)
  2. Find Record:  Airtable "Contacts" table, Email = <attendee email from step 1>
  3. Find Record:  Airtable "Deals" table, Kylas Contact Id = <step 2's Kylas Contact Id>
                   A contact with more than one deal returns whichever row the
                   Deals view hits first -- sort that view by most-recently-
                   updated if which deal wins should track recency.
  4. Update Record: Airtable "Deals", that row's "Meeting Notes" = the transcript
                   A plain overwrite is fine, no append needed: idempotency
                   below is by CONTENT (like deal_remarks_to_notes.py), so
                   each new transcript still produces its own Kylas note even
                   though the Airtable field only ever holds the latest one.

IDEMPOTENT, same mechanism as deal_remarks_to_notes.py: existing Kylas notes
on the deal are read once (get_all_notes) and a note is only created if this
exact (normalized) text is not already there. A Zap firing twice, or a manual
re-run, never double-posts -- and because it is content-keyed rather than
"has this deal already got a note today", a deal with two meetings in one day
still gets two notes.

    python scripts/deal_meeting_notes_to_kylas.py --probe     # one note, verified
    python scripts/deal_meeting_notes_to_kylas.py --dry-run   # list only, write nothing
    python scripts/deal_meeting_notes_to_kylas.py             # real run, all eligible
    python scripts/deal_meeting_notes_to_kylas.py --limit 20
"""
import argparse
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.kylas_client import KylasClient       # noqa: E402
from utils.airtable_client import AirtableClient  # noqa: E402

_WS = re.compile(r"\s+")
TABLE           = "Deals"
NOTES_FIELD     = "Meeting Notes"
DEAL_ID_FIELD   = "Kylas Deal Id"
DEAL_NAME_FIELD = "Deal Name"


def _html_unescape(text: str) -> str:
    """Reverse KylasClient._html_escape -- see deal_remarks_to_notes.py's copy
    of this same helper for why: get_all_notes() strips tags but not entities,
    so comparing un-escaped text on both sides is what keeps idempotency
    working for notes containing &, <, >, or quotes."""
    return (str(text or "")
            .replace("&lt;", "<").replace("&gt;", ">")
            .replace("&quot;", '"').replace("&#39;", "'")
            .replace("&nbsp;", " ").replace("&amp;", "&"))


def _normalize(text: str) -> str:
    return _WS.sub(" ", _html_unescape(text)).strip()


def ensure_notes_field(base_id: str, headers: dict) -> bool:
    """Add the 'Meeting Notes' column to Airtable's Deals table if it is not
    already there.

    This table is otherwise maintained by modules/03_deal_sync.py's Kylas ->
    Airtable field map (config/field_map.json), which has no entry for this
    field on purpose: it flows the opposite direction (Zapier writes it here,
    this script reads it), so the regular sync would never create it. Without
    this check, the field would need to be added by hand in the Airtable UI
    before the Zap could ever write to it.
    """
    import requests
    META = "https://api.airtable.com/v0/meta/bases"
    r = requests.get(f"{META}/{base_id}/tables", headers=headers, timeout=30)
    r.raise_for_status()
    table = next((t for t in r.json().get("tables", []) if t["name"] == TABLE), None)
    if table is None:
        print(f"[meeting-notes] ERROR: Airtable table {TABLE!r} does not exist")
        return False
    have = {f["name"] for f in table.get("fields", [])}
    if NOTES_FIELD in have:
        return True
    resp = requests.post(f"{META}/{base_id}/tables/{table['id']}/fields",
                         json={"name": NOTES_FIELD, "type": "multilineText"},
                         headers=headers, timeout=30)
    ok = resp.status_code in (200, 201)
    print(f"[meeting-notes] {'+ added' if ok else '! could not add'} column "
          f"{NOTES_FIELD!r} to {TABLE!r}"
          + ("" if ok else f" ({resp.status_code} {resp.text[:150]})"))
    return ok


def eligible_rows(at: AirtableClient) -> list:
    """[(deal_id, deal_name, notes_text), ...] for Deals rows with a non-blank
    Meeting Notes field and a known Kylas Deal Id."""
    out = []
    for r in at.table.all():
        f = r["fields"]
        deal_id = str(f.get(DEAL_ID_FIELD, "")).strip()
        notes = str(f.get(NOTES_FIELD, "") or "").strip()
        if deal_id and notes:
            out.append((deal_id, f.get(DEAL_NAME_FIELD, "") or f"Deal {deal_id}", notes))
    return out


def existing_notes_by_deal(kylas: KylasClient) -> dict:
    """{deal_id(str): set(normalized existing note texts)} for DEAL-relation notes."""
    by_deal: dict = {}
    for note in kylas.get_all_notes():
        norm = _normalize(note.get("text") or "")
        if not norm:
            continue
        for entity_type, entity_id in (note.get("relations") or []):
            if entity_type == "DEAL":
                by_deal.setdefault(entity_id, set()).add(norm)
    return by_deal


def plan(at: AirtableClient, kylas: KylasClient) -> tuple:
    """Returns (not_noted, already_count). not_noted: [(deal_id, deal_name,
    notes, normalized), ...] -- rows whose current Meeting Notes text is not
    yet on the deal in Kylas."""
    rows = eligible_rows(at)
    by_deal = existing_notes_by_deal(kylas)
    not_noted, already = [], 0
    for deal_id, deal_name, notes in rows:
        norm = _normalize(notes)
        if norm in by_deal.get(deal_id, set()):
            already += 1
        else:
            not_noted.append((deal_id, deal_name, notes, norm))
    return not_noted, already


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Push each Airtable Deal's Meeting Notes field into a Kylas Note on that deal")
    ap.add_argument("--probe", action="store_true",
                    help="Create exactly ONE note on ONE eligible deal, re-read notes, confirm it appears")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print eligible/already-noted/will-create counts + up to 8 samples; write nothing")
    ap.add_argument("--limit", type=int, default=None, metavar="N",
                    help="Cap the number of notes created in a real run (safety)")
    args = ap.parse_args()

    headers = {"Authorization": f"Bearer {os.environ['AIRTABLE_PAT']}",
               "Content-Type": "application/json"}
    if not ensure_notes_field(os.environ["AIRTABLE_BASE_ID"], headers):
        return 1

    at = AirtableClient(TABLE)
    kylas = KylasClient()

    print("Fetching Deals from Airtable...")
    not_noted, already = plan(at, kylas)
    eligible_n = len(not_noted) + already
    print(f"Eligible rows (non-blank Meeting Notes + Kylas Deal Id): {eligible_n}")

    if args.probe:
        if not not_noted:
            print("PROBE FAILED: no eligible, not-already-noted row available to probe")
            return 1
        deal_id, deal_name, notes, norm = not_noted[0]
        print(f"Probing deal {deal_id} ({deal_name}) — notes: {notes[:120]!r}")
        result = kylas.create_note("DEAL", deal_id, notes)
        print(f"create_note result: {result}")
        if not result.get("ok"):
            print(f"PROBE FAILED: {result.get('error')}")
            return 1
        time.sleep(1.0)  # let Kylas persist/index before re-reading
        refreshed = existing_notes_by_deal(kylas)
        if norm in refreshed.get(deal_id, set()):
            print("PROBE OK")
            return 0
        print("PROBE FAILED: create_note returned ok=True but the note text was "
              "not found on re-read of get_all_notes()")
        return 1

    if args.dry_run:
        print(f"\nEligible rows: {eligible_n} (already-noted: {already}, "
              f"will-create: {len(not_noted)})")
        print("\nSamples (deal_id | deal name | notes[:120]):")
        for deal_id, deal_name, notes, _ in not_noted[:8]:
            print(f"  {deal_id} | {deal_name} | {notes[:120]}")
        return 0

    todo = not_noted[:args.limit] if args.limit else not_noted
    print(f"\nCreating notes for {len(todo)} deal(s) "
          f"(eligible: {eligible_n}, already-noted: {already})"
          + (f" [--limit {args.limit}]" if args.limit else ""))

    created, failed = 0, 0
    for deal_id, deal_name, notes, _ in todo:
        result = kylas.create_note("DEAL", deal_id, notes)
        if result.get("ok"):
            created += 1
            print(f"  [ok] deal {deal_id} ({deal_name}) -> note {result.get('id')}")
        else:
            failed += 1
            print(f"  [FAIL {result.get('error')}] deal {deal_id} ({deal_name})")

    print(f"\nCreated: {created} | Skipped(existing): {already} | Failed: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    sys.exit(main())
