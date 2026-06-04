import os
from typing import Dict, Tuple
from pyairtable import Api


class AirtableClient:
    def __init__(self, table_name: str, base_id: str = None):
        api = Api(os.environ["AIRTABLE_PAT"])
        base = base_id or os.environ["AIRTABLE_BASE_ID"]
        self.table = api.table(base, table_name)
        self._cache: Dict[str, dict] = {}

    def build_cache(self, key_field: str) -> int:
        records = self.table.all()
        self._cache = {
            str(r["fields"][key_field]): r
            for r in records
            if key_field in r["fields"]
        }
        return len(self._cache)

    def upsert(
        self, key_field: str, kylas_id: str, fields: dict, updated_at: str
    ) -> Tuple[str, str]:
        """Returns (action, record_id). action = 'created' | 'updated' | 'skipped'."""
        existing = self._cache.get(str(kylas_id))

        if existing is None:
            record = self.table.create(fields)
            self._cache[str(kylas_id)] = record
            return "created", record["id"]

        if existing["fields"].get("Updated At", "") == updated_at:
            return "skipped", existing["id"]

        record = self.table.update(existing["id"], fields)
        self._cache[str(kylas_id)] = record
        return "updated", record["id"]
