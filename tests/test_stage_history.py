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


# ── last_call_date / effective_call_date ─────────────────────────────────────

def test_creation_does_not_set_last_call_date():
    snap, _, _ = sh.diff({}, _cur(c1="Yet to Be Mined"), "2026-09-02")
    assert snap["c1"]["last_call_date"] == ""


def test_a_real_stage_change_sets_last_call_date_to_the_observed_day():
    snap, _, _ = sh.diff({}, _cur(c1="Yet to Be Mined"), "2026-09-02")
    snap, _, _ = sh.diff(snap, _cur(c1="LinkedIn Outreach Initiated"), "2026-09-05")
    assert snap["c1"]["last_call_date"] == "2026-09-05"


def test_no_move_leaves_last_call_date_untouched():
    snap, _, _ = sh.diff({}, _cur(c1="Yet to Be Mined"), "2026-09-02")
    snap, _, _ = sh.diff(snap, _cur(c1="Follow-up (1)"), "2026-09-05")
    snap, _, _ = sh.diff(snap, _cur(c1="Follow-up (1)"), "2026-09-10")
    assert snap["c1"]["last_call_date"] == "2026-09-05"


def test_legacy_v1_entry_is_adopted_with_a_blank_last_call_date():
    """An entry from before this field existed must not crash or reset."""
    legacy = {"c1": {"stage": "Follow-up (1)", "owner": "Anjali Athya",
                     "email": "anjali.athya@enout.in", "since": "2026-08-31",
                     "changes": 0}}   # no last_call_date key at all
    snap, changes, stats = sh.diff(legacy, _cur(c1="Follow-up (1)"), "2026-09-01")
    assert stats["changed"] == 0
    assert snap["c1"]["last_call_date"] == ""


def test_effective_call_date_prefers_a_detected_change():
    snap = {"c1": {"last_call_date": "2026-09-05"}}
    assert sh.effective_call_date(snap, "c1", fallback="2026-01-01") == "2026-09-05"


def test_effective_call_date_falls_back_when_nothing_detected_yet():
    """Most contacts today: no detected change, so the caller's fallback
    (cfLastCalledAt, or the activity composite) must be used, not blank —
    otherwise Account Health and BD counts would read as empty for weeks."""
    assert sh.effective_call_date({}, "c1", fallback="2026-07-04") == "2026-07-04"
    assert sh.effective_call_date({"c1": {"last_call_date": ""}}, "c1",
                                  fallback="2026-07-04") == "2026-07-04"


def test_effective_call_date_with_no_fallback_is_blank():
    assert sh.effective_call_date({}, "c1") == ""


def test_effective_call_date_accepts_non_string_contact_ids():
    """Kylas contact ids arrive as ints in some payloads; the snapshot keys
    are strings, so this must not silently miss a match."""
    snap = {"123": {"last_call_date": "2026-09-05"}}
    assert sh.effective_call_date(snap, 123) == "2026-09-05"


def test_a_blank_email_from_one_caller_does_not_erase_a_known_one():
    """Module 2 and bd_stage_changes.py both call diff() on the same snapshot;
    one caller failing to resolve an email must not blank what the other set."""
    snap, _, _ = sh.diff({}, {"c1": {"stage": "Follow-up (1)", "owner": "Anjali Athya",
                                     "email": "anjali.athya@enout.in", "company": "Acme"}},
                        "2026-09-01")
    snap, _, _ = sh.diff(snap, {"c1": {"stage": "MQL (Marketing Qualified Lead)",
                                       "owner": "Anjali Athya", "email": "", "company": ""}},
                        "2026-09-02")
    assert snap["c1"]["email"] == "anjali.athya@enout.in"
    assert snap["c1"]["stage"] == "MQL (Marketing Qualified Lead)"
