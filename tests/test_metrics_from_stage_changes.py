"""
BD Metrics Daily is derived from stage MOVEMENT, not current position.

The distinction these tests protect: a contact used to count on every day its
"current stage" was read, which quietly inflated every day after the move. Now
it counts once, on the day it actually moved.

Run: python -m pytest tests/test_metrics_from_stage_changes.py -q
"""
import importlib.util
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("KYLAS_API_KEY", "test:1")
os.environ.setdefault("AIRTABLE_PAT", "test")
os.environ.setdefault("AIRTABLE_BASE_ID", "app_test")


def _long():
    spec = importlib.util.spec_from_file_location(
        "bd_metrics_long",
        os.path.join(os.path.dirname(os.path.dirname(__file__)),
                     "scripts", "bd_metrics_long.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


long_mod = _long()


def _chg(date, to, rep="Anjali Athya", email="anjali.athya@enout.in", cid="501"):
    return {"date": date, "rep": rep, "email": email, "company_id": cid, "to": to}


def _rows(changes):
    rows, _ = long_mod.build_long(all_owners=True, changes=changes)
    return rows


def test_a_move_to_a_real_stage_counts_as_attempted_and_connected():
    rows = _rows([_chg("2026-09-08", "Follow-up (1)")])
    k = ("Anjali Athya", "anjali.athya@enout.in", "2026-09-08", "Contact")
    assert rows[(*k, "Call Attempted")] == 1
    assert rows[(*k, "Call Connected")] == 1


def test_a_move_to_a_cnc_stage_is_attempted_but_not_connected():
    """Tried and could not reach them — that is the whole point of the CNC
    stages, and it is the attempted/connected split the team cares about."""
    rows = _rows([_chg("2026-09-08", "CNC (Could Not Connect) - 1")])
    k = ("Anjali Athya", "anjali.athya@enout.in", "2026-09-08", "Contact")
    assert rows[(*k, "Call Attempted")] == 1
    assert rows[(*k, "Call Connected")] == 0


def test_a_move_to_the_bottom_stage_is_not_an_attempt():
    """Landing back on the un-mined stage — a rename or a regression — is a
    stage change but never evidence that anyone called."""
    rows = _rows([_chg("2026-09-08", "LinkedIn Outreach Initiated")])
    k = ("Anjali Athya", "anjali.athya@enout.in", "2026-09-08", "Contact")
    # An all-zero contact key emits no row at all, which reads as zero just the
    # same — .get() asserts the outcome rather than the storage detail.
    assert rows.get((*k, "Call Attempted"), 0) == 0
    assert rows.get((*k, "Call Connected"), 0) == 0


def test_a_contact_counts_only_on_the_day_it_moved():
    """The core behaviour change. Two moves on two days = one count each,
    never a running total carried forward."""
    rows = _rows([_chg("2026-09-08", "Follow-up (1)"),
                  _chg("2026-09-09", "MQL (Marketing Qualified Lead)")])
    base = ("Anjali Athya", "anjali.athya@enout.in")
    assert rows[(*base, "2026-09-08", "Contact", "Call Attempted")] == 1
    assert rows[(*base, "2026-09-09", "Contact", "Call Attempted")] == 1
    assert rows[(*base, "2026-09-08", "Contact", "MQL")] == 0
    assert rows[(*base, "2026-09-09", "Contact", "MQL")] == 1


def test_several_moves_by_one_rep_on_one_day_accumulate():
    rows = _rows([_chg("2026-09-08", "Follow-up (1)", cid="1"),
                  _chg("2026-09-08", "Follow-up (2)", cid="2"),
                  _chg("2026-09-08", "CNC (Could Not Connect) - 2", cid="3")])
    k = ("Anjali Athya", "anjali.athya@enout.in", "2026-09-08", "Contact")
    assert rows[(*k, "Call Attempted")] == 3
    assert rows[(*k, "Call Connected")] == 2


def test_reps_are_kept_separate():
    rows = _rows([_chg("2026-09-08", "Follow-up (1)"),
                  _chg("2026-09-08", "Follow-up (1)",
                       rep="Gaurav Kumar", email="gaurav@enout.in")])
    for rep, email in (("Anjali Athya", "anjali.athya@enout.in"),
                       ("Gaurav Kumar", "gaurav@enout.in")):
        assert rows[(rep, email, "2026-09-08", "Contact", "Call Attempted")] == 1


def test_sql_is_counted_from_the_move_into_sql():
    rows = _rows([_chg("2026-09-08", "SQL (Sales Qualified Lead)")])
    k = ("Anjali Athya", "anjali.athya@enout.in", "2026-09-08", "Contact")
    assert rows[(*k, "SQL")] == 1


def test_an_empty_log_produces_no_rows_rather_than_stale_numbers():
    """A day nobody moved anything is zero. Previously this read as whatever
    every contact's current stage happened to be."""
    assert _rows([]) == {}


def test_rows_missing_a_date_owner_or_stage_are_dropped_not_guessed():
    rows = _rows([
        _chg("", "Follow-up (1)"),                       # no date
        _chg("2026-09-08", ""),                          # no stage
        {"date": "2026-09-08", "rep": "", "email": "x@y.z",
         "company_id": "1", "to": "Follow-up (1)"},      # no owner
    ])
    assert rows == {}


def test_off_roster_owners_are_excluded_when_a_roster_applies(monkeypatch):
    monkeypatch.setattr(long_mod.funnel, "bd_roster",
                        lambda: {"anjali.athya@enout.in"})
    changes = [_chg("2026-09-08", "Follow-up (1)"),
               _chg("2026-09-08", "Follow-up (1)",
                    rep="Someone Else", email="someone@elsewhere.com")]
    rows, stats = long_mod.build_long(all_owners=False, changes=changes)
    assert stats["skipped"] == 1
    assert not any(k[0] == "Someone Else" for k in rows)
