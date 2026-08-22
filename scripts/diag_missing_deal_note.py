"""
Why did deal 4676048 get no call note?

Reported: contact Sandeep Saini (#5927888) carries two call logs (19 Aug
12:17 pm, 532s and 20 Aug 5:59 pm, 66s), and deal 4676048 lists that contact
as an associated contact -- yet the EOD run wrote no note on the deal.

Two candidates, and this settles which:

  A. DATE WINDOW. The backfill ran for 2026-08-21 only. Both calls are 19/20
     Aug, so they were correctly outside the window and nothing is broken --
     the deal simply has no calls on the day that was run.

  B. TRUNCATION. /call-logs returns one un-paged response; the last sweep
     returned ten rows with ids 43794436-43843303, and the reported call log
     43734306 sits BELOW that range. If it never comes back from the sweep,
     the tenant-wide-sweep design cannot see older calls at all and the EOD
     run would miss them on every date, not just this one.

Read-only: it writes nothing. Prints an IST date histogram of everything the
sweep returns, checks the reported call log against it, re-sweeps seeded by
the CONTACT instead of a deal, and confirms the deal->contact link.

SAFETY: repo is public, logs are public. Ids, counts, dates and enums print;
names, phone numbers and free text are masked.
"""
import os
import sys
from collections import Counter
from datetime import timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

from utils.kylas_client import KylasClient
from scripts.eod_call_notes import _parse_utc, _is_open, _ist_day_bounds

IST = timezone(timedelta(hours=5, minutes=30))

DEAL = int(os.environ.get("MISSING_DEAL") or 4676048)
CONTACT = int(os.environ.get("MISSING_CONTACT") or 5927888)
CALL_LOG = int(os.environ.get("MISSING_CALL_LOG") or 43734306)


def ist_day(call):
    dt = _parse_utc(call.get("startTime"))
    return dt.astimezone(IST).date().isoformat() if dt else "?"


def describe(rows, label):
    ids = sorted(r.get("id") for r in rows if r.get("id"))
    print(f"\n  {label}: {len(rows)} row(s)")
    if not ids:
        return set()
    print(f"    id range : {ids[0]} .. {ids[-1]}")
    hist = Counter(ist_day(r) for r in rows)
    for day in sorted(hist):
        print(f"    {day}: {hist[day]} call(s)")
    return set(ids)


def main():
    load_dotenv()
    client = KylasClient()

    print("=" * 72)
    print(f"DIAG: deal {DEAL} / contact {CONTACT} / call log {CALL_LOG}")
    print("=" * 72)

    # --- 1. the sweep exactly as the EOD run does it -----------------------
    print("\n[1] tenant sweep, seeded by recent deals (what eod_call_notes does)")
    seeds = client.recent_deal_ids(2)
    rows, ignored = client.get_all_call_logs(seeds)
    print(f"    seeds={seeds} filter_ignored={ignored}")
    deal_seeded = describe(rows, "deal-seeded sweep")
    print(f"\n    call log {CALL_LOG} present in this sweep? "
          f"{'YES' if CALL_LOG in deal_seeded else 'NO'}")

    # --- 2. the same sweep seeded by the contact --------------------------
    # If the endpoint really ignores its filter, this must return the same set.
    # If it returns MORE -- or returns the missing record -- then the sweep is
    # not tenant-wide after all and seeding matters.
    print(f"\n[2] same sweep, seeded by contact {CONTACT} instead")
    c_rows, c_ignored = client.get_all_call_logs([CONTACT, CONTACT + 1], "contact")
    print(f"    filter_ignored={c_ignored}")
    contact_seeded = describe(c_rows, "contact-seeded sweep")
    print(f"\n    call log {CALL_LOG} present? "
          f"{'YES' if CALL_LOG in contact_seeded else 'NO'}")
    only_contact = contact_seeded - deal_seeded
    only_deal = deal_seeded - contact_seeded
    print(f"    ids only in the contact sweep: {sorted(only_contact)[:20]}")
    print(f"    ids only in the deal sweep   : {sorted(only_deal)[:20]}")

    # --- 3. single-seed sweep on the contact, unfiltered ------------------
    print(f"\n[3] one raw read: /call-logs?entityId={CONTACT}&entityType=contact")
    raw = client._sweep_call_logs(CONTACT, "contact")
    raw_ids = describe(raw, "raw single read")
    mine = [r for r in raw
            if CONTACT in KylasClient.call_log_relations(r, "contact")]
    print(f"\n    of those, {len(mine)} name contact {CONTACT} in relatedTo/associatedTo")
    for r in sorted(mine, key=lambda x: str(x.get("startTime") or "")):
        print(f"      id={r.get('id')} start={r.get('startTime')} "
              f"day_ist={ist_day(r)} outcome={r.get('outcome')} "
              f"dur={r.get('duration')} deals={sorted(KylasClient.call_log_relations(r,'deal'))}")

    # --- 4. the reported record, read directly ---------------------------
    print(f"\n[4] direct read of call log {CALL_LOG}")
    try:
        one = client._get(f"call-logs/{CALL_LOG}")
        body = one.get("data", one) if isinstance(one, dict) else one
        if isinstance(body, dict):
            print(f"    start={body.get('startTime')} outcome={body.get('outcome')} "
                  f"dur={body.get('duration')}")
            print(f"    relatedTo deals   ={sorted(KylasClient.call_log_relations(body,'deal'))}")
            print(f"    relatedTo contacts={sorted(KylasClient.call_log_relations(body,'contact'))}")
        else:
            print(f"    unexpected shape: {type(body).__name__}")
    except Exception as exc:
        print(f"    failed: {str(exc)[:200]}")

    # --- 5. the deal -> contact link the bridge depends on ---------------
    print(f"\n[5] deal {DEAL}: is contact {CONTACT} an associated contact?")
    deals = client.get_deals()
    target = next((d for d in deals if int(d.get("id") or 0) == DEAL), None)
    if not target:
        print(f"    deal {DEAL} was NOT in get_deals() ({len(deals)} deals returned)")
    else:
        cids = [c.get("id") if isinstance(c, dict) else c
                for c in (target.get("associatedContacts") or [])]
        stage = (target.get("pipelineStage") or {}).get("name")
        print(f"    found. stage={stage!r} open={_is_open(target)}")
        print(f"    associatedContacts ids={cids}")
        print(f"    contact {CONTACT} linked? {'YES' if CONTACT in [int(c) for c in cids if c] else 'NO'}")

    # --- 6. what each candidate day would have produced ------------------
    print("\n[6] per-day replay over everything the sweep can see")
    allrows = {r.get("id"): r for r in list(rows) + list(c_rows) + list(raw)}
    for day in ("2026-08-19", "2026-08-20", "2026-08-21"):
        n = sum(1 for r in allrows.values() if ist_day(r) == day)
        hit = sum(1 for r in allrows.values()
                  if ist_day(r) == day
                  and CONTACT in KylasClient.call_log_relations(r, "contact"))
        print(f"    {day}: {n} call(s) visible, {hit} of them on contact {CONTACT}")

    print("\n" + "=" * 72)
    print("READ THE ABOVE AS:")
    print("  [1] NO + [3] YES  -> truncation: the sweep cannot see older calls.")
    print("  [1] NO + [3] NO   -> the record is not reachable by any read here.")
    print("  [1] YES           -> not truncation; check [6] for the date window.")
    print("=" * 72)


if __name__ == "__main__":
    main()
