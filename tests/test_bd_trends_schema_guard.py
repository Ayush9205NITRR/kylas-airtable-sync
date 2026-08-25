"""Regression tests for the BD Trends rollup's missing-table handling.

The nightly rollup used to die with a bare Airtable 403
(INVALID_PERMISSIONS_OR_MODEL_NOT_FOUND) whenever the "BD Trends" table was
absent from the base — a message that reads like a token problem but is really
"no such table". Two guards now cover it:

  1. build_bd_trends.push_to_airtable() calls ensure_table() first, so a missing
     table is created instead of 403-ing.
  2. AirtableClient.build_cache() translates that 403 into AirtableTableNotFound
     with an actionable message, for every other table/script.

Run: python -m pytest tests/test_bd_trends_schema_guard.py -q
"""
import os
import sys

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("AIRTABLE_PAT", "pat_test")
os.environ.setdefault("AIRTABLE_BASE_ID", "appTESTTESTTEST1")

import scripts.build_bd_trends as bt  # noqa: E402
import scripts.setup_bd_trends as setup  # noqa: E402
from utils.airtable_client import AirtableClient, AirtableTableNotFound  # noqa: E402


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} Client Error")


# ──────────────────────────────────────────────────────────────────────────
# ensure_table
# ──────────────────────────────────────────────────────────────────────────

def test_ensure_table_creates_missing_table(monkeypatch):
    """Table absent from the base -> POST /tables and report success."""
    posted = []
    monkeypatch.setattr(setup.requests, "get",
                        lambda *a, **k: _Resp(200, {"tables": [{"name": "BD Daily Stats",
                                                                "id": "tbl1", "fields": []}]}))

    def _post(url, json=None, **kwargs):
        posted.append((url, json))
        return _Resp(200, {"id": "tblNEW"})

    monkeypatch.setattr(setup.requests, "post", _post)

    assert setup.ensure_table() is True
    assert len(posted) == 1
    url, body = posted[0]
    assert url.endswith("/tables")
    assert body["name"] == "BD Trends"
    # Key stays the primary field, and every metric column ships with the table.
    assert body["fields"][0]["name"] == "Key"
    names = {f["name"] for f in body["fields"]}
    assert {"Grain", "Period", "Period Start", "Owner"} <= names
    assert set(setup.METRIC_FIELDS) <= names


def test_ensure_table_adds_missing_fields_only(monkeypatch):
    """Table present but short a column -> patch that one column, don't recreate."""
    existing = {"tables": [{"name": "BD Trends", "id": "tblBD",
                            "fields": [{"name": n} for n in
                                       ["Key", "Grain", "Period", "Period Start", "Owner",
                                        "Attempted", "Connected", "Discovery Calls",
                                        "MQL", "Activation"]]}]}
    posted = []
    monkeypatch.setattr(setup.requests, "get", lambda *a, **k: _Resp(200, existing))
    monkeypatch.setattr(setup.time, "sleep", lambda s: None)

    def _post(url, json=None, **kwargs):
        posted.append((url, json))
        return _Resp(200, {})

    monkeypatch.setattr(setup.requests, "post", _post)

    assert setup.ensure_table() is True
    assert [body["name"] for _, body in posted] == ["SQL"]   # the only gap
    assert "/fields" in posted[0][0]


def test_ensure_table_survives_unreadable_schema(monkeypatch):
    """A PAT without schema.bases:read can still read/write records — don't block."""
    def _boom(*a, **k):
        raise requests.exceptions.HTTPError("403 Client Error")

    monkeypatch.setattr(setup.requests, "get", _boom)
    assert setup.ensure_table() is True


def test_ensure_table_false_when_create_fails(monkeypatch):
    """Missing AND uncreatable is the one case the caller must not push through."""
    monkeypatch.setattr(setup.requests, "get", lambda *a, **k: _Resp(200, {"tables": []}))
    monkeypatch.setattr(setup.requests, "post",
                        lambda *a, **k: _Resp(403, text="INVALID_PERMISSIONS"))
    assert setup.ensure_table() is False


# ──────────────────────────────────────────────────────────────────────────
# push_to_airtable
# ──────────────────────────────────────────────────────────────────────────

def test_push_aborts_without_touching_data_api(monkeypatch):
    """ensure_table() False -> (None, None), and no AirtableClient is constructed."""
    monkeypatch.setattr(bt, "ensure_table", lambda **kwargs: False)

    def _no_client(*a, **k):
        raise AssertionError("data API must not be touched when the table is missing")

    monkeypatch.setattr(bt, "AirtableClient", _no_client)
    assert bt.push_to_airtable({g: {} for g in bt.GRAINS}) == (None, None)


def test_push_runs_ensure_table_before_build_cache(monkeypatch):
    """Ordering is the whole point: schema first, then the data API."""
    calls = []

    monkeypatch.setattr(bt, "ensure_table", lambda **kwargs: calls.append("ensure") or True)

    class _FakeTbl:
        def build_cache(self, key):
            calls.append("build_cache")
            return 0

        def upsert(self, *a, **k):
            return "created", ""

        def flush(self):
            calls.append("flush")

    monkeypatch.setattr(bt, "AirtableClient", lambda name: _FakeTbl())

    tally, tbl = bt.push_to_airtable({g: {} for g in bt.GRAINS})
    assert calls == ["ensure", "build_cache", "flush"]
    assert tbl is not None
    assert all(tally[g] == {} for g in bt.GRAINS)


# ──────────────────────────────────────────────────────────────────────────
# AirtableClient error translation
# ──────────────────────────────────────────────────────────────────────────

def test_build_cache_translates_403_into_named_error(monkeypatch):
    client = AirtableClient("BD Trends")

    class _Table:
        def all(self, **kwargs):
            raise requests.exceptions.HTTPError(
                "403 Client Error: Forbidden for url: https://api.airtable.com/v0/appX/BD%20Trends "
                "{'type': 'INVALID_PERMISSIONS_OR_MODEL_NOT_FOUND'}"
            )

    client.table = _Table()
    with pytest.raises(AirtableTableNotFound) as exc:
        client.build_cache("Key")
    assert "BD Trends" in str(exc.value)
    assert "does not exist" in str(exc.value)


def test_build_cache_still_retries_transient_errors(monkeypatch):
    """The new branch must not swallow the 429/503 retry path."""
    client = AirtableClient("BD Trends")
    attempts = []

    class _Table:
        def all(self, **kwargs):
            attempts.append(1)
            if len(attempts) < 3:
                raise requests.exceptions.HTTPError("429 Client Error: Too Many Requests")
            return [{"id": "rec1", "fields": {"Key": "Day|2026-06-01|A"}}]

    client.table = _Table()
    monkeypatch.setattr("utils.airtable_client.time.sleep", lambda s: None)
    assert client.build_cache("Key") == 1
    assert len(attempts) == 3
