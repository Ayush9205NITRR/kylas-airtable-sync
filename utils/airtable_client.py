import os
import re
import time
from typing import Dict, List, Optional, Set, Tuple

import requests
from pyairtable import Api


class AirtableClient:
    def __init__(self, table_name: str, base_id: str = None):
        api = Api(os.environ["AIRTABLE_PAT"])
        base = base_id or os.environ["AIRTABLE_BASE_ID"]
        self.table = api.table(base, table_name)
        self._cache: Dict[str, dict] = {}
        self._creates: List[Tuple[str, dict]] = []
        self._updates: List[Tuple[str, str, dict]] = []
        self._skip_fields: Set[str] = set()

    def build_cache(self, key_field: str) -> int:
        for attempt in range(4):
            try:
                records = self.table.all()
                self._cache = {
                    str(r["fields"][key_field]): r
                    for r in records
                    if key_field in r["fields"]
                }
                return len(self._cache)
            except requests.exceptions.HTTPError as exc:
                err = str(exc)
                if any(code in err for code in ("406", "429", "503")):
                    wait = 2 ** attempt
                    print(f"[AirtableClient] transient error on build_cache (attempt {attempt+1}/4), retrying in {wait}s...")
                    time.sleep(wait)
                else:
                    raise
        raise RuntimeError(f"build_cache failed after 4 attempts for {key_field!r}")

    def upsert(
        self, key_field: str, kylas_id: str, fields: dict, updated_at: str,
        updated_at_field: str = "Updated At",
    ) -> Tuple[str, str]:
        """Buffer the operation. Call flush() after the loop.

        Pass updated_at_field="" to always update (e.g. when the table has no
        timestamp column to compare against).
        """
        existing = self._cache.get(str(kylas_id))

        if existing is None:
            self._creates.append((str(kylas_id), fields))
            return "created", ""

        if updated_at_field and existing["fields"].get(updated_at_field, "") == updated_at:
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

    def _probe_bad_field(self, record_id: str, fields: dict) -> Optional[str]:
        """Test each field individually to find the one causing a nameless 422."""
        for field, value in fields.items():
            if field in self._skip_fields:
                continue
            try:
                self.table.update(record_id, {field: value})
            except requests.exceptions.HTTPError as exc:
                if _is_value_error(str(exc)):
                    print(f"[AirtableClient] WARNING: probe identified bad field {field!r}")
                    return field
        return None

    def _try_skip_named(self, exc: requests.exceptions.HTTPError) -> bool:
        """If error contains a field name, skip it and return True."""
        field = _extract_skip_field(str(exc))
        if field:
            print(f"[AirtableClient] WARNING: skipping field {field!r}")
            self._skip_fields.add(field)
            return True
        return False

    def _batch_create_safe(self, records_fields: List[dict]) -> List[dict]:
        """batch_create with auto-retry on skippable 422 errors."""
        while True:
            try:
                return self.table.batch_create(
                    [self._strip_skip(f) for f in records_fields]
                )
            except requests.exceptions.HTTPError as exc:
                if "TOO_MANY_RECORDS_IN_BASE" in str(exc):
                    print(f"[AirtableClient] WARNING: base is at record limit — "
                          f"skipping {len(records_fields)} create(s). "
                          f"Upgrade the Airtable workspace to allow new records.")
                    return []
                if not self._try_skip_named(exc):
                    raise

    def _batch_update_safe(self, updates: List[dict]) -> None:
        """batch_update with auto-retry, including probe for nameless field errors."""
        while True:
            try:
                self.table.batch_update(
                    [{"id": u["id"], "fields": self._strip_skip(u["fields"])} for u in updates]
                )
                return
            except requests.exceptions.HTTPError as exc:
                err_str = str(exc)
                if self._try_skip_named(exc):
                    continue
                # Error has no field name — probe fields of the first record to find culprit
                if _is_value_error(err_str) and updates:
                    active = {k: v for k, v in updates[0]["fields"].items()
                              if k not in self._skip_fields}
                    bad = self._probe_bad_field(updates[0]["id"], active)
                    if bad:
                        self._skip_fields.add(bad)
                        continue
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
    """Extract field name from Airtable 422 errors that name the problematic field."""
    m = re.search(
        r'Field "([^"]+)" cannot accept a value because the field is computed',
        error_msg,
    )
    if m:
        return m.group(1)
    m = re.search(r'Unknown field name: "([^"]+)"', error_msg)
    if m:
        return m.group(1)
    return ""


def _is_value_error(error_msg: str) -> bool:
    """True for 422s that are about a specific field value being wrong."""
    return "INVALID_VALUE_FOR_COLUMN" in error_msg or "UNKNOWN_FIELD_NAME" in error_msg
