"""
Tests for pushing Airtable Deals' "Updated Meeting Notes" field into Kylas
Notes.

Mirrors deal_remarks_to_notes.py's test shape: the safety net is the
idempotency logic (never double-post) and the eligibility filter (never guess
at a deal id), since this writes production Kylas data. This script never
reads or writes "Previous Meeting Notes" -- that rolling shift happens on the
Zapier side -- so these tests don't touch it either, apart from
ensure_notes_field() also being responsible for creating that column.
"""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "deal_meeting_notes_to_kylas",
    os.path.join(_ROOT, "scripts", "deal_meeting_notes_to_kylas.py"))
dmn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dmn)


class _FakeTable:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeAt:
    def __init__(self, rows):
        self.table = _FakeTable(rows)


class _FakeKylas:
    """create_note() always succeeds and is recorded; get_all_notes() returns
    whatever the test seeds, and a call() lets a test simulate a real create
    landing (so plan() run again after apply reflects it)."""
    def __init__(self, notes=None):
        self._notes = list(notes or [])
        self.created = []

    def get_all_notes(self):
        return self._notes

    def create_note(self, entity_type, entity_id, text):
        self.created.append((entity_type, entity_id, text))
        self._notes.append({"text": text, "relations": [(entity_type, str(entity_id))],
                            "owner_id": None})
        return {"ok": True, "status": 200, "id": len(self.created), "error": ""}


def _row(deal_id="", deal_name="Acme Renewal", notes=""):
    fields = {}
    if deal_id:
        fields[dmn.DEAL_ID_FIELD] = deal_id
    if deal_name:
        fields[dmn.DEAL_NAME_FIELD] = deal_name
    if notes:
        fields[dmn.NOTES_FIELD] = notes
    return {"id": "rec1", "fields": fields}


def test_a_row_with_notes_and_a_deal_id_is_eligible():
    at = _FakeAt([_row(deal_id="4383813", notes="Discussed pricing.")])
    rows = dmn.eligible_rows(at)
    assert rows == [("4383813", "Acme Renewal", "Discussed pricing.")]


def test_a_row_with_no_meeting_notes_is_not_eligible():
    at = _FakeAt([_row(deal_id="4383813", notes="")])
    assert dmn.eligible_rows(at) == []


def test_a_row_with_no_kylas_deal_id_is_not_eligible():
    """Cannot cascade a note onto a deal id that doesn't exist -- must not guess."""
    at = _FakeAt([_row(deal_id="", notes="Discussed pricing.")])
    assert dmn.eligible_rows(at) == []


def test_a_row_with_no_deal_name_falls_back_to_a_labelled_placeholder():
    at = _FakeAt([_row(deal_id="4383813", deal_name="", notes="Notes here.")])
    rows = dmn.eligible_rows(at)
    assert rows[0][1] == "Deal 4383813"


def test_plan_skips_a_deal_whose_current_notes_are_already_logged():
    at = _FakeAt([_row(deal_id="1", notes="Same text.")])
    kylas = _FakeKylas(notes=[{"text": "Same text.", "relations": [("DEAL", "1")]}])
    not_noted, already = dmn.plan(at, kylas)
    assert not_noted == [] and already == 1


def test_plan_flags_a_deal_whose_notes_changed_since_last_sync():
    """The exact case a new meeting produces: the Airtable field was
    overwritten with a fresh transcript, so it must read as new work even
    though the deal already has an OLDER note on it."""
    at = _FakeAt([_row(deal_id="1", notes="Second meeting transcript.")])
    kylas = _FakeKylas(notes=[{"text": "First meeting transcript.", "relations": [("DEAL", "1")]}])
    not_noted, already = dmn.plan(at, kylas)
    assert len(not_noted) == 1 and not_noted[0][0] == "1"
    assert already == 0


def test_plan_matches_only_the_deal_the_note_is_related_to():
    at = _FakeAt([_row(deal_id="2", notes="Text.")])
    kylas = _FakeKylas(notes=[{"text": "Text.", "relations": [("DEAL", "1")]}])  # different deal
    not_noted, _already = dmn.plan(at, kylas)
    assert len(not_noted) == 1, "a note on a DIFFERENT deal must not suppress this one"


def test_idempotency_ignores_html_entity_differences():
    """get_all_notes() strips tags but not entities, so an existing note read
    back with escaped punctuation must still match the plain-text source."""
    at = _FakeAt([_row(deal_id="1", notes="Client said \"yes\" & signed.")])
    kylas = _FakeKylas(notes=[{"text": "Client said &quot;yes&quot; &amp; signed.",
                              "relations": [("DEAL", "1")]}])
    not_noted, already = dmn.plan(at, kylas)
    assert not_noted == [] and already == 1


def test_apply_via_create_note_records_the_right_deal_and_text():
    at = _FakeAt([_row(deal_id="4383813", notes="Discussed renewal terms.")])
    kylas = _FakeKylas()
    not_noted, _ = dmn.plan(at, kylas)
    deal_id, _name, notes, _norm = not_noted[0]
    result = kylas.create_note("DEAL", deal_id, notes)
    assert result["ok"] is True
    assert kylas.created == [("DEAL", "4383813", "Discussed renewal terms.")]


# ── ensure_notes_field: adding the column only when it's actually missing ──

class _Resp:
    def __init__(self, payload=None, status=200):
        self._payload, self.status_code, self.text = payload or {}, status, ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")


def test_ensure_notes_field_adds_both_columns_when_missing(monkeypatch):
    # ensure_notes_field() does `import requests` locally, binding to the same
    # sys.modules entry patched here -- no need to touch the dmn module itself.
    posts = []
    tables_payload = {"tables": [{"id": "tblX", "name": "Deals",
                                  "fields": [{"name": "Deal Name"}]}]}
    import requests as real_requests
    monkeypatch.setattr(real_requests, "get", lambda *a, **k: _Resp(tables_payload))
    monkeypatch.setattr(real_requests, "post",
                        lambda url, **k: (posts.append((url, k.get("json"))), _Resp(status=200))[1])
    assert dmn.ensure_notes_field("app_test", {}) is True
    assert len(posts) == 2
    bodies_by_name = {body["name"]: body for _url, body in posts}
    assert set(bodies_by_name) == {"Updated Meeting Notes", "Previous Meeting Notes"}
    for url, body in posts:
        assert url.endswith("/tables/tblX/fields")
        assert body["type"] == "multilineText"


def test_ensure_notes_field_adds_only_the_missing_one_of_the_two(monkeypatch):
    posts = []
    tables_payload = {"tables": [{"id": "tblX", "name": "Deals",
                                  "fields": [{"name": "Deal Name"},
                                             {"name": "Updated Meeting Notes"}]}]}
    import requests as real_requests
    monkeypatch.setattr(real_requests, "get", lambda *a, **k: _Resp(tables_payload))
    monkeypatch.setattr(real_requests, "post",
                        lambda url, **k: (posts.append((url, k.get("json"))), _Resp(status=200))[1])
    assert dmn.ensure_notes_field("app_test", {}) is True
    assert len(posts) == 1
    assert posts[0][1] == {"name": "Previous Meeting Notes", "type": "multilineText"}


def test_ensure_notes_field_is_a_no_op_when_already_present(monkeypatch):
    posts = []
    tables_payload = {"tables": [{"id": "tblX", "name": "Deals",
                                  "fields": [{"name": "Deal Name"},
                                             {"name": "Updated Meeting Notes"},
                                             {"name": "Previous Meeting Notes"}]}]}
    import requests as real_requests
    monkeypatch.setattr(real_requests, "get", lambda *a, **k: _Resp(tables_payload))
    monkeypatch.setattr(real_requests, "post", lambda url, **k: (posts.append(url), _Resp())[1])
    assert dmn.ensure_notes_field("app_test", {}) is True
    assert posts == []


def test_ensure_notes_field_fails_loudly_if_the_deals_table_is_missing(monkeypatch):
    import requests as real_requests
    monkeypatch.setattr(real_requests, "get", lambda *a, **k: _Resp({"tables": []}))
    assert dmn.ensure_notes_field("app_test", {}) is False
