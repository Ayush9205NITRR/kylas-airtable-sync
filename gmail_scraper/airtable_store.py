"""
Airtable storage for the `Email Threads` table.

Uses Airtable's native upsert (PATCH with performUpsert), keyed on Thread ID —
so re-running the scraper refreshes existing threads (new replies move the Last
Email Date, new CCs get appended) instead of creating duplicates.
"""
import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gmail_scraper import config

_TRANSIENT = (429, 500, 502, 503)
BATCH_SIZE = 10


def _base_url() -> str:
    if not config.AIRTABLE_BASE_ID:
        raise RuntimeError("GMAIL_AIRTABLE_BASE_ID / AIRTABLE_BASE_ID not set")
    return (f"https://api.airtable.com/v0/{config.AIRTABLE_BASE_ID}/"
            f"{requests.utils.quote(config.TABLE_NAME)}")


def _headers() -> dict:
    if not config.AIRTABLE_TOKEN:
        raise RuntimeError("AIRTABLE_PAT / AIRTABLE_TOKEN not set")
    return {"Authorization": f"Bearer {config.AIRTABLE_TOKEN}",
            "Content-Type": "application/json"}


def _request(method: str, payload: dict) -> dict:
    for attempt in range(5):
        r = requests.request(method, _base_url(), headers=_headers(),
                             json=payload, timeout=45)
        if r.status_code in _TRANSIENT and attempt < 4:
            wait = 2 ** attempt
            print(f"[airtable] {r.status_code}, retry in {wait}s "
                  f"({attempt + 1}/5)")
            time.sleep(wait)
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"Airtable {r.status_code}: {r.text[:500]}")
        return r.json()
    raise RuntimeError("Airtable request failed after 5 attempts")


def upsert(records: list) -> dict:
    """Upsert rows on Thread ID.

    Returns {'created': n, 'updated': n, 'by_key': {thread_id: record}} — the
    record ids come back so attachments can be uploaded onto them afterwards.
    """
    created = updated = 0
    by_key = {}
    for i in range(0, len(records), BATCH_SIZE):
        chunk = records[i:i + BATCH_SIZE]
        payload = {
            "performUpsert": {"fieldsToMergeOn": [config.KEY_FIELD]},
            "records": [
                {"fields": {k: v for k, v in fields.items()
                            if v is not None and v != ""}}
                for fields in chunk
            ],
            "typecast": True,
        }
        resp = _request("PATCH", payload)
        created += len(resp.get("createdRecords", []))
        updated += len(resp.get("updatedRecords", []))
        for record in resp.get("records", []):
            key = record.get("fields", {}).get(config.KEY_FIELD)
            if key:
                by_key[key] = record
    return {"created": created, "updated": updated, "by_key": by_key}
