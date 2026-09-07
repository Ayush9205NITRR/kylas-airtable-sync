"""
_ensure() must ADD columns that are missing from an already-existing table.

The bug this guards: _ensure() only ever created a table that did not exist.
A column added to its `fields` list AFTER the table already existed was never
created on it, and AirtableClient silently drops values for unknown columns
(printing only a WARNING). That is exactly how "Company Id" stayed empty on
every row of "BD Stage Changes" — which then zeroed all four Company-group
metrics downstream, because build_long() skips any change with no company_id:

    cid = ch["company_id"]
    if cid:            # never true, so company_best stays empty
        ...

so "Companies Worked", "Companies Reached", "Requirements Stated" and
"Handoff Calls Held" read 0 for every associate on every digest.
"""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("KYLAS_API_KEY", "test:1")
os.environ.setdefault("AIRTABLE_PAT", "test")
os.environ.setdefault("AIRTABLE_BASE_ID", "app_test")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "bd_stage_changes", os.path.join(_ROOT, "scripts", "bd_stage_changes.py"))
bsc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bsc)


class _Resp:
    def __init__(self, payload=None, status=200):
        self._payload, self.status_code, self.text = payload or {}, status, ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")


def _existing_table(field_names):
    return {"tables": [{"id": "tbl123", "name": "BD Stage Changes",
                        "fields": [{"name": n} for n in field_names]}]}


def test_missing_columns_are_added_to_an_existing_table(monkeypatch):
    posts = []
    monkeypatch.setattr(bsc.requests, "get",
                        lambda *a, **k: _Resp(_existing_table(["Key", "Date"])))
    monkeypatch.setattr(bsc.requests, "post",
                        lambda url, **k: (posts.append((url, k.get("json"))),
                                          _Resp(status=200))[1])

    fields = [{"name": "Key", "type": "singleLineText"},
              {"name": "Date", "type": "singleLineText"},
              {"name": "Company Id", "type": "singleLineText"}]
    assert bsc._ensure("app_test", {}, "BD Stage Changes", fields) is True

    assert len(posts) == 1, "only the genuinely missing column should be added"
    url, body = posts[0]
    assert url.endswith("/tables/tbl123/fields")
    assert body == {"name": "Company Id", "type": "singleLineText"}


def test_a_table_that_already_has_every_column_is_left_alone(monkeypatch):
    posts = []
    monkeypatch.setattr(bsc.requests, "get",
                        lambda *a, **k: _Resp(_existing_table(["Key", "Company Id"])))
    monkeypatch.setattr(bsc.requests, "post",
                        lambda url, **k: (posts.append(url), _Resp(status=200))[1])

    fields = [{"name": "Key", "type": "singleLineText"},
              {"name": "Company Id", "type": "singleLineText"}]
    assert bsc._ensure("app_test", {}, "BD Stage Changes", fields) is True
    assert posts == [], "no column is missing, so nothing should be POSTed"


def test_a_failed_column_add_does_not_abort_the_run(monkeypatch):
    """Losing one column costs that column's data, not the whole write."""
    monkeypatch.setattr(bsc.requests, "get",
                        lambda *a, **k: _Resp(_existing_table(["Key"])))
    monkeypatch.setattr(bsc.requests, "post",
                        lambda url, **k: _Resp(status=422))

    fields = [{"name": "Key", "type": "singleLineText"},
              {"name": "Company Id", "type": "singleLineText"}]
    assert bsc._ensure("app_test", {}, "BD Stage Changes", fields) is True


def test_a_table_that_does_not_exist_yet_is_still_created(monkeypatch):
    """The original behaviour must survive: no table -> create it whole."""
    posts = []
    monkeypatch.setattr(bsc.requests, "get", lambda *a, **k: _Resp({"tables": []}))
    monkeypatch.setattr(bsc.requests, "post",
                        lambda url, **k: (posts.append((url, k.get("json"))),
                                          _Resp(status=200))[1])

    fields = [{"name": "Key", "type": "singleLineText"}]
    assert bsc._ensure("app_test", {}, "BD Stage Changes", fields) is True
    assert len(posts) == 1
    url, body = posts[0]
    assert url.endswith("/tables"), "creates the table, not a field on one"
    assert body["name"] == "BD Stage Changes"
