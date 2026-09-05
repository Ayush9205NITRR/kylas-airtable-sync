"""
Tests for _last_activity() — the composite date behind "Last Called At (Contacts)".

cfLastCalledAt is blank on a large share of contacts, which left accounts looking
untouched forever. The composite is max(createdAt, updatedAt, cfLastCalledAt).

The load-bearing test here is the last one: the composite must NOT feed the
called/called_apr19 counters. Every contact has a createdAt, so if it did, every
account would read as "called" and the Fresh/Active and Tapped/Stale splits would
collapse silently.
"""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("AIRTABLE_PAT", "x")
os.environ.setdefault("AIRTABLE_BASE_ID", "app_test")

_spec = importlib.util.spec_from_file_location(
    "ah_activity", os.path.join(_ROOT, "modules", "06_account_health.py"))
ah = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ah)


def _ct(created=None, updated=None, called=None, company=77, cid=1):
    return {"id": cid, "company": company,
            "createdAt": created, "updatedAt": updated,
            "customFieldValues": {"cfLastCalledAt": called} if called else {}}


def test_uses_updated_at_when_never_called():
    """The real case from production: cfLastCalledAt blank, timestamps present."""
    assert ah._last_activity(
        _ct("2026-04-08T17:33:29.90", "2026-06-05T07:23:22.11")) == "2026-06-05"


def test_real_call_beats_an_older_update():
    assert ah._last_activity(
        _ct("2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z", "2026-03-09")) == "2026-03-09"


def test_update_after_the_last_call_wins():
    assert ah._last_activity(
        _ct("2026-01-01T00:00:00Z", "2026-08-20T00:00:00Z", "2026-03-09")) == "2026-08-20"


def test_created_only_still_yields_a_date():
    assert ah._last_activity(_ct("2026-07-04T09:00:00Z")) == "2026-07-04"


def test_nothing_usable_is_blank():
    assert ah._last_activity(_ct()) == ""


def test_long_form_call_date_still_parses():
    """cfLastCalledAt also arrives as "Jun 05, 2026" — it must still compare."""
    assert ah._last_activity(
        _ct("2026-01-01T00:00:00Z", None, "Jun 05, 2026")) == "2026-06-05"


def test_account_created_today_is_active_not_fresh():
    """The intended reclassification: activity of any kind counts as touched, so
    a contact created today makes its account Active rather than Fresh — even
    though nobody has phoned it and cfLastCalledAt is empty."""
    health = ah.compute_health([_ct("2026-08-30T00:00:00Z", "2026-08-30T00:00:00Z")])
    e = health["77"]
    assert e["called"] == 1
    assert e["called_apr19"] == 1          # 2026-08-30 is past the Apr 19 cutoff
    assert e["status"] == "Active"
    assert e["status_of_reachout"] == "Tapped – Active"
    assert e["last_activity"] == "2026-08-30"


def test_activity_before_the_cutoff_is_still_stale():
    """Touched, but not since Apr 19 — must stay Stale, or the cutoff is pointless."""
    health = ah.compute_health([_ct("2026-01-05T00:00:00Z", "2026-02-02T00:00:00Z")])
    e = health["77"]
    assert e["called"] == 1
    assert e["called_apr19"] == 0
    assert e["status_of_reachout"] == "Stale"


def test_exhausted_still_wins_over_activity():
    """Exhausted is evaluated ahead of the activity branch and must survive."""
    # two NOI contacts → noi >= 2 → Exhausted, despite fresh activity dates
    health2 = ah.compute_health([
        dict(_ct("2026-08-30T00:00:00Z", "2026-08-30T00:00:00Z", cid=1),
             customFieldValues={"cfPipelineStageBd": "Not Interested"}),
        dict(_ct("2026-08-30T00:00:00Z", "2026-08-30T00:00:00Z", cid=2),
             customFieldValues={"cfPipelineStageBd": "Not Interested"}),
    ])
    assert health2["77"]["status"] == "Exhausted", health2["77"]["status"]


def test_account_takes_the_latest_activity_across_its_contacts():
    health = ah.compute_health([
        _ct("2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z", cid=1),
        _ct("2026-01-01T00:00:00Z", "2026-08-25T00:00:00Z", cid=2),
    ])
    assert health["77"]["last_activity"] == "2026-08-25"


# ── Account Pipeline Stage coverage for companies with no contacts ────────────

class _FakeTbl:
    """Minimal AirtableClient stand-in: records what _write_table would write."""
    def __init__(self, cache):
        self._cache, self._updates, self.flushed = cache, [], False
        self.table = self
    def all(self):
        return list(self._cache.values())
    def flush(self):
        self.flushed = True


_FM = {"id": "Kylas Company ID", "accountPipelineStage": "Account Pipeline Stage"}


def _rec(rid, kid, stage=None):
    f = {"Kylas Company ID": kid}
    if stage is not None:
        f["Account Pipeline Stage"] = stage
    return {"id": rid, "fields": f}


def _written(updates):
    return {kid: fields.get("Account Pipeline Stage") for kid, _rid, fields in updates}


def test_company_with_no_contacts_is_marked_yet_to_be_mined():
    """It never appears in health (built from contacts), so without the sweep
    its column would stay blank forever."""
    tbl = _FakeTbl({"1": _rec("rec1", "1"), "99": _rec("rec99", "99")})
    health = {"1": {"status": "Active", "account_pipeline_stage": "SQL (Sales Qualified Lead)",
                    "last_called": "", "needs_reassign": False}}
    ah._write_table(tbl, health, _FM)
    w = _written(tbl._updates)
    assert w["1"] == "SQL (Sales Qualified Lead)"
    assert w["99"] == ah.NO_CONTACT_STAGE, "company with no contacts must be marked"
    assert tbl.flushed


def test_no_contact_company_already_correct_is_not_rewritten():
    tbl = _FakeTbl({"99": _rec("rec99", "99", ah.NO_CONTACT_STAGE)})
    ah._write_table(tbl, {}, _FM)
    assert tbl._updates == [], "must not spend an update re-writing the same value"


def test_company_with_contacts_but_no_rank_is_unmined_not_no_contacts():
    """It HAS people, they just have unrecognised stages. That is a different
    state from having nobody at all, and must not be collapsed into it."""
    tbl = _FakeTbl({"1": _rec("rec1", "1")})
    health = {"1": {"status": "Active", "account_pipeline_stage": "",
                    "last_called": "", "needs_reassign": False}}
    ah._write_table(tbl, health, _FM)
    assert _written(tbl._updates)["1"] == ah.UNMINED_STAGE
    assert ah.UNMINED_STAGE != ah.NO_CONTACT_STAGE


def test_failed_ranking_leaves_the_column_untouched():
    """If the ranking blew up upstream the key is absent — existing Airtable
    data must be preserved, not blanked or stamped un-mined."""
    tbl = _FakeTbl({"1": _rec("rec1", "1", "SQL (Sales Qualified Lead)")})
    health = {"1": {"status": "Active", "last_called": "", "needs_reassign": False}}
    ah._write_table(tbl, health, _FM)
    for _kid, _rid, fields in tbl._updates:
        assert "Account Pipeline Stage" not in fields


def test_the_two_empty_states_are_labelled_differently():
    """No contacts at all vs contacts nobody ranked — distinct labels."""
    assert ah.NO_CONTACT_STAGE == "No Contacts"
    assert ah.UNMINED_STAGE == "Yet to Be Mined"

    tbl = _FakeTbl({"1": _rec("rec1", "1"), "99": _rec("rec99", "99")})
    health = {"1": {"status": "Active", "account_pipeline_stage": "",
                    "last_called": "", "needs_reassign": False}}
    ah._write_table(tbl, health, _FM)
    w = _written(tbl._updates)
    assert w["1"] == "Yet to Be Mined", "has contacts, none ranked"
    assert w["99"] == "No Contacts", "no contacts at all"


# ── stage-change date takes precedence in compute_health ─────────────────────

def test_detected_stage_change_overrides_the_activity_composite():
    """A contact with a DETECTED change should use that date, even if the
    composite (createdAt/updatedAt) would suggest something more recent —
    the whole point of the stage-change date is that it is not bumped by
    unrelated edits."""
    ct = _ct("2026-01-01T00:00:00Z", "2026-08-30T00:00:00Z", cid=1)
    snap = {"1": {"last_call_date": "2026-06-15"}}
    health = ah.compute_health([ct], stage_snap=snap)
    assert health["77"]["last_activity"] == "2026-06-15"


def test_no_detected_change_falls_back_to_the_composite():
    ct = _ct("2026-01-01T00:00:00Z", "2026-08-30T00:00:00Z", cid=1)
    health = ah.compute_health([ct], stage_snap={"1": {"last_call_date": ""}})
    assert health["77"]["last_activity"] == "2026-08-30"


def test_stage_snap_defaults_to_empty_without_error():
    ct = _ct("2026-01-01T00:00:00Z", "2026-08-30T00:00:00Z", cid=1)
    health = ah.compute_health([ct])
    assert health["77"]["last_activity"] == "2026-08-30"
