import os
import re
from typing import Dict, List, Set, Tuple

import requests
from pyairtable import Api


class AirtableClient:
    def __init__(self, table_name: str, base_id: str = None):
        api = Api(os.environ["AIRTABLE_PAT"])
        base = base_id or os.environ["AIRTABLE_BASE_ID"]
        self.table = api.table(base, table_name)
        self._cache: Dict[str, dict] = {}
        self._creates: List[Tuple[str, dict]] = []   # (kylas_id, fields)
        self._updates: List[Tuple[str, str, dict]] = []  # (kylas_id, record_id, fields)
        self._skip_fields: Set[str] = set()

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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _strip_skip(self, fields: dict) -> dict:
        if not self._skip_fields:
            return fields
        return {k: v for k, v in fields.items() if k not in self._skip_fields}

    def _handle_422(self, exc: requests.exceptions.HTTPError) -> bool:
        """Return True if we can retry after stripping the offending field."""
        field = _extract_skip_field(str(exc))
        if field:
            print(f"[AirtableClient] WARNING: skipping field {field!r} ({exc})")
            self._skip_fields.add(field)
            return True
        return False

    def _batch_create_safe(self, records_fields: List[dict]) -> List[dict]:
        """batch_create with auto-retry on computed or unknown-field 422."""
        while True:
            try:
                return self.table.batch_create(
                    [self._strip_skip(f) for f in records_fields]
                )
            except requests.exceptions.HTTPError as exc:
                if not self._handle_422(exc):
                    raise

    def _batch_update_safe(self, updates: List[dict]) -> None:
        """batch_update with auto-retry on computed or unknown-field 422."""
        while True:
            try:
                self.table.batch_update(
                    [{"id": u["id"], "fields": self._strip_skip(u["fields"])} for u in updates]
                )
                return
            except requests.exceptions.HTTPError as exc:
                if not self._handle_422(exc):
                    raise

    # ------------------------------------------------------------------
    # Public flush
    # ------------------------------------------------------------------

    def flush(self) -> None:
        """Execute all buffered creates and updates in batches of 10."""
        if self._creates:
            created_records = self._batch_create_safe(
                [fields for _, fields in self._creates]
            )
            for (kylas_id, _), record in zip(self._creates, created_records):
                self._cache[kylas_id] = record
            self._creates.clear()

        if self._updates:
            self._batch_update_safe(
                [{"id": rid, "fields": fields} for _, rid, fields in self._updates]
            )
            self._updates.clear()


def _extract_skip_field(error_msg: str) -> str:
    """Extract field name from Airtable 422 errors we can skip and retry."""
    # INVALID_VALUE_FOR_COLUMN: Field "X" cannot accept a value because the field is computed
    m = re.search(
        r'Field "([^"]+)" cannot accept a value because the field is computed',
        error_msg,
    )
    if m:
        return m.group(1)
    # UNKNOWN_FIELD_NAME: Unknown field name: "X"
    m = re.search(r'Unknown field name: "([^"]+)"', error_msg)
    if m:
        return m.group(1)
    return ""
