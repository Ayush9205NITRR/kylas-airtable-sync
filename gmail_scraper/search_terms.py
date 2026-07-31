"""
Search terms, read from Airtable so they can be edited without touching code.

The `Search Terms` table is the control panel: add a row, type a name in
**Search Term**, tick **Active**, and the next run picks it up. Nothing else is
required — a bare name becomes `"That Name" in:anywhere`.

Columns (all optional except the first):
    Search Term   text      the name/phrase to look for, or a full Gmail query
    Active        checkbox  unticked rows are ignored (default: treated as on
                            when the column doesn't exist)
    Category      text      label to file matches under; defaults to the term
    Scope         text      overrides `in:anywhere` for this row

If the table is missing or empty the scraper falls back to
config/email_categories.json, so a fresh checkout still works.
"""
import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gmail_scraper import config

_TRANSIENT = (429, 500, 502, 503)

TERM_FIELDS = ("Search Term", "Search Parameter", "Name", "Keyword", "Term")
CATEGORY_FIELDS = ("Category", "Label")
SCOPE_FIELDS = ("Scope",)
ACTIVE_FIELDS = ("Active", "Enabled")


def _first(fields: dict, names) -> str:
    """First non-empty value among `names` — tolerates a renamed column."""
    for name in names:
        value = fields.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _fetch_rows() -> list:
    url = (f"https://api.airtable.com/v0/{config.AIRTABLE_BASE_ID}/"
           f"{requests.utils.quote(config.SEARCH_TERMS_TABLE)}")
    headers = {"Authorization": f"Bearer {config.AIRTABLE_TOKEN}"}
    rows, offset = [], None
    while True:
        params = {"pageSize": 100}
        if offset:
            params["offset"] = offset
        for attempt in range(4):
            r = requests.get(url, headers=headers, params=params, timeout=30)
            if r.status_code in _TRANSIENT and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            break
        if r.status_code == 404:
            raise LookupError(
                f"Table {config.SEARCH_TERMS_TABLE!r} not found in base "
                f"{config.AIRTABLE_BASE_ID}"
            )
        if r.status_code >= 400:
            raise RuntimeError(f"Airtable {r.status_code}: {r.text[:300]}")
        data = r.json()
        rows.extend(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            break
    return rows


def from_airtable() -> list:
    """[{'name', 'query'}, ...] built from the `Search Terms` table."""
    out = []
    for record in _fetch_rows():
        fields = record.get("fields", {})
        term = _first(fields, TERM_FIELDS)
        if not term:
            continue
        # A missing Active column means "no opt-in gate configured" -> include.
        active = next((fields[n] for n in ACTIVE_FIELDS if n in fields), True)
        if not active:
            continue
        scope = _first(fields, SCOPE_FIELDS) or None
        query = config.build_query(term, scope)
        if not query:
            continue
        out.append({"name": _first(fields, CATEGORY_FIELDS) or term,
                    "query": query})
    return out


def load(source: str = "auto") -> list:
    """Resolve this run's search terms.

    source: 'airtable' (fail loudly if unusable), 'config' (JSON only), or
    'auto' (Airtable, quietly falling back to the JSON file).
    """
    if source == "config":
        return config.load_categories()

    try:
        terms = from_airtable()
        if terms:
            print(f"[terms] {len(terms)} active term(s) from Airtable "
                  f"'{config.SEARCH_TERMS_TABLE}'")
            return terms
        message = f"'{config.SEARCH_TERMS_TABLE}' has no active rows"
    except (LookupError, RuntimeError, requests.RequestException) as exc:
        message = str(exc)

    if source == "airtable":
        raise RuntimeError(f"--terms-from airtable: {message}")
    print(f"[terms] {message} — falling back to {config.CATEGORIES_FILE}")
    return config.load_categories()


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    for t in load():
        print(f"  {t['name']:<24} -> {t['query']}")
