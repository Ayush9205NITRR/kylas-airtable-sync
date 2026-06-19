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
    client._get = lambda path, params=None: {"data": dict(full)}
    client._put = lambda path, body: put_body.update(body) or {}
    res = client.update_company_fields(123, {"cfSourceOfData": "LinkedIn"})
    assert res == "updated", res
    # ownedBy is excluded from the field PUT (owner is set separately, and a
    # full PUT carrying ownedBy is rejected); audit fields are stripped too.
    for ro in ("id", "recordActions", "updatedAt", "ownedBy"):
        assert ro not in put_body, (ro, put_body)
    assert put_body["customFieldValues"]["cfSourceOfData"] == "LinkedIn", put_body
    print("PASS update_company_fields PUTs a cleaned, owner-free body")


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
    assert "id" not in put_body and "ownedBy" not in put_body, put_body  # owner-free PUT
    print("PASS update_contact_fields coerces company id and excludes owner")


def test_owner_key_in_field_map_is_skipped():
    client = KylasClient()
    client._get = lambda path: {"data": {"id": 1, "name": "Acme"}}
    issued = {"put": False}
    client._put = lambda path, body: issued.update(put=True) or {}
    res = client.update_company_fields(1, {"ownedBy": "Some Name"})
    assert res == "unchanged" and not issued["put"], (res, issued)
    print("PASS ownedBy-only field map is skipped (no bad PUT)")


def test_raise_for_status_surfaces_body():
    import requests

    class _ErrResp:
        status_code = 500
        reason = "Internal Server Error"
        url = "https://api.kylas.io/v1/companies/1"

        def json(self):
            return {"message": "cfBooleanPostLink: expected boolean but got String"}

    try:
        KylasClient._raise_for_status(_ErrResp())
    except requests.HTTPError as exc:
        assert "cfBooleanPostLink" in str(exc) and "500" in str(exc), str(exc)
        print("PASS _raise_for_status surfaces Kylas error body:", str(exc)[-70:])
    else:
        raise AssertionError("expected HTTPError")


def test_object_field_from_scalar_is_skipped():
    client = KylasClient()
    # numberOfEmployees is an object in Kylas; mapping a scalar onto it must be
    # skipped (not corrupt the body), while the custom field still applies.
    full = {
        "id": 9, "name": "Acme",
        "numberOfEmployees": {"id": 83, "name": "50-99"},
        "customFieldValues": {},
    }
    put_body = {}
    client._get = lambda path, params=None: {"data": dict(full)}
    client._put = lambda path, body: put_body.update(body) or {}
    res = client.update_company_fields(
        9, {"numberOfEmployees": 50, "cfSourceOfData": "LinkedIn"})
    assert res == "updated", res
    assert put_body["numberOfEmployees"] == {"id": 83, "name": "50-99"}, put_body
    assert put_body["customFieldValues"]["cfSourceOfData"] == "LinkedIn", put_body
    print("PASS object field is not clobbered by a scalar mapping")


def test_failing_field_is_isolated_and_good_ones_kept():
    import requests
    client = KylasClient()
    client._get = lambda path, params=None: {"data": {"id": 7, "name": "Acme",
                                                      "customFieldValues": {}}}

    puts = []

    def fake_put(path, body):
        puts.append(dict(body.get("customFieldValues") or {}))
        # Kylas 500s whenever the bad field is present (mimics a dropdown/boolean
        # field rejecting a raw string), succeeds otherwise.
        if "cfOffsiteTimeline" in (body.get("customFieldValues") or {}):
            raise requests.HTTPError("500 Server Error — Generic error, please check with admin.")
        return {}

    client._put = fake_put
    res = client.update_company_fields(7, {
        "cfSourceOfData": "LinkedIn",
        "cfOffsiteTimeline": "This Quarter",   # the culprit
        "cfBooleanPostLink": True,
    })
    # Partial success: good fields applied, culprit isolated out.
    assert res == "updated", res
    last_good = puts[-1]
    assert "cfOffsiteTimeline" not in last_good, last_good
    assert last_good.get("cfSourceOfData") == "LinkedIn", last_good
    assert last_good.get("cfBooleanPostLink") is True, last_good
    print("PASS bad field isolated, good fields kept")


def test_all_fields_fail_returns_failed():
    import requests
    client = KylasClient()
    client._get = lambda path, params=None: {"data": {"id": 8, "name": "Acme",
                                                      "customFieldValues": {}}}

    def fake_put(path, body):
        if body.get("customFieldValues"):      # any field present -> reject
            raise requests.HTTPError("500 Server Error — Generic error")
        return {}                              # unchanged base round-trips fine

    client._put = fake_put
    res = client.update_company_fields(8, {"cfA": "x", "cfB": "y"})
    assert res == "failed", res
    print("PASS all-fields-bad returns failed (base still round-trips)")


def test_custom_field_defs_parsed_from_endpoint():
    client = KylasClient()
    client._get = lambda path, params=None: {"content": [
        {"fieldName": "cfOffsiteTimeline", "type": "PICK_LIST",
         "pickLists": [{"id": 257199, "value": "Jan - Mar"},
                       {"id": 257202, "value": "Oct - Dec"}]},
        {"fieldName": "cfBooleanPostLink", "type": "TOGGLE", "pickLists": []},
        {"fieldName": "cfNotes", "type": "TEXT_FIELD"},
    ]}
    defs = client.get_custom_field_defs("company")
    assert defs["cfOffsiteTimeline"]["options"]["jan - mar"] == 257199, defs
    assert defs["cfOffsiteTimeline"]["options"]["oct - dec"] == 257202, defs
    assert defs["cfBooleanPostLink"]["type"] == "TOGGLE", defs
    # cached: a second call must not re-hit the endpoint
    client._get = lambda *a, **k: (_ for _ in ()).throw(AssertionError("should be cached"))
    assert client.get_custom_field_defs("company")["cfNotes"]["type"] == "TEXT_FIELD"
    print("PASS custom field defs parsed (dropdown options + types) and cached")


def test_field_put_excludes_owner():
    # The field PUT must never carry ownedBy — owner is assigned separately.
    client = KylasClient()
    client.get_custom_field_defs = lambda e: {}
    client._get = lambda path, params=None: {"data": {
        "id": 5, "name": "Acme", "ownedBy": {"id": 99, "name": "Mayra"},
        "customFieldValues": {}}}
    put = {}
    client._put = lambda path, body: put.update(body) or {}
    res = client.update_company_fields(5, {"cfSourceOfData": "LinkedIn"})
    assert res == "updated", res
    assert "ownedBy" not in put and "ownerId" not in put, put
    assert put["customFieldValues"]["cfSourceOfData"] == "LinkedIn", put
    print("PASS field PUT excludes owner (handled by dedicated endpoint)")


def test_dropdown_encoding_cached_after_first_isolation():
    import requests
    client = KylasClient()
    client.get_custom_field_defs = lambda e: {
        "cfX": {"type": "PICK_LIST", "options": {"a": 111}, "labels": {111: "A"}}}
    client._get = lambda path, params=None: {"data": {
        "id": 1, "name": "Co", "customFieldValues": {}}}
    puts = []

    def fake_put(path, body):
        v = (body.get("customFieldValues") or {}).get("cfX")
        puts.append(v)
        if v is not None and not isinstance(v, dict):   # bare id rejected, {id} accepted
            raise requests.HTTPError("500 Server Error — Generic error")
        return {}

    client._put = fake_put
    # 1st record: isolation discovers the {id} encoding works and caches it.
    assert client.update_company_fields(1, {"cfX": "A"}) == "updated"
    assert client._pick_style.get("cfX") == "idobj", client._pick_style
    # 2nd record: the very first PUT must already use {id} (fast path, no isolation).
    puts.clear()
    assert client.update_company_fields(2, {"cfX": "A"}) == "updated"
    assert puts[0] == {"id": 111}, puts          # used cached encoding immediately
    assert len(puts) == 1, puts                  # one PUT, no isolation round-trips
    print("PASS dropdown encoding cached -> later records skip isolation (fast)")


def test_company_dropdown_via_config_with_shape_fallback():
    import requests
    client = KylasClient()
    # API returns no field defs for company; the config picklist map must
    # supply cfOffsiteTimeline's options (Jan - Mar -> 257199).
    def fake_get(path, params=None):
        if path.startswith("entities/"):
            return {"content": []}
        return {"data": {"id": 1, "name": "Acme", "customFieldValues": {}}}
    client._get = fake_get

    puts = []
    def fake_put(path, body):
        puts.append(dict(body.get("customFieldValues") or {}))
        v = (body.get("customFieldValues") or {}).get("cfOffsiteTimeline")
        if v is not None and not isinstance(v, dict):   # bare id rejected; {id} accepted
            raise requests.HTTPError("500 Server Error — Generic error, please check with admin.")
        return {}
    client._put = fake_put

    res = client.update_company_fields(1, {"cfOffsiteTimeline": "Jan - Mar"})
    assert res == "updated", res
    assert puts[-1]["cfOffsiteTimeline"] == {"id": 257199}, puts[-1]
    print("PASS company dropdown resolved via config + write-shape self-corrected to {id}")


def test_dropdown_label_and_boolean_coerced_on_write():
    client = KylasClient()
    client.get_custom_field_defs = lambda entity: {
        "cfOffsiteTimeline": {"type": "PICK_LIST",
                              "options": {"jan - mar": 257199, "oct - dec": 257202}},
        "cfBooleanPostLink": {"type": "TOGGLE", "options": {}},
        "cfSourceOfData":    {"type": "TEXT_FIELD", "options": {}},
    }
    client._get = lambda path, params=None: {"data": {"id": 1, "name": "Acme",
                                                      "customFieldValues": {}}}
    put_body = {}
    client._put = lambda path, body: put_body.update(body) or {}
    res = client.update_company_fields(1, {
        "cfOffsiteTimeline": "Jan - Mar",   # dropdown label -> option id
        "cfBooleanPostLink": "yes",         # boolean string -> True
        "cfSourceOfData":    "LinkedIn",    # text -> unchanged
    })
    assert res == "updated", res
    cfv = put_body["customFieldValues"]
    assert cfv["cfOffsiteTimeline"] == 257199, cfv
    assert cfv["cfBooleanPostLink"] is True, cfv
    assert cfv["cfSourceOfData"] == "LinkedIn", cfv
    print("PASS dropdown label -> id and boolean coerced; text untouched")


if __name__ == "__main__":
    test_clean_for_put_strips_readonly()
    test_request_retries_on_429()
    test_request_gives_up_after_max_retries()
    test_update_company_fields_cleans_body()
    test_update_contact_fields_coerces_company_id()
    test_owner_key_in_field_map_is_skipped()
    test_raise_for_status_surfaces_body()
    test_object_field_from_scalar_is_skipped()
    test_failing_field_is_isolated_and_good_ones_kept()
    test_all_fields_fail_returns_failed()
    test_custom_field_defs_parsed_from_endpoint()
    test_field_put_excludes_owner()
    test_dropdown_encoding_cached_after_first_isolation()
    test_company_dropdown_via_config_with_shape_fallback()
    test_dropdown_label_and_boolean_coerced_on_write()
    print("\nALL TESTS PASSED")
