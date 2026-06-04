import os
import time
import requests
from typing import List

KYLAS_BASE = "https://api.kylas.io/v1"
PAGE_SIZE = 200

_COMPANY_FIELDS = [
    "id", "name", "industry", "website", "phoneNumbers", "emails",
    "city", "state", "country", "description", "ownedBy", "createdAt", "updatedAt",
]
_CONTACT_FIELDS = [
    "id", "firstName", "lastName", "emails", "phoneNumbers",
    "company", "designation", "ownedBy", "createdAt", "updatedAt",
]
_DEAL_FIELDS = [
    "id", "name", "estimatedValue", "pipeline", "pipelineStage",
    "associatedContacts", "company", "estimatedClosureOn",
    "ownedBy", "createdAt", "updatedAt",
]


class KylasClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "api-key": os.environ["KYLAS_API_KEY"],
            "Content-Type": "application/json",
        })
        self._delay = 0.25  # ~4 req/s

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
            resp = r.json()
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
