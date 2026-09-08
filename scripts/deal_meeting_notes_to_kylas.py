#!/usr/bin/env python3
"""
Push each Airtable Deal's "Updated Meeting Notes" field into a Note on that
Deal in Kylas. The other half of the Otter -> Zapier -> Kylas pipeline; this
is the "...-> Kylas" step.

WHY AIRTABLE IS THE MIDDLE STEP, NOT A DIRECT ZAPIER-TO-KYLAS WRITE
Zapier's native Kylas CRM app exposes only Create Contact / Create Company as
actions -- no search-by-email, no create-note. Airtable's native Zapier app
(and, in the current Zap, a Code-by-Zapier step calling Airtable's REST API
directly) can do the rest, and this repo already keeps Airtable's Deals table
synced FROM Kylas. So the email -> deal lookup and the write both happen on
the Zapier side, and this script -- the same route Kylas Deal notes already
flow through, see deal_remarks_to_notes.py -- carries it the rest of the way
into Kylas.

THE ZAP (built in Zapier, not this repo) matches a Deal directly by its
"Email (POC)" field against the meeting's external attendee -- no separate
Contacts-table hop needed, since the POC email lives on the Deal itself:
  1. Trigger:  Otter.ai "New Recording"
  2. Code:     extract the external (non-@enout.in) attendee's email
  3. Code:     find the Deal by Email (POC) == that address, then PATCH it:
                 - "Previous Meeting Notes" <- whatever "Updated Meeting
                   Notes" currently holds (the ROLLING one-step-back shift,
                   read below)
                 - "Updated Meeting Notes"  <- this meeting's transcript

TWO-FIELD ROLLING HISTORY, not accumulation. A Deal sees many meetings over
its life; concatenating every transcript into one ever-growing field would
make it unreadable and would re-push the entire history to Kylas on every
run (since this script pushes whatever text currently sits in the field).
Instead, only the immediately preceding version is kept, in "Previous
Meeting Notes", and "Updated Meeting Notes" always holds just the latest:

    run 1 (Meeting 1): Previous <- "" (nothing yet),   Updated <- Meeting 1
    run 2 (Meeting 2): Previous <- "Meeting 1",         Updated <- Meeting 2
    run 3 (Meeting 3): Previous <- "Meeting 2",         Updated <- Meeting 3

The shift happens on the ZAPIER side (it already has the deal record loaded
to do the POC-email match, so it reads the old Updated value and writes both
fields in one PATCH). This script only ever reads "Updated Meeting Notes" --
"Previous Meeting Notes" is a same-base historical reference for humans
browsing Airtable, deliberately never pushed to Kylas again here, since its
content was already pushed in the run where IT was the latest.

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
TABLE            = "Deals"
NOTES_FIELD      = "Updated Meeting Notes"
PREV_NOTES_FIELD = "Previous Meeting Notes"
DEAL_ID_FIELD    = "Kylas Deal Id"
DEAL_NAME_FIELD  = "Deal Name"


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
    """Add the 'Updated Meeting Notes' / 'Previous Meeting Notes' columns to
    Airtable's Deals table if either is not already there.

    This table is otherwise maintained by modules/03_deal_sync.py's Kylas ->
    Airtable field map (config/field_map.json), which has no entry for these
    fields on purpose: they flow the opposite direction (the Zap writes both,
    this script only ever reads NOTES_FIELD back), so the regular sync would
    never create them. Without this check, the Zap's PATCH would fail the
    first time it tries to write a column that doesn't exist yet.
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
    ok = True
    for field in (NOTES_FIELD, PREV_NOTES_FIELD):
        if field in have:
            continue
        resp = requests.post(f"{META}/{base_id}/tables/{table['id']}/fields",
                             json={"name": field, "type": "multilineText"},
                             headers=headers, timeout=30)
        field_ok = resp.status_code in (200, 201)
        print(f"[meeting-notes] {'+ added' if field_ok else '! could not add'} column "
              f"{field!r} to {TABLE!r}"
              + ("" if field_ok else f" ({resp.status_code} {resp.text[:150]})"))
        ok = field_ok and ok
    return ok


def eligible_rows(at: AirtableClient) -> list:
    """[(deal_id, deal_name, notes_text), ...] for Deals rows with a non-blank
    Updated Meeting Notes field and a known Kylas Deal Id."""
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
        description="Push each Airtable Deal's Updated Meeting Notes field into a Kylas Note on that deal")
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
    print(f"Eligible rows (non-blank Updated Meeting Notes + Kylas Deal Id): {eligible_n}")

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
