#!/usr/bin/env python3
"""
Who owns the rotting deals — and which Kylas team are they on?

WHY THIS EXISTS
"Deals assigned to non-revenue folks" cannot be answered from config today:
team.json has a `revenue_team` key, but it is empty and nothing in the
codebase reads it. So there is no stored definition of who counts as revenue,
and inventing one here would just bury a guess inside a report.

What Kylas does know is TEAM membership (teams[].name) — the same signal
sync_team.py uses to decide who is Demand Generation. This lists every
currently-rotting deal with its owner and that owner's team(s), then groups
owners by team. That turns "which of these are non-revenue?" from a guess
into something answerable by looking at the output.

The rotting set is produced by 08_deal_rot's own _read_deals/_find_rotten, so
these are exactly the deals the alert fires on — not a reimplementation that
could drift from it.

Read-only: fetches from Kylas, writes nothing anywhere.

    python scripts/probe_deal_owner_teams.py
    python scripts/probe_deal_owner_teams.py --idle-days 2
"""
import argparse
import csv
import importlib.util
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.kylas_client import KylasClient, KYLAS_BASE   # noqa: E402

TEAM_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "config", "team.json")


def _load_deal_rot():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "modules", "08_deal_rot.py")
    spec = importlib.util.spec_from_file_location("deal_rot", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def member_teams(kylas) -> dict:
    """{lowercased full name: {"email": str, "teams": [str], "profile": str}}

    Paginated, like KylasClient.get_users_by_email: a bare GET returns only
    the first page, which silently makes every user past it look like they do
    not exist. That is exactly the wrong failure for this report -- an owner
    who is merely on page 2 would be indistinguishable from one who is not a
    Kylas user at all.

    A user on no team is kept with an empty list rather than dropped: "nobody
    assigned them a team" is a finding, not a reason to hide the row.
    """
    out = {}
    for path in ("tenant/team-members", "users"):
        try:
            page = 0
            while True:
                kylas._pace()
                r = kylas.session.get(f"{KYLAS_BASE}/{path}",
                                      params={"page": page, "size": 100},
                                      timeout=30)
                r.raise_for_status()
                resp = r.json()
                members = resp.get("content") or resp.get("data") or []
                if not isinstance(members, list):
                    break
                for m in members:
                    name = (m.get("name") or
                            f"{m.get('firstName', '')} {m.get('lastName', '')}".strip())
                    if not name:
                        continue
                    teams = []
                    for t in (m.get("teams") or []):
                        tn = t.get("name") if isinstance(t, dict) else str(t)
                        if tn:
                            teams.append(tn)
                    prof = m.get("profile") or {}
                    prof_name = (prof.get("name") if isinstance(prof, dict)
                                 else str(prof)) or m.get("profileName") or ""
                    email = m.get("email") or m.get("emailId") or ""
                    if not email:
                        ef = m.get("emails") or []
                        if isinstance(ef, list) and ef:
                            first = ef[0]
                            email = (first.get("value") or first.get("email")
                                     or (first if isinstance(first, str) else ""))
                    out[name.strip().lower()] = {"name": name, "email": email,
                                                 "teams": teams,
                                                 "profile": prof_name}
                total_pages = resp.get("totalPages", 1)
                if page >= total_pages - 1 or not members:
                    break
                page += 1
            if out:
                print(f"[probe] user list from /{path}")
                return out
        except Exception as exc:
            print(f"[probe] /{path} failed — {exc}")
            continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="List deals with each owner's Kylas team")
    ap.add_argument("--idle-days", type=int, default=None,
                    help="Override deal_rot.idle_days from team.json")
    ap.add_argument("--all-deals", action="store_true",
                    help="Every deal regardless of stage or idle time, "
                         "open AND closed — not just the rotting ones the "
                         "alert fires on")
    ap.add_argument("--non-revenue-only", action="store_true",
                    help="Keep only deals whose owner is NOT on the revenue team")
    ap.add_argument("--revenue-team", default="Revenue", metavar="NAME",
                    help="Kylas team name that counts as revenue (default: Revenue)")
    ap.add_argument("--csv", default="", metavar="PATH",
                    help="Also write the rows to this CSV")
    args = ap.parse_args()

    dr = _load_deal_rot()
    with open(TEAM_PATH) as fh:
        cfg = json.load(fh).get("deal_rot", {})
    idle = args.idle_days or int(cfg.get("idle_days", 2))

    kylas = KylasClient()
    print("[probe] reading deals...")
    deals = dr._read_deals(kylas)
    if args.all_deals:
        # No stage or idle filter at all: a deal parked with the wrong team is
        # a misassignment whether or not it is closed, and the rot alert's
        # stage allow-list would hide most of them.
        selected = [{**d, "idle": ""} for d in deals]
        print(f"[probe] {len(deals)} deal(s) read, ALL included "
              f"(open and closed, every stage)\n")
    else:
        selected = dr._find_rotten(deals, idle, cfg.get("terminal_stages", []),
                                   cfg.get("alert_stages", []),
                                   cfg.get("excluded_stages", []))
        print(f"[probe] {len(deals)} deal(s) read, {len(selected)} rotting\n")

    print("[probe] reading Kylas team membership...")
    members = member_teams(kylas)
    print(f"[probe] {len(members)} Kylas user(s)\n")

    rev = args.revenue_team.strip().lower()
    rows, unknown_owner = [], []
    for d in selected:
        owner = (d.get("owner") or "").strip()
        info = members.get(owner.lower())
        if info is None:
            if owner:
                unknown_owner.append(owner)
            teams, prof = ["(not found in Kylas users)"], ""
        else:
            teams = info["teams"] or ["(no team assigned)"]
            prof = info["profile"]
        is_revenue = any(t.strip().lower() == rev for t in teams)
        if args.non_revenue_only and is_revenue:
            continue
        rows.append({"deal": d.get("name", ""), "owner": owner or "Unassigned",
                     "teams": teams, "profile": prof, "revenue": is_revenue,
                     "stage": d.get("stage", ""), "idle": d.get("idle", "")})

    by_team = defaultdict(lambda: {"deals": 0, "owners": set()})
    for r in rows:
        for t in r["teams"]:
            by_team[t]["deals"] += 1
            by_team[t]["owners"].add(r["owner"])

    label = "ALL DEALS" if args.all_deals else "ROTTING DEALS"
    if args.non_revenue_only:
        label += " — NON-REVENUE OWNERS ONLY"

    print("=" * 96)
    print(f"{label} BY OWNER'S KYLAS TEAM")
    print("=" * 96)
    print(f"  {'team':34} {'deals':>6}  owners")
    print(f"  {'-' * 34} {'-' * 6}  {'-' * 44}")
    for team, v in sorted(by_team.items(), key=lambda kv: -kv[1]["deals"]):
        who = ", ".join(sorted(v["owners"]))
        print(f"  {team:34} {v['deals']:6}  {who[:60]}")

    print("\n" + "=" * 96)
    print(label)
    print("=" * 96)
    print(f"  {'account / deal':44} {'stage':26} {'owner':20} team")
    print(f"  {'-' * 44} {'-' * 26} {'-' * 20} {'-' * 20}")
    for r in sorted(rows, key=lambda r: (r["teams"][0], r["owner"], r["deal"])):
        print(f"  {r['deal'][:44]:44} {r['stage'][:26]:26} "
              f"{r['owner'][:20]:20} {'/'.join(r['teams'])[:20]}")

    if args.csv:
        os.makedirs(os.path.dirname(os.path.abspath(args.csv)) or ".",
                    exist_ok=True)
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["Account / Deal Name", "Stage", "Assigned To",
                        "Owner Kylas Team", "Counts As Revenue", "Idle (days)"])
            for r in sorted(rows, key=lambda r: (r["teams"][0], r["owner"],
                                                 r["deal"])):
                w.writerow([r["deal"], r["stage"], r["owner"],
                            "/".join(r["teams"]),
                            "yes" if r["revenue"] else "no", r["idle"]])
        print(f"\n[probe] wrote {len(rows)} row(s) → {args.csv}")

    if unknown_owner:
        print(f"\n[probe] {len(set(unknown_owner))} owner(s) not found among Kylas "
              f"users: {', '.join(sorted(set(unknown_owner)))}")

    print(f"\n[probe] revenue team = {args.revenue_team!r} (from Kylas team "
          f"membership; team.json revenue_team is "
          f"{'still EMPTY and unused' if not json.load(open(TEAM_PATH)).get('revenue_team') else 'populated'})")
    return 0


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    sys.exit(main())
