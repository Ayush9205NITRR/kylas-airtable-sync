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
import importlib.util
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.kylas_client import KylasClient   # noqa: E402

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

    Read from the same endpoints sync_team.py uses. A user on no team at all
    is kept with an empty list rather than dropped — "nobody assigned them a
    team" is exactly the kind of thing this report should surface, not hide.
    """
    out = {}
    for path in ("tenant/team-members", "users"):
        try:
            resp = kylas._get(path)
        except Exception as exc:
            print(f"[probe] {path} failed — {exc}")
            continue
        members = resp.get("content") or resp.get("data") or []
        if not isinstance(members, list) or not members:
            continue
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
            prof_name = (prof.get("name") if isinstance(prof, dict) else str(prof)) \
                or m.get("profileName") or ""
            email = m.get("email") or m.get("emailId") or ""
            if not email:
                ef = m.get("emails") or []
                if isinstance(ef, list) and ef:
                    first = ef[0]
                    email = (first.get("value") or first.get("email")
                             or (first if isinstance(first, str) else ""))
            out[name.strip().lower()] = {"name": name, "email": email,
                                         "teams": teams, "profile": prof_name}
        if out:
            return out
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="List rotting deals with each owner's Kylas team")
    ap.add_argument("--idle-days", type=int, default=None,
                    help="Override deal_rot.idle_days from team.json")
    args = ap.parse_args()

    dr = _load_deal_rot()
    with open(TEAM_PATH) as fh:
        cfg = json.load(fh).get("deal_rot", {})
    idle = args.idle_days or int(cfg.get("idle_days", 2))

    kylas = KylasClient()
    print(f"[probe] reading deals (idle_days={idle})...")
    deals = dr._read_deals(kylas)
    rotten = dr._find_rotten(deals, idle, cfg.get("terminal_stages", []),
                             cfg.get("alert_stages", []),
                             cfg.get("excluded_stages", []))
    print(f"[probe] {len(deals)} deal(s) read, {len(rotten)} rotting\n")

    print("[probe] reading Kylas team membership...")
    members = member_teams(kylas)
    print(f"[probe] {len(members)} Kylas user(s)\n")

    rows, unknown_owner = [], []
    for d in rotten:
        owner = (d.get("owner") or "").strip()
        info = members.get(owner.lower())
        if info is None:
            unknown_owner.append(owner)
            teams, prof = ["(not found in Kylas users)"], ""
        else:
            teams = info["teams"] or ["(no team assigned)"]
            prof = info["profile"]
        rows.append({"deal": d.get("name", ""), "owner": owner or "Unassigned",
                     "teams": teams, "profile": prof,
                     "stage": d.get("stage", ""), "idle": d.get("idle", "")})

    by_team = defaultdict(lambda: {"deals": 0, "owners": set()})
    for r in rows:
        for t in r["teams"]:
            by_team[t]["deals"] += 1
            by_team[t]["owners"].add(r["owner"])

    print("=" * 96)
    print("ROTTING DEALS BY OWNER'S KYLAS TEAM")
    print("=" * 96)
    print(f"  {'team':34} {'deals':>6}  owners")
    print(f"  {'-' * 34} {'-' * 6}  {'-' * 44}")
    for team, v in sorted(by_team.items(), key=lambda kv: -kv[1]["deals"]):
        who = ", ".join(sorted(v["owners"]))
        print(f"  {team:34} {v['deals']:6}  {who[:60]}")

    print("\n" + "=" * 96)
    print("EVERY ROTTING DEAL")
    print("=" * 96)
    print(f"  {'deal':44} {'owner':20} {'idle':>4}  team")
    print(f"  {'-' * 44} {'-' * 20} {'-' * 4}  {'-' * 24}")
    for r in sorted(rows, key=lambda r: (r["teams"][0], -int(r["idle"] or 0))):
        print(f"  {r['deal'][:44]:44} {r['owner'][:20]:20} {r['idle']:>4}  "
              f"{'/'.join(r['teams'])[:30]}")

    if unknown_owner:
        print(f"\n[probe] {len(set(unknown_owner))} owner(s) not found among Kylas "
              f"users: {', '.join(sorted(set(unknown_owner)))}")

    print(f"\n[probe] team.json revenue_team is "
          f"{'EMPTY — nothing defines who counts as revenue' if not json.load(open(TEAM_PATH)).get('revenue_team') else 'populated'}")
    return 0


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    sys.exit(main())
