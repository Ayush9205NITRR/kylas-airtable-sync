"""Offline unit tests for KylasClient write paths (no network / no API key).

Guards the two failure modes seen in the Airtable -> Kylas field push:
  * 400 Bad Request from PUTting a fetched object back verbatim (read-only
    fields not stripped) -> _clean_for_put / update_*_fields.
  * 429 Too Many Requests aborting an update with no retry -> _request backoff.

Run: python tests/test_kylas_client.py
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("KYLAS_API_KEY", "test:1")  # client needs *a* key to init

from utils.kylas_client import KylasClient  # noqa: E402


class _Resp:
    def __init__(self, code):
        self.status_code = code
        self.headers = {}
        self.content = b"{}"

    def json(self):
        return {}

    def raise_for_status(self):
        import requests
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    @property
    def ok(self):
        return self.status_code < 400


def test_clean_for_put_strips_readonly():
    dirty = {
        "id": 1, "name": "Acme", "createdAt": "x", "updatedAt": "y",
        "createdBy": {}, "updatedBy": {}, "ownerId": 9, "ownedBy": {"id": 9},
        "recordActions": {"edit": True}, "tenantId": 5,
        "industry": {"id": 610, "name": "IT"}, "customFieldValues": {"cfX": 1},
    }
    clean = KylasClient._clean_for_put(dirty)
    assert set(clean) == {"name", "industry", "customFieldValues"}, clean
    print("PASS clean_for_put strips read-only fields")


def test_request_retries_on_429():
    client = KylasClient()
    client._delay = 0
    calls = {"n": 0}

    def fake_request(method, url, **kw):
        calls["n"] += 1
        return _Resp(429) if calls["n"] < 3 else _Resp(200)

    client.session.request = fake_request
    with mock.patch("utils.kylas_client.time.sleep"):  # skip real backoff waits
        r = client._request("PUT", "companies/1", json={})
    assert r.status_code == 200 and calls["n"] == 3, (r.status_code, calls["n"])
    print("PASS _request retries on 429 then succeeds")


def test_request_gives_up_after_max_retries():
    client = KylasClient()
    client._delay = 0
    client._max_retries = 2
    calls = {"n": 0}

    def fake_request(method, url, **kw):
        calls["n"] += 1
        return _Resp(429)

    client.session.request = fake_request
    with mock.patch("utils.kylas_client.time.sleep"):
        r = client._request("GET", "companies/1")
    assert r.status_code == 429 and calls["n"] == 3, (r.status_code, calls["n"])  # 1 + 2 retries
    print("PASS _request returns final 429 after exhausting retries")


def test_update_company_fields_cleans_body():
    client = KylasClient()
    full = {
        "id": 123, "name": "Acme", "recordActions": {}, "ownedBy": {"id": 9},
        "updatedAt": "x", "industry": {"id": 1, "name": "IT"},
        "customFieldValues": {},
    }
    put_body = {}
    client._get = lambda path: {"data": dict(full)}
    client._put = lambda path, body: put_body.update(body) or {}
    res = client.update_company_fields(123, {"cfSourceOfData": "LinkedIn"})
    assert res == "updated", res
    for ro in ("id", "recordActions", "ownedBy", "updatedAt"):
        assert ro not in put_body, (ro, put_body)
    assert put_body["customFieldValues"]["cfSourceOfData"] == "LinkedIn", put_body
    print("PASS update_company_fields PUTs a cleaned body")


def test_update_contact_fields_coerces_company_id():
    client = KylasClient()
    contact = {
        "id": 555, "firstName": "A", "company": {"id": 894, "name": "Acme"},
        "ownedBy": {"id": 9}, "recordActions": {}, "customFieldValues": {},
    }
    put_body = {}
    client._put = lambda path, body: put_body.update(body) or {}
    res = client.update_contact_fields(555, {"cfSourceOfData": "Referral"},
                                       contact_data=contact)
    assert res == "updated", res
    assert put_body["company"] == 894, put_body.get("company")  # dict -> int id
    assert "id" not in put_body and "ownedBy" not in put_body, put_body
    print("PASS update_contact_fields coerces company id and cleans body")


def test_owner_key_in_field_map_is_skipped():
    client = KylasClient()
    client._get = lambda path: {"data": {"id": 1, "name": "Acme"}}
    issued = {"put": False}
    client._put = lambda path, body: issued.update(put=True) or {}
    res = client.update_company_fields(1, {"ownedBy": "Some Name"})
    assert res == "unchanged" and not issued["put"], (res, issued)
    print("PASS ownedBy-only field map is skipped (no bad PUT)")


if __name__ == "__main__":
    test_clean_for_put_strips_readonly()
    test_request_retries_on_429()
    test_request_gives_up_after_max_retries()
    test_update_company_fields_cleans_body()
    test_update_contact_fields_coerces_company_id()
    test_owner_key_in_field_map_is_skipped()
    print("\nALL TESTS PASSED")
