import os
import re
import time
import requests
from datetime import datetime, timezone
from typing import Dict, List, Optional

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

    def get_all_notes(self, max_pages: int = 20, page_size: int = 100) -> List[dict]:
        """
        Fetch all notes for the tenant via POST /notes/search, newest first.

        This is the only working notes endpoint on this tenant (GET /notes,
        /notes/relation, and /search/note all fail). Each note carries a
        `relations` array tying it to an entity — notably notes are attached to
        a COMPANY or CONTACT (or DEAL), so a deal's notes are the notes on the
        deal itself, its company, and its associated contacts. The request body
        filter is ignored (it returns all notes), so callers index by relation
        and match ids themselves.

        Returns (in createdAt-desc order):
            [{"text": str, "relations": [(ENTITY_TYPE, "id"), ...],
              "owner_id": int|None}, ...]
        """
        out = []
        for page in range(max_pages):
            time.sleep(self._delay)
            try:
                r = self.session.post(
                    f"{KYLAS_BASE}/notes/search",
                    params={"page": page, "size": page_size, "sort": "createdAt,desc"},
                    json={}, timeout=60,
                )
                r.raise_for_status()
                resp = r.json()
            except Exception:
                break
            content = resp.get("content", []) if isinstance(resp, dict) else []
            if not content:
                break
            for it in content:
                if not isinstance(it, dict):
                    continue
                rels = []
                for rel in (it.get("relations") or []):
                    et  = str(rel.get("entityType") or "").upper()
                    eid = rel.get("entityId")
                    if et and eid is not None:
                        rels.append((et, str(eid)))
                out.append({
                    "text":      _strip_html(it.get("description") or ""),
                    "relations": rels,
                    "owner_id":  it.get("ownerId"),
                })
            if page >= resp.get("totalPages", 1) - 1:
                break
        return out

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

    def get_users_by_email(self) -> Dict[str, int]:
        """Return {email: user_id} for ALL Kylas team members (all pages)."""
        result: Dict[str, int] = {}

        def _extract(members):
            for m in members:
                uid = m.get("id")
                if not uid:
                    continue
                email = m.get("email") or m.get("emailId") or ""
                if not email:
                    emails_field = m.get("emails") or []
                    if isinstance(emails_field, list) and emails_field:
                        first = emails_field[0]
                        email = (first.get("value") or first.get("email")
                                 or first if isinstance(first, str) else "")
                if email:
                    result[str(email).strip().lower()] = int(uid)

        for path in ["tenant/team-members", "users"]:
            try:
                page = 0
                while True:
                    time.sleep(self._delay)
                    r = self.session.get(
                        f"{KYLAS_BASE}/{path}",
                        params={"page": page, "size": 100},
                        timeout=30,
                    )
                    r.raise_for_status()
                    resp = r.json()
                    members = resp.get("content") or resp.get("data") or []
                    if not isinstance(members, list):
                        break
                    _extract(members)
                    total_pages = resp.get("totalPages", 1)
                    if page >= total_pages - 1 or not members:
                        break
                    page += 1
                if result:
                    return result
            except Exception:
                continue
        return result

    def find_user_id_by_email(self, email: str) -> Optional[int]:
        """
        Direct lookup of a single Kylas user by email.
        Tries POST /search/user first, then falls back to full user list.
        """
        email = email.strip().lower()
        # Try filtered search
        try:
            time.sleep(self._delay)
            r = self.session.post(
                f"{KYLAS_BASE}/search/user",
                params={"page": 0, "size": 10},
                json={"fields": ["id", "email"], "jsonRule": {
                    "condition": "AND",
                    "rules": [{"field": "email", "operator": "equal", "value": email}],
                }},
                timeout=30,
            )
            if r.ok:
                content = r.json().get("content", [])
                for m in content:
                    uid = m.get("id")
                    if uid:
                        return int(uid)
        except Exception:
            pass
        # Fall back to full list
        all_map = self.get_users_by_email()
        return all_map.get(email)

    def _put(self, path: str, body: dict) -> dict:
        time.sleep(self._delay)
        r = self.session.put(f"{KYLAS_BASE}/{path}", json=body, timeout=30)
        r.raise_for_status()
        return r.json() if r.content else {}

    def _patch(self, path: str, body: dict) -> dict:
        time.sleep(self._delay)
        r = self.session.patch(f"{KYLAS_BASE}/{path}", json=body, timeout=30)
        r.raise_for_status()
        return r.json() if r.content else {}

    def update_company_owner(self, company_id: int, user_id: int) -> bool:
        """Reassign a company to a different Kylas user. Returns True on success."""
        try:
            # Kylas PUT /companies/{id} requires the full object body.
            # GET first, update ownedBy, PUT back.
            body = self._get(f"companies/{company_id}")
            body["ownedBy"] = {"id": user_id}
            self._put(f"companies/{company_id}", body)
            return True
        except Exception as exc:
            print(f"[Kylas] ERROR updating company {company_id} owner: {exc}")
            return False

    def update_contact_owner(self, contact_id: int, user_id: int) -> bool:
        """Reassign a contact to a different Kylas user. Returns True on success."""
        try:
            # Kylas PUT /contacts/{id} requires the full object body.
            # GET first, update ownedBy, PUT back.
            body = self._get(f"contacts/{contact_id}")
            body["ownedBy"] = {"id": user_id}
            self._put(f"contacts/{contact_id}", body)
            return True
        except Exception as exc:
            print(f"[Kylas] ERROR updating contact {contact_id} owner: {exc}")
            return False

    def get_contacts_by_company(self, company_id: int) -> List[dict]:
        """Fetch all contacts linked to a specific company ID."""
        fields = ["id", "name", "ownedBy", "company"]
        filter_rule = {
            "condition": "AND",
            "rules": [{
                "id": "company.id", "field": "company.id",
                "type": "integer", "operator": "equal",
                "value": int(company_id),
            }],
        }
        records, page = [], 0
        while True:
            time.sleep(self._delay)
            r = self.session.post(
                f"{KYLAS_BASE}/search/contact",
                params={"page": page, "size": PAGE_SIZE, "sort": "updatedAt,desc"},
                json={"fields": fields, "jsonRule": filter_rule},
                timeout=60,
            )
            r.raise_for_status()
            resp    = r.json()
            content = resp.get("content", [])
            records.extend(content)
            if page >= resp.get("totalPages", 1) - 1 or not content:
                break
            page += 1
        return records
