"""Unit tests for KylasClient.create_call_log / get_call_logs.

Run: python -m pytest tests/test_call_log.py -q
"""
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("KYLAS_API_KEY", "test:1")

from utils.kylas_client import KylasClient  # noqa: E402


class _Resp:
    def __init__(self, status=201, payload=None, text=""):
        self.status_code = status
        self.ok = status < 300
        self._payload = payload if payload is not None else {"id": 999}
        self.text = text
        self.content = b"x"

    def json(self):
        return self._payload


def _client(resp=None, capture=None):
    c = KylasClient()

    def fake_request(method, path, **kw):
        if capture is not None:
            capture.append((method, path, kw.get("json")))
        return resp or _Resp()

    c._request = fake_request
    return c


def _payload(**over):
    """Build a call log in dry-run mode and return the payload it would send."""
    c = KylasClient()
    kw = dict(deal_id=4383813, made_by="Hritik", duration_seconds=270,
              note="Nov offsite discussed", dry_run=True)
    kw.update(over)
    res = c.create_call_log(**kw)
    assert res["ok"]
    return res["payload"]


# --------------------------------------------------------------- payload shape

def test_payload_links_the_deal():
    p = _payload()
    assert p["relatedTo"]["id"] == 4383813
    assert p["relatedTo"]["entity"] == "deal"


def test_connected_call_carries_duration_as_string():
    assert _payload()["duration"] == "270"


@pytest.mark.parametrize("outcome", ["missed", "rejected", "busy", "no_answer"])
def test_duration_omitted_when_not_connected(outcome):
    # Kylas rejects a duration on any outcome other than connected.
    p = _payload(outcome=outcome, duration_seconds=270)
    assert "duration" not in p
    assert p["outcome"] == outcome


def test_rep_name_goes_into_the_note():
    # Kylas will not accept a caller field, so the rep must survive in the note.
    desc = _payload()["notes"][0]["description"]
    assert desc.startswith("Call by Hritik | connected | 4m30s")
    assert "Nov offsite discussed" in desc


def test_note_headline_without_duration_when_missed():
    desc = _payload(outcome="missed", duration_seconds=0)["notes"][0]["description"]
    assert desc.startswith("Call by Hritik | missed")
    assert "None" not in desc


def test_contacts_and_phone_populate_associated_to():
    p = _payload(contact_ids=[5362056], phone_number="8489598564")
    assert p["associatedTo"] == [
        {"id": 5362056, "entity": "contact", "phoneNumber": "8489598564"}]
    assert p["relatedTo"]["phoneNumber"] == "8489598564"
    assert p["phoneNumber"] == "8489598564"


def test_no_associated_to_key_when_no_contacts():
    assert "associatedTo" not in _payload(contact_ids=[])


def test_recording_url_becomes_call_recording():
    p = _payload(recording_url="https://example.com/a/call_42.mp3")
    assert p["callRecording"] == {"url": "https://example.com/a/call_42.mp3",
                                 "fileName": "call_42.mp3"}


def test_start_time_is_utc_with_millis_and_z():
    # Kylas renders in the tenant timezone, so we must send UTC, not IST.
    p = _payload(started_at=datetime(2026, 8, 21, 20, 0, 49, tzinfo=timezone.utc))
    assert p["startTime"] == "2026-08-21T20:00:49.000Z"


def test_naive_start_time_is_treated_as_utc():
    p = _payload(started_at=datetime(2026, 8, 21, 20, 0, 49))
    assert p["startTime"] == "2026-08-21T20:00:49.000Z"


# ------------------------------------------------------------------ validation

def test_bad_outcome_is_rejected_without_calling_the_api():
    calls = []
    res = _client(capture=calls).create_call_log(
        deal_id=1, made_by="Hritik", outcome="answered")
    assert not res["ok"] and "outcome must be one of" in res["error"]
    assert calls == []


def test_bad_deal_id_is_rejected_without_calling_the_api():
    calls = []
    res = _client(capture=calls).create_call_log(deal_id="not-an-id", made_by="X")
    assert not res["ok"] and "bad deal_id" in res["error"]
    assert calls == []


def test_missing_rep_falls_back_to_unknown():
    assert _payload(made_by="")["notes"][0]["description"].startswith("Call by unknown")


# ----------------------------------------------------------------- round trips

def test_create_posts_to_call_logs_and_returns_id():
    calls = []
    res = _client(capture=calls).create_call_log(
        deal_id=4383813, made_by="Hritik", duration_seconds=60)
    assert res["ok"] and res["id"] == 999 and res["status"] == 201
    assert calls[0][0] == "POST" and calls[0][1] == "call-logs/"


def test_http_error_is_returned_not_raised():
    res = _client(_Resp(status=400, payload={}, text="bad request")).create_call_log(
        deal_id=1, made_by="Hritik")
    assert not res["ok"] and res["status"] == 400 and "bad request" in res["error"]


def test_get_call_logs_asks_for_a_full_page_not_the_default_ten():
    # The documented /call-logs/{id}?relatedToType= 404s on this tenant, so the
    # entityId/entityType form is what gets called -- even though the server
    # ignores the filter and the client has to re-apply it (below).
    #
    # `size` matters as much as the filter: the endpoint defaults to ten rows
    # against 5,399 call logs, and omitting it is what made every call-log
    # summary silently incomplete.
    seen = {}
    row = {"id": 43843294, "relatedTo": [{"id": 4383813, "entity": "deal"}]}

    c = KylasClient()
    c._get = lambda path, params=None: seen.update(path=path, params=params) or {
        "content": [row]}
    assert c.get_call_logs(4383813, "deal") == [row]
    assert seen["path"] == "call-logs"
    assert seen["params"]["entityId"] == 4383813
    assert seen["params"]["entityType"] == "deal"
    assert seen["params"]["size"] == KylasClient.CALL_LOG_PAGE_SIZE
    assert "page" in seen["params"]


def test_a_server_that_ignored_page_would_not_loop_to_the_cap():
    # It already ignores entityId/entityType. If it ignored `page` too, a sweep
    # that kept going would collect the same rows 200 times over.
    calls = []
    row = {"id": 1, "relatedTo": [{"id": 4383813, "entity": "deal"}]}
    c = KylasClient()
    c._get = lambda path, params=None: calls.append(params) or {"content": [row]}
    assert c.get_call_logs(4383813, "deal") == [row]
    assert len(calls) == 2  # page 0, then page 1 repeats it and the sweep stops


def test_get_call_logs_filters_out_other_deals_calls():
    # Measured: the server returns the whole tenant's call logs for any
    # entityId. Without this filter a per-deal read is silently wrong.
    mine = {"id": 1, "relatedTo": [{"id": 4383813, "entity": "deal"}]}
    theirs = {"id": 2, "relatedTo": [{"id": 4676048, "entity": "deal"}]}
    c = KylasClient()
    c._get = lambda path, params=None: {"content": [mine, theirs]}
    assert c.get_call_logs(4383813, "deal") == [mine]


def test_get_call_logs_swallows_errors():
    c = KylasClient()

    def boom(path, params=None):
        raise RuntimeError("429")

    c._get = boom
    assert c.get_call_logs(1, "deal") == []


@pytest.mark.parametrize("secs,expected", [
    (5, "5s"), (59, "59s"), (60, "1m00s"), (270, "4m30s"), (3600, "60m00s")])
def test_duration_formatting(secs, expected):
    assert KylasClient._fmt_duration(secs) == expected
