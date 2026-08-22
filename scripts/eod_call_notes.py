"""
End-of-day: write each deal's calls for the day into its Kylas Notes section.

    python scripts/eod_call_notes.py --dry-run          # print, write nothing
    python scripts/eod_call_notes.py --deal 4383813 --dry-run
    python scripts/eod_call_notes.py                    # write the notes

WHY THERE IS NO DEAL SWEEP
The /call-logs endpoint ignores its entityId/entityType filter: it returns the
same tenant-wide page whatever entity you ask for. A dry run over 60 deals
produced the identical two call logs for all 60, and those two belong to a
single deal -- live, that would have posted the same wrong note onto every
one of them. So this reads every call log ONCE and groups locally by the deal
each record itself names in relatedTo. That is both the correct answer and the
cheap one: a paged read instead of a request per deal.

AND WHY IT MUST BE PAGED
The listing IS paged -- `page` and `size` both work, and the response carries
totalElements/totalPages. This script did not use them at first, on a wrong
reading of an early probe that tried page, size and sort together and blamed
all three for `sort`'s 404. The default page size is 10 against a tenant that
holds 5,399 call logs, so every summary was built from the ten most recent
calls and was silently incomplete on every date. It surfaced as a deal whose
contact had two visible calls getting no note at all (deal 4676048, contact
5927888, call log 43734306 -- readable by its own id, absent from page 0).

Three other routes were measured and ruled out before landing here:

  - a call does NOT bump the parent deal's updatedAt (deal 4383813: newest
    call created 2026-08-21T20:34Z, deal updatedAt still 2026-06-26T09:09Z),
    and get_deals(since=24h) returned 0 deals
  - a call does NOT bump the contact either (contact 5362056: updatedAt
    2026-04-08, ten call logs including that day's)
  - cfLastCalledAt is maintained by something other than call logs: of 37,015
    contacts, 10,255 carry it and 0 matched the current day

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


def _ist_day_bounds(day):
    """UTC (start, end) for an IST calendar day, since Kylas stores UTC."""
    start_ist = datetime(day.year, day.month, day.day, tzinfo=IST)
    return (start_ist.astimezone(timezone.utc),
            (start_ist + timedelta(days=1)).astimezone(timezone.utc))


CLOSED_HINTS = ("closed", "won", "lost", "unqualified", "junk", "dropped")


def _is_open(deal):
    stage = ((deal.get("pipelineStage") or {}).get("name") or "").lower()
    return not any(h in stage for h in CLOSED_HINTS)


def contact_to_deals(deals):
    """
    contact id -> ([open deal ids], [all deal ids]).

    Reps log their calls against the CONTACT, not the deal: of this tenant's
    ten call logs, the only two naming a deal are the ones written through the
    API with an explicit relatedTo. So a deal-scoped summary has to bridge the
    gap itself, via each deal's associatedContacts.
    """
    open_by, all_by = defaultdict(list), defaultdict(list)
    for d in deals:
        did = d.get("id")
        if not did:
            continue
        live = _is_open(d)
        for c in d.get("associatedContacts") or []:
            cid = c.get("id") if isinstance(c, dict) else c
            if not cid:
                continue
            all_by[int(cid)].append(int(did))
            if live:
                open_by[int(cid)].append(int(did))
    return open_by, all_by


def deals_for_call(call, open_by, all_by):
    """
    Which deals this call belongs on.

    A call that names its own deal is taken at its word. Otherwise it is
    bridged through the contact it names, preferring that contact's OPEN deals
    and falling back to all of them -- a call landing on a closed deal is worth
    more than a call silently dropped. A contact on several open deals yields
    several, deliberately: under-reporting a rep's work is worse than the same
    call appearing on two deals they are both relevant to.
    """
    named = KylasClient.call_log_relations(call, "deal")
    if named:
        return named, "direct"
    out = set()
    for cid in KylasClient.call_log_relations(call, "contact"):
        out.update(open_by.get(cid) or [])
    if out:
        return out, "via-contact-open"
    for cid in KylasClient.call_log_relations(call, "contact"):
        out.update(all_by.get(cid) or [])
    return out, ("via-contact-closed" if out else "unmapped")


def _parse_utc(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


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
    p.add_argument("--max-deals", type=int, default=0,
                   help="Write at most N notes (0 = no limit)")
    p.add_argument("--note-pages", type=int, default=20,
                   help="Pages of recent notes to scan for the marker")
    args = p.parse_args()

    load_dotenv()
    client = KylasClient()

    day = (datetime.strptime(args.date, "%Y-%m-%d").date() if args.date
           else datetime.now(IST).date())
    start, end = _ist_day_bounds(day)
    print(f"EOD call notes for {day} IST  (UTC {start:%Y-%m-%d %H:%M} → {end:%Y-%m-%d %H:%M})")

    # One sweep of every call log, grouped locally by the deal each one names.
    #
    # There is deliberately no deal sweep here. The /call-logs endpoint ignores
    # its entityId/entityType filter and hands back the same tenant-wide page
    # whatever you ask for -- a dry run over 60 deals produced the identical two
    # call logs for all 60, and those two belong to one deal. Asking per deal is
    # therefore both wrong and needlessly expensive; asking once and grouping by
    # each record's own relatedTo is correct and costs one paged read.
    print("  reading call logs...")
    seeds = [args.deal] if args.deal else client.recent_deal_ids(2)
    # stop_before lets the sweep stop paging once it is safely past the day --
    # but only if it has observed the rows to be in descending time order, so a
    # change of ordering costs pages rather than correctness.
    calls, usable = client.get_all_call_logs(seeds, stop_before=start)
    print(f"  {len(calls)} call log(s) returned (seeds {seeds})")

    if not args.deal and not usable:
        # Either the tenant-wide read stopped being tenant-wide, or it did not
        # reach back past the start of the day. Both look identical to a quiet
        # day and both produce confidently wrong notes -- the second is exactly
        # what put no note on deal 4676048 while its contact had calls. Stop.
        print("  [STOP] the call-log read cannot be trusted for this day:")
        print("         either /call-logs filters by entity now (so one read is")
        print("         NOT tenant-wide and most deals would be missed), or it")
        print("         did not reach back past the start of the day.")
        print("         Re-run per deal with --deal, or widen CALL_LOG_SIZES.")
        return 1

    todays = [c for c in calls
              if (_parse_utc(c.get("startTime")) or datetime.min.replace(tzinfo=timezone.utc))
              and start <= (_parse_utc(c.get("startTime"))
                            or datetime.min.replace(tzinfo=timezone.utc)) < end]
    print(f"  {len(todays)} of them fall on {day}")

    open_by, all_by = {}, {}
    if todays:
        print("  building the contact -> deal index...")
        deals = client.get_deals()
        open_by, all_by = contact_to_deals(deals)
        print(f"  {len(deals)} deals, {len(all_by)} contacts linked to at least one")

    by_deal = defaultdict(list)
    routes = defaultdict(int)
    for c in todays:
        dids, how = deals_for_call(c, open_by, all_by)
        routes[how] += 1
        for did in dids:
            if args.deal and did != args.deal:
                continue
            by_deal[did].append(c)
    if routes:
        print(f"  routing: {dict(routes)}")
    if routes.get("unmapped"):
        print(f"  [WARN] {routes['unmapped']} call(s) name neither a deal nor a "
              f"contact on any deal — they cannot be summarised anywhere.")

    # Which deals already have today's note?
    done = set()
    if not args.dry_run:
        done = already_noted(client, day, args.note_pages)
        if done is None:
            return 1
        print(f"  {len(done)} deal(s) already carry today's marker")
        for did in list(by_deal):
            if did in done:
                del by_deal[did]

    if args.max_deals:
        for did in sorted(by_deal)[args.max_deals:]:
            del by_deal[did]

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
