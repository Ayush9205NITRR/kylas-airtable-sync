import os
import time
import requests
from typing import List

KYLAS_BASE = "https://api.kylas.io/v1"
PAGE_SIZE = 200


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

    def _fetch_all(self, entity: str) -> List[dict]:
        records, page = [], 0
        while True:
            resp = self._get(entity, params={"page": page, "size": PAGE_SIZE})
            data = resp.get("data", resp)
            if isinstance(data, list):
                records.extend(data)
                break
            content = data.get("content", [])
            records.extend(content)
            if page >= data.get("totalPages", 1) - 1 or not content:
                break
            page += 1
        return records

    def get_companies(self) -> List[dict]:
        return self._fetch_all("companies")

    def get_contacts(self) -> List[dict]:
        return self._fetch_all("contacts")

    def get_deals(self) -> List[dict]:
        return self._fetch_all("deals")

    def get_company(self, cid: int) -> dict:
        resp = self._get(f"companies/{cid}")
        return resp.get("data", resp)

    def get_contact(self, cid: int) -> dict:
        resp = self._get(f"contacts/{cid}")
        return resp.get("data", resp)

    def get_deal(self, did: int) -> dict:
        resp = self._get(f"deals/{did}")
        return resp.get("data", resp)
