"""
Sync all Kylas team members into config/team.json.

Updates:
  kylas_users       {str(id): name}    — used by run_sync.py for owner resolution
  kylas_user_emails {name: email}      — used by emails / calendar invites

Preserves all other keys (bd_team, cc, bd_targets, deal_rot, etc.).
Run via the sync_team GitHub Actions workflow (daily) or manually.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

import requests

TEAM_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "team.json")
KYLAS_BASE = "https://api.kylas.io/v1"


def _fetch_all_members(api_key: str) -> list:
    """
    Paginate through Kylas team-members (falls back to /users).
    Returns a flat list of raw member dicts — each has at least id + name.
    """
    headers = {"api-key": api_key, "Content-Type": "application/json"}
    for path in ["tenant/team-members", "users"]:
        members, page = [], 0
        try:
            while True:
                time.sleep(0.12)
                r = requests.get(
                    f"{KYLAS_BASE}/{path}",
                    params={"page": page, "size": 100},
                    headers=headers,
                    timeout=30,
                )
                r.raise_for_status()
                resp = r.json()
                content = resp.get("content") or resp.get("data") or []
                if not isinstance(content, list):
                    break
                members.extend(content)
                if page >= resp.get("totalPages", 1) - 1 or not content:
                    break
                page += 1
        except Exception as e:
            print(f"[sync_team] WARNING: {path} failed — {e}")
            members = []
        if members:
            print(f"[sync_team] Fetched {len(members)} members via /{path}")
            return members
    return []


def _profile_name_from_detail(detail: dict) -> str:
    """Pull a human-readable profile name out of a GET /users/{id} payload.

    Kylas may expose the profile under several shapes, so check the known
    keys first then fall back to any key containing 'profile'.
    """
    for key in ("profile", "userProfile", "userprofile", "role"):
        v = detail.get(key)
        if isinstance(v, dict):
            n = v.get("name") or v.get("displayName")
            if n:
                return str(n)
        elif isinstance(v, str) and v:
            return v
    for key in ("profileName", "profile_name", "roleName"):
        if detail.get(key):
            return str(detail[key])
    for k, v in detail.items():
        if "profile" in k.lower():
            if isinstance(v, dict):
                n = v.get("name") or v.get("displayName")
                if n:
                    return str(n)
            elif isinstance(v, str) and v:
                return v
    return ""


def _fetch_profiles(api_key: str, user_ids: list) -> dict:
    """Return {str(id): profile_name} via GET /users/{id}.

    The list endpoints (/users, /tenant/team-members) omit the profile, so we
    fetch each user's detail record (the same endpoint that carries the email).
    Logs the first user's profile-related fields once so the real shape is
    visible in the workflow logs.
    """
    headers = {"api-key": api_key, "Content-Type": "application/json"}
    profiles: dict = {}
    logged = False
    for uid in user_ids:
        time.sleep(0.12)
        try:
            r = requests.get(f"{KYLAS_BASE}/users/{uid}", headers=headers, timeout=30)
            r.raise_for_status()
            resp   = r.json()
            detail = resp.get("data", resp) if isinstance(resp, dict) else {}
        except Exception as e:
            print(f"[sync_team] WARNING: /users/{uid} failed — {e}")
            continue
        profiles[str(uid)] = _profile_name_from_detail(detail)
        if not logged and isinstance(detail, dict):
            prof_fields = {k: detail.get(k) for k in detail if "profile" in k.lower() or "role" in k.lower()}
            print(f"[sync_team] sample user {uid} keys: {sorted(detail.keys())}")
            print(f"[sync_team] sample user {uid} profile/role fields: {prof_fields}")
            logged = True
    return profiles


def _is_demand_generation(m: dict) -> bool:
    """True if a Kylas user/member record marks them as Demand Generation.

    Checked via TEAM membership (teams[].name) — the organisational grouping
    Kylas actually uses for "who's a BD caller" — not the permission profile
    (profileId / profile.name). A user's permission profile is independent of
    their team: e.g. Anjali Athya's profileId resolves to "Access Type
    Manager" (an elevated permissions tier) while her teams entry is
    "Demand Generation", so a profile-name-only check silently missed her and
    she never got added to bd_team (no section in any daily BD report). The
    profile-name check is kept as a secondary signal for older payload shapes
    that may carry the label there instead of/in addition to teams.
    """
    teams = m.get("teams") or []
    if isinstance(teams, list):
        for t in teams:
            tname = t.get("name") if isinstance(t, dict) else str(t)
            if tname and "demand generation" in tname.lower():
                return True
    profile_obj = m.get("profile") or {}
    profile_name = (
        profile_obj.get("name") if isinstance(profile_obj, dict) else str(profile_obj)
    ) or m.get("profileName") or ""
    return "demand generation" in profile_name.lower()


def _extract(members: list) -> tuple:
    """Return (users_by_id, emails_by_name, dg_names) from raw member list.

    dg_names is the set of full names on Kylas's "Demand Generation" team.
    """
    users_by_id: dict  = {}   # str(id) → name
    emails_by_name: dict = {} # name → email
    dg_names: set = set()     # names on the Demand Generation team

    for m in members:
        uid = m.get("id")
        if not uid:
            continue
        name = (m.get("name") or
                f"{m.get('firstName', '')} {m.get('lastName', '')}".strip())
        if not name:
            continue

        email = m.get("email") or m.get("emailId") or ""
        if not email:
            emails_f = m.get("emails") or []
            if isinstance(emails_f, list) and emails_f:
                first = emails_f[0]
                email = (first.get("value") or first.get("email")
                         or (first if isinstance(first, str) else ""))

        if _is_demand_generation(m):
            dg_names.add(name)

        users_by_id[str(uid)] = name
        if email:
            emails_by_name[name] = str(email).strip().lower()

    return users_by_id, emails_by_name, dg_names


def main():
    api_key = os.environ.get("KYLAS_API_KEY", "")
    if not api_key:
        print("[sync_team] ERROR: KYLAS_API_KEY not set")
        sys.exit(2)

    probe = os.environ.get("SYNC_TEAM_PROBE", "").strip().lower()
    if probe:
        members = _fetch_all_members(api_key)
        hits = [m for m in members
                if probe in (m.get("name") or f"{m.get('firstName','')} {m.get('lastName','')}").lower()]
        print(f"[probe] {len(hits)} member(s) matching {probe!r}")
        for m in hits:
            uid = m.get("id")
            print(f"[probe] raw list record for uid {uid}: {json.dumps(m, default=str)}")
            profs = _fetch_profiles(api_key, [uid])
            print(f"[probe] resolved profile for uid {uid}: {profs.get(str(uid))!r}")
            try:
                r = requests.get(f"{KYLAS_BASE}/users/{uid}",
                                  headers={"api-key": api_key, "Content-Type": "application/json"},
                                  timeout=30)
                r.raise_for_status()
                detail = r.json()
                detail = detail.get("data", detail) if isinstance(detail, dict) else detail
                print(f"[probe] full /users/{uid} detail: {json.dumps(detail, default=str)}")
            except Exception as e:
                print(f"[probe] /users/{uid} detail fetch failed: {e}")
        return

    members = _fetch_all_members(api_key)
    if not members:
        print("[sync_team] ERROR: could not fetch any team members from Kylas")
        sys.exit(2)

    new_users, new_emails, dg_names = _extract(members)
    # _extract already detects Demand Generation via team membership, which
    # the /users list endpoint returns directly (teams[].name) — no per-user
    # detail fetch needed. (The permission profile, e.g. profileId resolving
    # to "Access Type Manager", is a different, unreliable signal: it missed
    # Anjali Athya even though her team is Demand Generation — see
    # _is_demand_generation's docstring.)

    print(f"[sync_team] {len(new_users)} users, {len(new_emails)} with emails, "
          f"{len(dg_names)} Demand Generation")
    if dg_names:
        print(f"[sync_team] Demand Generation: {sorted(dg_names)}")

    with open(TEAM_PATH) as f:
        team = json.load(f)

    old_users  = team.get("kylas_users", {})
    old_emails = team.get("kylas_user_emails", {})

    # Compute diff for kylas_users / kylas_user_emails
    added_u   = [uid  for uid  in new_users  if uid  not in old_users]
    added_e   = [name for name in new_emails if name not in old_emails]
    changed_e = [name for name in new_emails
                 if name in old_emails and old_emails[name] != new_emails[name]]

    for uid in added_u:
        print(f"  + user  {uid}: {new_users[uid]}")
    for name in added_e:
        print(f"  + email {name!r}: {new_emails[name]}")
    for name in changed_e:
        print(f"  ~ email {name!r}: {old_emails[name]} → {new_emails[name]}")

    # Merge: existing wins on conflict (manual overrides are preserved)
    # New Kylas entries are added; changed emails (e.g. user updated email in Kylas)
    # ARE applied so the file stays current.
    team["kylas_users"]       = {**old_users,  **{k: v for k, v in new_users.items()  if k not in old_users}}
    team["kylas_user_emails"] = {**old_emails, **{k: v for k, v in new_emails.items() if k not in old_emails},
                                 **{k: v for k, v in new_emails.items() if k in changed_e}}

    # Add new Demand Generation users to bd_team so they receive BD daily emails.
    # Only users whose Kylas profile == "Demand Generation" are eligible.
    # Uses full name when a first-name collision exists (e.g. two Riyas).
    existing_bd_emails = {m.get("email", "").lower() for m in team.get("bd_team", [])}
    existing_first_names = {m.get("name", "").split()[0].lower()
                            for m in team.get("bd_team", [])}
    added_bd = []
    for name in dg_names:
        email = team["kylas_user_emails"].get(name, "")
        if not email or email.lower() in existing_bd_emails:
            continue
        first_name = name.split()[0]
        bd_name = name if first_name.lower() in existing_first_names else first_name
        team.setdefault("bd_team", []).append({"name": bd_name, "email": email})
        existing_bd_emails.add(email.lower())
        existing_first_names.add(first_name.lower())
        added_bd.append(name)
        print(f"  + bd_team {bd_name!r} ({name}): {email}")
    if added_bd:
        print(f"[sync_team] Added {len(added_bd)} Demand Generation user(s) to bd_team")

    if not added_u and not added_e and not changed_e and not added_bd:
        print("[sync_team] No changes — team.json is already up to date")
        return

    with open(TEAM_PATH, "w") as f:
        json.dump(team, f, indent=2, ensure_ascii=False)
        f.write("\n")

    total_u = len(team["kylas_users"])
    total_e = len(team["kylas_user_emails"])
    total_b = len(team.get("bd_team", []))
    print(f"[sync_team] Updated team.json → {total_u} users, {total_e} emails, {total_b} bd_team members")


if __name__ == "__main__":
    main()
