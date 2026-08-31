"""
Tests for the Account Health snapshot.

The load-bearing cases are the ones where a change is NOT real movement:
a formula change (which moves thousands of accounts at once) and a month
rollover. If either is miscounted, the monthly reallocation numbers are wrong
in a way nobody would notice for weeks.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from utils import health_history as hh  # noqa: E402

V = hh.FORMULA_VERSION


def _health(**kw):
    return {cid: {"status": s} for cid, s in kw.items()}


def test_first_run_baselines_everything_and_counts_nothing():
    snap, stats = hh.apply({}, _health(a="Active", b="MQL - Action Needed"), "2026-09-01")
    assert stats["new"] == 2 and stats["changed"] == 0
    assert snap["a"] == {"status": "Active", "baseline": "Active", "prev": "",
                         "changed": "", "count": 0, "month": "2026-09", "v": V}


def test_unchanged_account_stays_at_zero():
    snap, _ = hh.apply({}, _health(a="Active"), "2026-09-01")
    snap, stats = hh.apply(snap, _health(a="Active"), "2026-09-02")
    assert stats["unchanged"] == 1 and stats["changed"] == 0
    assert snap["a"]["count"] == 0
    assert snap["a"]["changed"] == ""


def test_a_real_change_is_recorded_with_from_and_to():
    snap, _ = hh.apply({}, _health(a="Active"), "2026-09-01")
    snap, stats = hh.apply(snap, _health(a="SQL"), "2026-09-14")
    assert stats["changed"] == 1
    e = snap["a"]
    assert (e["prev"], e["status"], e["changed"], e["count"]) == \
           ("Active", "SQL", "2026-09-14", 1)
    assert e["baseline"] == "Active", "baseline must survive the change"


def test_repeat_changes_accumulate_within_the_month():
    snap, _ = hh.apply({}, _health(a="Active"), "2026-09-01")
    snap, _ = hh.apply(snap, _health(a="MQL - Action Needed"), "2026-09-10")
    snap, _ = hh.apply(snap, _health(a="SQL"), "2026-09-20")
    e = snap["a"]
    assert e["count"] == 2
    assert e["baseline"] == "Active"      # still September's starting point
    assert e["prev"] == "MQL - Action Needed"


def test_month_rollover_resets_baseline_and_count_but_not_status():
    snap, _ = hh.apply({}, _health(a="Active"), "2026-09-01")
    snap, _ = hh.apply(snap, _health(a="SQL"), "2026-09-20")
    assert snap["a"]["count"] == 1

    snap, stats = hh.apply(snap, _health(a="SQL"), "2026-10-01")
    assert stats["month_rollover"] == 1
    e = snap["a"]
    assert e["month"] == "2026-10"
    assert e["baseline"] == "SQL", "October starts from what September ended at"
    assert e["count"] == 0
    assert e["status"] == "SQL"


def test_change_on_the_first_day_of_a_new_month_counts_once():
    snap, _ = hh.apply({}, _health(a="Active"), "2026-09-15")
    snap, _ = hh.apply(snap, _health(a="SQL"), "2026-10-01")
    e = snap["a"]
    assert e["month"] == "2026-10"
    assert e["baseline"] == "Active", "the value carried into October"
    assert e["count"] == 1
    assert e["status"] == "SQL"


def test_formula_change_rebaselines_instead_of_counting(monkeypatch):
    """The whole point: a redefinition must not look like 4,700 real moves."""
    snap, _ = hh.apply({}, _health(a="Fresh"), "2026-09-01")
    monkeypatch.setattr(hh, "FORMULA_VERSION", V + 1)
    snap, stats = hh.apply(snap, _health(a="Active"), "2026-09-02")
    assert stats["rebaselined"] == 1 and stats["changed"] == 0
    e = snap["a"]
    assert e["count"] == 0
    assert e["baseline"] == "Active"
    assert e["changed"] == ""


def test_deleted_accounts_are_carried_through_not_dropped():
    snap, _ = hh.apply({}, _health(a="Active", b="SQL"), "2026-09-01")
    snap, stats = hh.apply(snap, _health(a="Active"), "2026-09-02")
    assert "b" in snap, "history must survive an account leaving Kylas"
    assert snap["b"]["status"] == "SQL"
    assert stats["carried"] == 1


def test_save_then_load_round_trips(tmp_path):
    path = str(tmp_path / "snap.json")
    snap, _ = hh.apply({}, _health(a="Active"), "2026-09-01")
    hh.save(snap, path, today="2026-09-01")
    assert hh.load(path) == snap
    with open(path) as fh:
        assert json.load(fh)["formula_version"] == V


def test_missing_or_corrupt_file_reads_as_first_run(tmp_path):
    assert hh.load(str(tmp_path / "nope.json")) == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert hh.load(str(bad)) == {}
