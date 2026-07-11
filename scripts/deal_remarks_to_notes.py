"""
deal_remarks_to_notes.py — Copy each Kylas Deal's cfRemarks custom field
into a Note on that deal.

For every deal whose cfRemarks custom field has non-blank text, creates a
Kylas Note (KylasClient.create_note) attached to the deal, via POST /notes,
so the remarks show up in the deal's Notes/activity feed.

Idempotent: before writing anything, existing notes are read once
(get_all_notes) and indexed per deal. A deal is skipped if a note with the
same (normalized) text already exists on it, so re-running never creates
duplicate notes.

Usage:
    python scripts/deal_remarks_to_notes.py --dry-run
    python scripts/deal_remarks_to_notes.py --probe
    python scripts/deal_remarks_to_notes.py                  # real run, all eligible deals
    python scripts/deal_remarks_to_notes.py --limit 20        # real run, capped
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.kylas_client import KylasClient

_WS = re.compile(r"\s+")


def _html_unescape(text: str) -> str:
    """Reverse KylasClient._html_escape.

    get_all_notes() strips HTML tags from a note's description but does NOT
    un-escape entities (only "&nbsp;"), so a note created from remarks
    containing &, <, >, quotes would otherwise never compare equal to the
    original plain-text remarks on a re-run — breaking idempotency for those
    deals. Un-escaping here (on both sides, harmless no-op on plain text)
    keeps the comparison correct.
    """
    return (str(text or "")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", '"')
            .replace("&#39;", "'")
            .replace("&nbsp;", " ")
            .replace("&amp;", "&"))


def _normalize(text: str) -> str:
    """Un-escape + strip + collapse internal whitespace, for idempotency comparisons."""
    return _WS.sub(" ", _html_unescape(text)).strip()


def _remarks(deal: dict) -> str:
    """Return the deal's cfRemarks text, or '' if missing/blank."""
    cfv = deal.get("customFieldValues") or {}
    val = cfv.get("cfRemarks")
    if val is None:
        return ""
    return str(val).strip()


def _deal_name(deal: dict) -> str:
    return str(deal.get("name") or f"Deal {deal.get('id')}")


def _existing_notes_by_deal(kylas: KylasClient) -> dict:
    """Return {deal_id(str): set(normalized existing note texts)} for DEAL-relation notes."""
    by_deal: dict = {}
    for note in kylas.get_all_notes():
        norm = _normalize(note.get("text") or "")
        if not norm:
            continue
        for entity_type, entity_id in (note.get("relations") or []):
            if entity_type == "DEAL":
                by_deal.setdefault(entity_id, set()).add(norm)
    return by_deal


def _eligible_deals(kylas: KylasClient) -> list:
    """Return [(deal, remarks_text), ...] for deals with non-blank cfRemarks."""
    deals = kylas.get_deals()
    out = []
    for d in deals:
        r = _remarks(d)
        if r:
            out.append((d, r))
    return out


def main():
    ap = argparse.ArgumentParser(
        description="Copy each Kylas Deal's cfRemarks custom field into a Note on that deal")
    ap.add_argument("--probe", action="store_true",
                    help="Create exactly ONE note on ONE eligible deal, re-read notes, confirm it appears")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print eligible/already-noted/will-create counts + up to 8 samples; write nothing")
    ap.add_argument("--limit", type=int, default=None, metavar="N",
                    help="Cap the number of notes created in a real run (safety)")
    args = ap.parse_args()

    kylas = KylasClient()

    print("Fetching deals from Kylas...")
    eligible = _eligible_deals(kylas)
    print(f"Eligible deals (non-blank cfRemarks): {len(eligible)}")

    print("Fetching existing notes from Kylas (for idempotency check)...")
    existing_by_deal = _existing_notes_by_deal(kylas)

    not_noted, already = [], 0
    for deal, remarks in eligible:
        norm = _normalize(remarks)
        if norm in existing_by_deal.get(str(deal.get("id")), set()):
            already += 1
        else:
            not_noted.append((deal, remarks, norm))

    # ── --probe ──────────────────────────────────────────────────────────────
    if args.probe:
        if not not_noted:
            print("PROBE FAILED: no eligible, not-already-noted deal available to probe")
            return
        deal, remarks, norm = not_noted[0]
        deal_id = deal.get("id")
        print(f"Probing deal {deal_id} ({_deal_name(deal)}) — remarks: {remarks[:120]!r}")
        result = kylas.create_note("DEAL", deal_id, remarks)
        print(f"create_note result: {result}")
        if not result.get("ok"):
            print(f"PROBE FAILED: {result.get('error')}")
            return
        import time
        time.sleep(1.0)  # let Kylas persist/index before re-reading
        refreshed = _existing_notes_by_deal(kylas)
        if norm in refreshed.get(str(deal_id), set()):
            print("PROBE OK")
        else:
            print("PROBE FAILED: create_note returned ok=True but the note text was "
                  "not found on re-read of get_all_notes()")
        return

    # ── --dry-run ────────────────────────────────────────────────────────────
    if args.dry_run:
        print(f"\nEligible deals: {len(eligible)} "
              f"(already-noted: {already}, will-create: {len(not_noted)})")
        print("\nSamples (deal_id | deal name | remarks[:120]):")
        for deal, remarks, _ in not_noted[:8]:
            print(f"  {deal.get('id')} | {_deal_name(deal)} | {remarks[:120]}")
        return

    # ── default: real run ───────────────────────────────────────────────────
    todo = not_noted[:args.limit] if args.limit else not_noted
    print(f"\nCreating notes for {len(todo)} deal(s) "
          f"(eligible: {len(eligible)}, already-noted: {already})"
          + (f" [--limit {args.limit}]" if args.limit else ""))

    created, failed = 0, 0
    for deal, remarks, _ in todo:
        deal_id = deal.get("id")
        result = kylas.create_note("DEAL", deal_id, remarks)
        if result.get("ok"):
            created += 1
            print(f"  [ok] deal {deal_id} ({_deal_name(deal)}) -> note {result.get('id')}")
        else:
            failed += 1
            print(f"  [FAIL {result.get('error')}] deal {deal_id} ({_deal_name(deal)})")

    print(f"\nCreated: {created} | Skipped(existing): {already} | Failed: {failed}")


if __name__ == "__main__":
    from dotenv import load_dotenv; load_dotenv()
    main()
