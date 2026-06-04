import os
import time
import requests
from typing import Dict, List

KYLAS_BASE = "https://api.kylas.io/v1"
PAGE_SIZE  = 200

_COMPANY_FIELDS = [
    "id", "name", "industry", "ownedBy", "ownerId",
    "createdAt", "updatedAt", "customFieldValues",
]

_CONTACT_FIELDS = [
    "id", "name", "firstName", "lastName", "emails", "phoneNumbers",
    "company", "designation", "ownedBy", "ownerId",
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
        self._delay = 0.25

    def _get(self, path: str, params: dict = None) -> dict:
        time.sleep(self._delay)
        r = self.session.get(f"{KYLAS_BASE}/{path}", params=params or {}, timeout=30)
        r.raise_for_status()
        return r.json()

    def _search_all(self, entity: str, fields: list) -> List[dict]:
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
            records.extend(content)
            if page >= resp.get("totalPages", 1) - 1 or not content:
                break
            page += 1
        return records

    def get_companies(self) -> List[dict]:
        return self._search_all("company", _COMPANY_FIELDS)

    def get_contacts(self) -> List[dict]:
        return self._search_all("contact", _CONTACT_FIELDS)

    def get_deals(self) -> List[dict]:
        return self._search_all("deal", _DEAL_FIELDS)

    def get_company(self, cid: int) -> dict:
        resp = self._get(f"companies/{cid}")
        return resp.get("data", resp)

    def get_contact(self, cid: int) -> dict:
        resp = self._get(f"contacts/{cid}")
        return resp.get("data", resp)

    def get_deal(self, did: int) -> dict:
        resp = self._get(f"deals/{did}")
        return resp.get("data", resp)

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
