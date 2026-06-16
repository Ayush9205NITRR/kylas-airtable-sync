"""
Airtable storage for the `Calls` table (REST API).

Uses the cold-call base (COLD_CALL_AIRTABLE_BASE_ID, falling back to
AIRTABLE_BASE_ID) and the repo's AIRTABLE_PAT token. typecast=True lets Airtable
auto-coerce single-select values (status / discovery_outcome / call_language).
"""
import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cold_call import config

_TRANSIENT = (429, 500, 502, 503)


def _base_url() -> str:
    if not config.AIRTABLE_BASE_ID:
        raise RuntimeError("COLD_CALL_AIRTABLE_BASE_ID / AIRTABLE_BASE_ID not set")
    return f"https://api.airtable.com/v0/{config.AIRTABLE_BASE_ID}/{config.TABLE_NAME}"


def _headers() -> dict:
    if not config.AIRTABLE_TOKEN:
        raise RuntimeError("AIRTABLE_PAT / AIRTABLE_TOKEN not set")
    return {"Authorization": f"Bearer {config.AIRTABLE_TOKEN}",
            "Content-Type": "application/json"}


def _escape(value: str) -> str:
    # filterByFormula values are wrapped in double quotes; drop any inside.
    return str(value).replace('"', "")


def check_duplicate(bd_name: str, filename: str) -> bool:
    """True if this bd_name + audio_filename was already processed.

    Rows with status='error' are NOT counted, so a file that failed (e.g. a
    transient transcription error) gets retried on the next run.
    """
    formula = (f'AND({{bd_name}}="{_escape(bd_name)}",'
               f'{{audio_filename}}="{_escape(filename)}",'
               f'{{status}}!="error")')
    params = {"filterByFormula": formula, "maxRecords": 1}
    for attempt in range(4):
        r = requests.get(_base_url(), headers=_headers(), params=params, timeout=30)
        if r.status_code in _TRANSIENT and attempt < 3:
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
        return len(r.json().get("records", [])) > 0
    return False


def insert_record(fields: dict) -> dict:
    """Create one record. Empty values are dropped so blanks stay blank."""
    clean = {k: v for k, v in fields.items() if v is not None and v != ""}
    payload = {"fields": clean, "typecast": True}
    for attempt in range(4):
        r = requests.post(_base_url(), headers=_headers(), json=payload, timeout=30)
        if r.status_code in _TRANSIENT and attempt < 3:
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
        return r.json()
