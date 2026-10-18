#!/usr/bin/env python3
"""
What does Kylas actually store in the Next Call Date fields?

WHY THIS EXISTS
The calendar invites blocked 03:30 AM IST for almost every contact, even
though reps pick a specific time in Kylas. send_call_invites.py reads
cfNextCallDateCallLater and takes the HH:MM:SS straight out of the raw string,
treating it as IST ("Kylas stores timestamps with a Z but the actual value is
IST"). Two things could be wrong with that, and they need different fixes:

  1. Wrong FIELD. The Airtable sync reads cfNextCallDate; the invites read
     cfNextCallDateCallLater. If reps fill in the other one, the invite field
     only ever holds a default time.
  2. Wrong TIMEZONE. If Kylas really stores UTC, then 03:30Z is 09:00 AM IST
     and every time is 5h30m off.

This prints the field definitions and the raw stored value of every
Next-Call-ish field for the given contacts, so the answer comes from Kylas
rather than from a guess. Read-only: nothing is written anywhere.

    python scripts/probe_next_call_fields.py --ids 6326286,5379132
"""
import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.kylas_client import KylasClient   # noqa: E402

FIELDS = ("cfNextCallDate", "cfNextCallDateCallLater")


def _time_part(raw) -> str:
    raw = str(raw or "")
    return raw[11:19] if len(raw) > 18 and raw[10] == "T" else "(no time)"


def main() -> int:
    ap = argparse.ArgumentParser(description="Show raw Next Call Date values")
    ap.add_argument("--ids", required=True, metavar="ID,ID,...",
                    help="Kylas contact IDs to inspect")
    args = ap.parse_args()
    ids = [int(x) for x in args.ids.replace(" ", "").split(",") if x]

    kylas = KylasClient()

    print("=" * 96)
    print("FIELD DEFINITIONS on Contact whose key or label mentions 'call'")
    print("=" * 96)
    for fld in kylas.list_entity_fields("contact"):
        key = str(fld.get("name") or "")
        label = str(fld.get("displayName") or "")
        if "call" in key.lower() or "call" in label.lower():
            extra = {k: fld.get(k) for k in ("type", "dateFormat", "timeFormat",
                                              "isStandard", "active")
                     if fld.get(k) is not None}
            print(f"  {key:34} | {label:34} | {extra}")

    print("\n" + "=" * 96)
    print("RAW VALUES")
    print("=" * 96)
    print(f"  {'id':>9}  {'name':24} {'cfNextCallDate':32} cfNextCallDateCallLater")
    times = {f: Counter() for f in FIELDS}
    other_keys = Counter()
    for cid in ids:
        try:
            ct = kylas.get_contact(cid)
        except Exception as exc:
            print(f"  {cid:>9}  (fetch failed: {exc})")
            continue
        cf = ct.get("customFieldValues") or {}
        for k in cf:
            if "call" in k.lower():
                other_keys[k] += 1
        vals = [cf.get(f) for f in FIELDS]
        for f, v in zip(FIELDS, vals):
            if v:
                times[f][_time_part(v)] += 1
        name = (ct.get("name") or
                f"{ct.get('firstName', '')} {ct.get('lastName', '')}".strip())[:24]
        print(f"  {cid:>9}  {name:24} {str(vals[0] or '-'):32} {vals[1] or '-'}")

    print("\n" + "=" * 96)
    print("TIME-OF-DAY SPREAD (how many contacts share each stored time)")
    print("=" * 96)
    for f in FIELDS:
        print(f"  {f}:")
        for t, n in times[f].most_common():
            print(f"    {t:12} x{n}")
        if not times[f]:
            print("    (no values)")

    print("\n  every 'call' custom-field key seen on these contacts: "
          + ", ".join(f"{k} (x{n})" for k, n in other_keys.most_common()))
    return 0


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    sys.exit(main())
