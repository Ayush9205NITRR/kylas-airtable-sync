import os
from typing import Dict, List, Tuple
from pyairtable import Api


class AirtableClient:
    def __init__(self, table_name: str, base_id: str = None):
        api = Api(os.environ["AIRTABLE_PAT"])
        base = base_id or os.environ["AIRTABLE_BASE_ID"]
        self.table = api.table(base, table_name)
        self._cache: Dict[str, dict] = {}
        self._creates: List[Tuple[str, dict]] = []   # (kylas_id, fields)
        self._updates: List[Tuple[str, str, dict]] = []  # (kylas_id, record_id, fields)

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
        """Buffer the operation. Call flush() after the loop."""
        existing = self._cache.get(str(kylas_id))

        if existing is None:
            self._creates.append((str(kylas_id), fields))
            return "created", ""

        if existing["fields"].get("Updated At", "") == updated_at:
            return "skipped", existing["id"]

        self._updates.append((str(kylas_id), existing["id"], fields))
        return "updated", existing["id"]

    def flush(self) -> None:
        """Execute all buffered creates and updates in batches of 10."""
        if self._creates:
            created_records = self.table.batch_create(
                [fields for _, fields in self._creates]
            )
            for (kylas_id, _), record in zip(self._creates, created_records):
                self._cache[kylas_id] = record
            self._creates.clear()

        if self._updates:
            self.table.batch_update(
                [{"id": rid, "fields": fields} for _, rid, fields in self._updates]
            )
            self._updates.clear()
