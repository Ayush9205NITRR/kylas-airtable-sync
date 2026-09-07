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


# ── push() must report whether the change log actually got written ─────────
#
# This is the contract every caller relies on to decide whether it may advance
# the stage snapshot. stage_history.diff() is first-write-wins: a change is
# reported once, and the moment the snapshot moves past it, it is gone. Every
# stage move before ~1:30 PM IST on 2026-09-07 was destroyed exactly this way
# — detected, not written (the push crashed), snapshot advanced regardless.

class _FakeAt:
    """Stand-in for AirtableClient that records what it was asked to write."""
    def __init__(self, *a, **k):
        self.rows = []

    def build_cache(self, _key):
        return 0

    def upsert(self, _kf, _kv, fields, _stamp, updated_at_field=""):
        self.rows.append(fields)
        return "created", None

    def flush(self):
        pass


def _patch_airtable(monkeypatch, at=None, ensure=True):
    """Point push() at a fake Airtable and a controllable _ensure()."""
    import utils.airtable_client as ac
    monkeypatch.setattr(ac, "AirtableClient", lambda *a, **k: at or _FakeAt())
    monkeypatch.setattr(bsc, "_ensure", lambda *a, **k: ensure)
    monkeypatch.setattr(bsc.funnel, "prune_expired", lambda *a, **k: None)
    monkeypatch.setenv("AIRTABLE_BASE_ID", "app_test")
    monkeypatch.setenv("AIRTABLE_PAT", "test")


def _change(cid="1", to="Follow-up (1)"):
    return {"contact_id": cid, "name": "A Contact", "owner": "Anjali Athya",
            "email": "anjali.athya@enout.in", "company": "Acme",
            "company_id": "501", "from": "CNC (Could Not Connect) - 1",
            "to": to, "date": "2026-09-07"}


def test_push_reports_success_when_the_log_is_written(monkeypatch):
    at = _FakeAt()
    _patch_airtable(monkeypatch, at=at)
    assert bsc.push([_change()], {}) is True
    assert len(at.rows) == 1


def test_push_reports_failure_when_the_log_table_is_unavailable(monkeypatch):
    """_ensure() returning False used to drop the rows and still look like
    success — the caller then advanced the snapshot over them."""
    _patch_airtable(monkeypatch, ensure=False)
    assert bsc.push([_change()], {}) is False


def test_push_reports_failure_when_the_write_raises(monkeypatch):
    class _Boom(_FakeAt):
        def upsert(self, *a, **k):
            raise RuntimeError("Airtable 503")

    _patch_airtable(monkeypatch, at=_Boom())
    assert bsc.push([_change()], {}) is False


def test_push_with_nothing_to_log_is_a_success(monkeypatch):
    """No changes means nothing can be lost, so the snapshot may advance."""
    _patch_airtable(monkeypatch)
    assert bsc.push([], {}) is True


def test_rows_dropped_at_the_record_limit_count_as_a_failed_write(monkeypatch):
    """AirtableClient swallows TOO_MANY_RECORDS_IN_BASE: it warns, drops the
    rows and returns normally. Without this check that is indistinguishable
    from a clean write, so the snapshot advances over changes that were never
    stored — the same silent loss as the KeyError, by a different route."""
    at = _FakeAt()
    at.dropped_records = 3
    _patch_airtable(monkeypatch, at=at)
    assert bsc.push([_change()], {}) is False


def test_a_clean_write_reports_no_dropped_rows(monkeypatch):
    at = _FakeAt()
    at.dropped_records = 0
    _patch_airtable(monkeypatch, at=at)
    assert bsc.push([_change()], {}) is True


def test_a_failed_rollup_does_not_block_the_snapshot(monkeypatch):
    """The per-day rollup is derived — every digest recomputes from the log
    table — so losing a rollup day costs a view, not a number. Blocking on it
    would wedge the pipeline into retrying forever."""
    at = _FakeAt()

    def _ensure(_b, _h, name, _f):
        return name != bsc.DAILY_TABLE      # log fine, rollup unavailable

    import utils.airtable_client as ac
    monkeypatch.setattr(ac, "AirtableClient", lambda *a, **k: at)
    monkeypatch.setattr(bsc, "_ensure", _ensure)
    monkeypatch.setattr(bsc.funnel, "prune_expired", lambda *a, **k: None)
    monkeypatch.setenv("AIRTABLE_BASE_ID", "app_test")
    monkeypatch.setenv("AIRTABLE_PAT", "test")

    grid = {("Anjali Athya", "anjali.athya@enout.in", "2026-09-07"):
            {"Stage Changes": 1, "Forward Moves": 1, "Backward Moves": 0}}
    assert bsc.push([_change()], grid) is True


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
