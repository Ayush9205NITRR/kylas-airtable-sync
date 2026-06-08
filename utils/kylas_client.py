import os
import re
import time
import requests
from datetime import datetime, timezone
from typing import Dict, List

_TAG = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    if not text:
        return ""
    return _TAG.sub(" ", str(text)).replace("&nbsp;", " ").strip()

KYLAS_BASE = "https://api.kylas.io/v1"
PAGE_SIZE  = 200

_COMPANY_FIELDS = [
    "id", "name", "industry", "ownedBy", "ownerId",
    "createdAt", "updatedAt", "customFieldValues",
]

_CONTACT_FIELDS = [
    "id", "name", "firstName", "lastName", "emails", "phoneNumbers",
    "company", "designation", "ownedBy", "ownerId", "updatedBy",
    "linkedin", "city", "state", "country", "source",
    "createdAt", "updatedAt", "customFieldValues",
]

_DEAL_FIELDS = [
    "id", "name", "estimatedValue", "actualValue", "pipeline", "pipelineStage",
    "associatedContacts", "company", "estimatedClosureOn",
    "ownedBy", "ownerId", "source", "forecastingType",
    "createdAt", "updatedAt", "customFieldValues",
]


class KylasClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "api-key": os.environ["KYLAS_API_KEY"],
            "Content-Type": "application/json",
        })
        self._delay = 0.1   # reduced from 0.25 — Kylas allows ~10 req/s

    def _get(self, path: str, params: dict = None) -> dict:
        time.sleep(self._delay)
        r = self.session.get(f"{KYLAS_BASE}/{path}", params=params or {}, timeout=30)
        r.raise_for_status()
        return r.json()

    def _search_all(self, entity: str, fields: list, since: str = None) -> List[dict]:
        """
        Fetch all records of `entity` from Kylas.

        since (ISO string, e.g. "2026-06-03T12:00:00Z"):
            Stop fetching once we see records with updatedAt < since.
            Records are returned sorted updatedAt DESC, so the first record
            older than the cutoff means all subsequent pages are also older.
            Use for daily incremental syncs (~72 hours window) — drastically
            reduces API calls when only a small fraction was updated recently.
            Pass None for a full sync (initial import / after a gap).
        """
        cutoff = None
        if since:
            cutoff = datetime.fromisoformat(since.replace("Z", "+00:00"))
            if cutoff.tzinfo is None:
                cutoff = cutoff.replace(tzinfo=timezone.utc)

        records, page = [], 0
        while True:
            time.sleep(self._delay)
            r = self.session.post(
                f"{KYLAS_BASE}/search/{entity}",
                params={"page": page, "size": PAGE_SIZE, "sort": "updatedAt,desc"},
                json={"fields": fields, "jsonRule": None},
                timeout=60,
            )
            r.raise_for_status()
            resp    = r.json()
            content = resp.get("content", [])

            if cutoff:
                kept, stop = [], False
                for item in content:
                    upd = item.get("updatedAt", "")
                    if upd:
                        try:
                            item_dt = datetime.fromisoformat(upd.replace("Z", "+00:00"))
                            if item_dt.tzinfo is None:
                                item_dt = item_dt.replace(tzinfo=timezone.utc)
                            if item_dt < cutoff:
                                stop = True
                                break   # all following records on this & later pages are older
                        except (ValueError, TypeError):
                            pass
                    kept.append(item)
                records.extend(kept)
                if stop:
                    print(f"  [{entity}] incremental stop at page {page} — {len(records)} records since cutoff")
                    break
            else:
                records.extend(content)

            if page >= resp.get("totalPages", 1) - 1 or not content:
                break
            page += 1

        return records

    def get_companies(self, since: str = None) -> List[dict]:
        return self._search_all("company", _COMPANY_FIELDS, since=since)

    def get_contacts(self, since: str = None) -> List[dict]:
        return self._search_all("contact", _CONTACT_FIELDS, since=since)

    def get_deals(self, since: str = None) -> List[dict]:
        return self._search_all("deal", _DEAL_FIELDS, since=since)

    def get_company(self, cid: int) -> dict:
        resp = self._get(f"companies/{cid}")
        return resp.get("data", resp)

    def get_contact(self, cid: int) -> dict:
        resp = self._get(f"contacts/{cid}")
        return resp.get("data", resp)

    def get_deal(self, did: int) -> dict:
        resp = self._get(f"deals/{did}")
        return resp.get("data", resp)

    def get_deal_notes(self, deal_id) -> List[dict]:
        """
        Best-effort fetch of notes/comments on a deal, newest first.

        Kylas exposes notes under a few different shapes depending on the
        tenant; we try each and return [] if none work (the rotting alert
        then falls back to the deal's own updatedAt clock).

        Returns: [{"text": str, "createdAt": str}, ...]
        """
        attempts = [
            ("get",  f"deals/{deal_id}/notes", None),
            ("get",  "notes", {"entityType": "deal", "entityId": deal_id}),
            ("post", "search/note",
             {"jsonRule": {"rules": [{"id": "entityId", "field": "entityId",
                                      "operator": "equal", "value": str(deal_id)}]}}),
        ]
        for method, path, payload in attempts:
            try:
                time.sleep(self._delay)
                if method == "get":
                    r = self.session.get(f"{KYLAS_BASE}/{path}",
                                         params=payload or {}, timeout=30)
                else:
                    r = self.session.post(
                        f"{KYLAS_BASE}/{path}",
                        params={"page": 0, "size": 20, "sort": "createdAt,desc"},
                        json=payload, timeout=30)
                r.raise_for_status()
                data  = r.json()
                items = (data.get("content") or data.get("data")
                         or (data if isinstance(data, list) else []))
                notes = []
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    text = (it.get("note") or it.get("description")
                            or it.get("body") or it.get("content") or "")
                    created = it.get("createdAt") or it.get("updatedAt") or ""
                    if created:
                        notes.append({"text": _strip_html(text), "createdAt": created})
                if notes:
                    return sorted(notes, key=lambda n: n["createdAt"], reverse=True)
            except Exception:
                continue
        return []

    def get_user_email(self, user_id) -> str:
        """
        Fetch a single user's email via GET /users/{id}.

        This is the authoritative per-user lookup (the /users/{id} response
        carries a top-level "email" field) and covers EVERY user, unlike the
        team-members list which only returns the first page. Returns "" on
        failure so callers can fall back to other sources.
        """
        if not user_id:
            return ""
        try:
            resp = self._get(f"users/{user_id}")
            data = resp.get("data", resp) if isinstance(resp, dict) else {}
            email = data.get("email") or data.get("updatedEmail") or ""
            return str(email).strip().lower() if email else ""
        except Exception:
            return ""

    def get_users(self) -> Dict[int, str]:
        """Return {user_id: name} for contact owner resolution."""
        for path in ["tenant/team-members", "users"]:
            try:
                resp    = self._get(path)
                members = resp.get("content") or resp.get("data") or []
                if isinstance(members, list) and members:
                    result = {}
                    for m in members:
                        if "id" not in m:
                            continue
                        name = (m.get("name") or
                                f"{m.get('firstName', '')} {m.get('lastName', '')}".strip() or
                                f"User {m['id']}")
                        result[int(m["id"])] = name
                    if result:
                        return result
            except Exception:
                continue
        return {}

    def get_user_emails(self) -> Dict[str, str]:
        """Return {full_name: email} for all Kylas team members."""
        for path in ["tenant/team-members", "users"]:
            try:
                resp    = self._get(path)
                members = resp.get("content") or resp.get("data") or []
                if not isinstance(members, list) or not members:
                    continue
                result = {}
                for m in members:
                    name = (m.get("name") or
                            f"{m.get('firstName', '')} {m.get('lastName', '')}".strip())
                    # Kylas may expose email as a string or nested list
                    email = m.get("email") or m.get("emailId") or ""
                    if not email:
                        emails_field = m.get("emails") or []
                        if isinstance(emails_field, list) and emails_field:
                            first = emails_field[0]
                            email = (first.get("value") or first.get("email")
                                     or first if isinstance(first, str) else "")
                    if name and email:
                        result[name] = str(email).strip().lower()
                if result:
                    return result
            except Exception:
                continue
        return {}
