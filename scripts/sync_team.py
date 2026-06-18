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


def _extract(members: list) -> tuple:
    """Return (users_by_id, emails_by_name) from raw member list."""
    users_by_id: dict  = {}   # str(id) → name
    emails_by_name: dict = {} # name → email

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

        users_by_id[str(uid)] = name
        if email:
            emails_by_name[name] = str(email).strip().lower()

    return users_by_id, emails_by_name


def main():
    api_key = os.environ.get("KYLAS_API_KEY", "")
    if not api_key:
        print("[sync_team] ERROR: KYLAS_API_KEY not set")
        sys.exit(2)

    members = _fetch_all_members(api_key)
    if not members:
        print("[sync_team] ERROR: could not fetch any team members from Kylas")
        sys.exit(2)

    new_users, new_emails = _extract(members)
    print(f"[sync_team] {len(new_users)} users, {len(new_emails)} with emails")

    with open(TEAM_PATH) as f:
        team = json.load(f)

    old_users  = team.get("kylas_users", {})
    old_emails = team.get("kylas_user_emails", {})

    # Compute diff
    added_u   = [uid  for uid  in new_users  if uid  not in old_users]
    added_e   = [name for name in new_emails if name not in old_emails]
    changed_e = [name for name in new_emails
                 if name in old_emails and old_emails[name] != new_emails[name]]

    if not added_u and not added_e and not changed_e:
        print("[sync_team] No changes — team.json is already up to date")
        return

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

    with open(TEAM_PATH, "w") as f:
        json.dump(team, f, indent=2, ensure_ascii=False)
        f.write("\n")

    total_u = len(team["kylas_users"])
    total_e = len(team["kylas_user_emails"])
    print(f"[sync_team] Updated team.json → {total_u} users, {total_e} emails")


if __name__ == "__main__":
    main()
