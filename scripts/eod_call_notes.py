"""
End-of-day: write each deal's calls for the day into its Kylas Notes section.

    python scripts/eod_call_notes.py --dry-run          # print, write nothing
    python scripts/eod_call_notes.py --deal 4383813 --dry-run
    python scripts/eod_call_notes.py                    # write the notes

WHY THE CANDIDATE SET LOOKS LIKE THIS
Call logs can only be read per entity, so this needs a list of deals to ask
about. Everything cheaper was measured and ruled out:

  - a call does NOT bump the parent deal's updatedAt (deal 4383813: newest
    call created 2026-08-21T20:34Z, deal updatedAt still 2026-06-26T09:09Z),
    and get_deals(since=24h) returned 0 deals
  - a call does NOT bump the contact either (contact 5362056: updatedAt
    2026-04-08, ten call logs including today's)
  - cfLastCalledAt is maintained by something other than call logs: of 37,015
    contacts, 10,255 carry it and 0 matched today

So the cheapest correct set is OPEN deals, from one bulk sweep. Closed deals
do not get called. Everything after that is one get_call_logs per open deal,
which is why --dry-run reports the deal count and estimated cost before
anything is scheduled against it.

ATTRIBUTION
The note is authored by the API key's user, but the rep names inside it are
read from each call's createdBy -- the reps log their own calls, so those are
the real callers. That is the whole point of summarising rather than writing
call logs ourselves.

IDEMPOTENCY
Each note ends with a [calls YYYY-MM-DD] marker. One get_all_notes() sweep
finds the markers already present, so re-running -- or a manual re-dispatch
after the scheduled one -- never double-posts.
"""
import argparse
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

from utils.kylas_client import KylasClient

IST = timezone(timedelta(hours=5, minutes=30))
MARKER = "[calls {date}]"
CLOSED_HINTS = ("closed", "won", "lost", "unqualified", "junk", "dropped")


def _ist_day_bounds(day):
    """UTC (start, end) for an IST calendar day, since Kylas stores UTC."""
    start_ist = datetime(day.year, day.month, day.day, tzinfo=IST)
    return (start_ist.astimezone(timezone.utc),
            (start_ist + timedelta(days=1)).astimezone(timezone.utc))


def _parse_utc(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_open(deal):
    stage = ((deal.get("pipelineStage") or {}).get("name") or "").lower()
    return not any(h in stage for h in CLOSED_HINTS)


def _fmt_duration(seconds):
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return ""
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


def build_note(calls, day):
    """The note body for one deal's calls on one day."""
    lines = [f"Call log — {day.strftime('%d %b %Y')}", ""]
    connected = 0
    total = 0
    for c in sorted(calls, key=lambda x: str(x.get("startTime") or "")):
        started = _parse_utc(c.get("startTime"))
        when = started.astimezone(IST).strftime("%I:%M %p").lstrip("0") if started else "?"
        who = (c.get("createdBy") or {}).get("name") or "unknown"
        outcome = str(c.get("outcome") or "").replace("_", " ")
        row = f"• {who} — {when} — {outcome}"
        if c.get("duration"):
            row += f" — {_fmt_duration(c['duration'])}"
            total += int(c["duration"])
        if str(c.get("outcome")) == "connected":
            connected += 1
        lines.append(row)
    summary = f"{len(calls)} call{'s' if len(calls) != 1 else ''} · {connected} connected"
    if total:
        summary += f" · {_fmt_duration(total)} talk time"
    lines += ["", summary, MARKER.format(date=day.isoformat())]
    return "\n".join(lines)


def already_noted(client, day, max_pages):
    """Deal ids that already carry today's marker, from one notes sweep."""
    marker = MARKER.format(date=day.isoformat())
    done = set()
    try:
        notes = client.get_all_notes(max_pages=max_pages)
    except Exception as exc:
        print(f"  [WARN] could not read existing notes ({exc}); "
              f"proceeding could double-post, so nothing will be written.")
        return None
    for n in notes:
        desc = str(n.get("description") or "")
        if marker not in desc:
            continue
        for rel in n.get("relations") or []:
            if str(rel.get("entityType") or "").upper() == "DEAL" and rel.get("entityId"):
                done.add(int(rel["entityId"]))
    return done


def main():
    p = argparse.ArgumentParser(description="Write today's calls into each deal's notes.")
    p.add_argument("--date", help="IST date YYYY-MM-DD (default: today)")
    p.add_argument("--deal", type=int, help="Only this deal (skips the sweep)")
    p.add_argument("--dry-run", action="store_true", help="Print, write nothing")
    p.add_argument("--include-closed", action="store_true",
                   help="Also scan closed deals (much slower)")
    p.add_argument("--max-deals", type=int, default=0,
                   help="Stop after N candidate deals (0 = no limit)")
    p.add_argument("--note-pages", type=int, default=20,
                   help="Pages of recent notes to scan for the marker")
    args = p.parse_args()

    load_dotenv()
    client = KylasClient()

    day = (datetime.strptime(args.date, "%Y-%m-%d").date() if args.date
           else datetime.now(IST).date())
    start, end = _ist_day_bounds(day)
    print(f"EOD call notes for {day} IST  (UTC {start:%Y-%m-%d %H:%M} → {end:%Y-%m-%d %H:%M})")

    # Candidate deals.
    if args.deal:
        candidates = [args.deal]
        print(f"  single deal: {args.deal}")
    else:
        print("  sweeping deals...")
        deals = client.get_deals()
        opened = [d for d in deals if args.include_closed or _is_open(d)]
        candidates = [int(d["id"]) for d in opened if d.get("id")]
        print(f"  {len(deals)} deals, {len(candidates)} "
              f"{'total' if args.include_closed else 'open'} to scan")
        print(f"  estimated call-log reads: ~{len(candidates) * 0.35 / 60:.1f} min")
    if args.max_deals:
        candidates = candidates[:args.max_deals]
        print(f"  capped at {len(candidates)} deal(s)")

    # Which deals already have today's note?
    done = set()
    if not args.dry_run:
        done = already_noted(client, day, args.note_pages)
        if done is None:
            return 1
        print(f"  {len(done)} deal(s) already carry today's marker")

    # Collect today's calls per deal.
    by_deal = defaultdict(list)
    for i, did in enumerate(candidates, 1):
        if did in done:
            continue
        for c in client.get_call_logs(did, "deal"):
            started = _parse_utc(c.get("startTime"))
            if started and start <= started < end:
                by_deal[did].append(c)
        if i % 250 == 0:
            print(f"    ...{i}/{len(candidates)} deals scanned, "
                  f"{len(by_deal)} with calls today")

    print(f"\n  {len(by_deal)} deal(s) had calls on {day}")
    if not by_deal:
        print("  nothing to write.")
        return 0

    written = failed = 0
    for did, calls in sorted(by_deal.items()):
        body = build_note(calls, day)
        if args.dry_run:
            print(f"\n--- deal {did} ({len(calls)} call(s)) ---")
            print(body)
            continue
        res = client.create_note("DEAL", did, body)
        if res.get("ok"):
            written += 1
            print(f"  deal {did}: note {res.get('id')} written ({len(calls)} calls)")
        else:
            failed += 1
            print(f"  deal {did}: FAILED {res.get('status')} {res.get('error')[:200]}")

    if args.dry_run:
        print(f"\n[DRY RUN] would write {len(by_deal)} note(s). Nothing was written.")
    else:
        print(f"\nWrote {written}, failed {failed}.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
