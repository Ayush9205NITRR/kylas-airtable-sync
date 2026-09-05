"""
Tests for contact stage-change detection.

The load-bearing case is the first run: with no prior snapshot, every contact
is a first sighting. If those were emitted as changes, day one would report
~37k "moves" that are simply the initial read, and every downstream count
would be wrong from the outset.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from utils import stage_history as sh  # noqa: E402


def _cur(**kw):
    return {cid: {"stage": s, "owner": "Anjali Athya",
                  "email": "anjali.athya@enout.in", "company": "Acme",
                  "name": f"C{cid}"}
            for cid, s in kw.items()}


def test_first_run_is_a_baseline_not_a_wave_of_changes():
    snap, changes, stats = sh.diff({}, _cur(c1="Follow-up (1)", c2="MQL (Marketing Qualified Lead)"),
                                   "2026-09-02")
    assert changes == [], "a first sighting is not a move"
    assert stats["new"] == 2 and stats["changed"] == 0
    assert snap["c1"]["stage"] == "Follow-up (1)"
    assert snap["c1"]["since"] == "2026-09-02"
    assert snap["c1"]["changes"] == 0


def test_a_move_is_recorded_with_from_and_to():
    snap, _, _ = sh.diff({}, _cur(c1="CNC (Could Not Connect) - 1"), "2026-09-02")
    snap, changes, stats = sh.diff(snap, _cur(c1="Follow-up (1)"), "2026-09-03")
    assert stats["changed"] == 1 and len(changes) == 1
    c = changes[0]
    assert c["from"] == "CNC (Could Not Connect) - 1"
    assert c["to"] == "Follow-up (1)"
    assert c["date"] == "2026-09-03"
    assert c["owner"] == "Anjali Athya"
    assert snap["c1"]["changes"] == 1
    assert snap["c1"]["since"] == "2026-09-03"


def test_no_move_produces_no_row():
    snap, _, _ = sh.diff({}, _cur(c1="Follow-up (1)"), "2026-09-02")
    snap, changes, stats = sh.diff(snap, _cur(c1="Follow-up (1)"), "2026-09-03")
    assert changes == []
    assert stats["unchanged"] == 1
    assert snap["c1"]["since"] == "2026-09-02", "since must not be bumped"


def test_repeat_moves_accumulate_on_the_contact():
    snap, _, _ = sh.diff({}, _cur(c1="CNC (Could Not Connect) - 1"), "2026-09-01")
    snap, _, _ = sh.diff(snap, _cur(c1="Follow-up (1)"), "2026-09-02")
    snap, _, _ = sh.diff(snap, _cur(c1="MQL (Marketing Qualified Lead)"), "2026-09-03")
    assert snap["c1"]["changes"] == 2


def test_a_contact_that_disappears_is_carried_not_dropped():
    snap, _, _ = sh.diff({}, _cur(c1="Follow-up (1)", c2="MQL (Marketing Qualified Lead)"),
                         "2026-09-02")
    snap, changes, stats = sh.diff(snap, _cur(c1="Follow-up (1)"), "2026-09-03")
    assert "c2" in snap, "history must survive a contact leaving the filter"
    assert stats["carried"] == 1
    assert changes == []


def test_owner_change_alone_is_not_a_stage_change():
    """Reassignment moves the owner but not the pipeline stage."""
    snap, _, _ = sh.diff({}, _cur(c1="Follow-up (1)"), "2026-09-02")
    moved = {"c1": {"stage": "Follow-up (1)", "owner": "Mayra Singh",
                    "email": "mayra@enout.in", "company": "Acme", "name": "C1"}}
    snap, changes, stats = sh.diff(snap, moved, "2026-09-03")
    assert changes == []
    assert stats["changed"] == 0
    assert snap["c1"]["owner"] == "Mayra Singh", "owner must still be refreshed"


def test_save_then_load_round_trips(tmp_path):
    path = str(tmp_path / "stage.json")
    snap, _, _ = sh.diff({}, _cur(c1="Follow-up (1)"), "2026-09-02")
    sh.save(snap, path, today="2026-09-02")
    assert sh.load(path) == snap
    with open(path) as fh:
        assert json.load(fh)["schema_version"] == sh.SCHEMA_VERSION


def test_missing_or_corrupt_snapshot_reads_as_first_run(tmp_path):
    assert sh.load(str(tmp_path / "nope.json")) == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert sh.load(str(bad)) == {}
