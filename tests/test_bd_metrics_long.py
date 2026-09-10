"""
Tests for the long/tidy metrics table.

The risk here is silent drift: these six contact metrics are ALSO computed by
bd_monthly_matrix.py, and if the two ever disagree the daily chart and the
monthly matrix show different numbers for the same thing. So the tests assert
against the matrix's own stage sets rather than against restated literals.
"""
import importlib.util
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("AIRTABLE_PAT", "x")
os.environ.setdefault("AIRTABLE_BASE_ID", "app_test")

_spec = importlib.util.spec_from_file_location(
    "bd_metrics_long", os.path.join(_ROOT, "scripts", "bd_metrics_long.py"))
ml = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ml)

_norm = ml.matrix._norm
_real_roster_names_and_emails = ml._roster_names_and_emails


def _on(stage):
    """Metric names that fire for a stage."""
    return {k for k, v in ml._contact_counts(_norm(stage), None).items() if v}


def test_metric_names_are_exactly_what_the_dashboard_expects():
    assert ml.CONTACT_METRICS == ["Call Attempted", "Call Connected",
                                  "Meeting Booked", "Meeting Done", "SQL", "MQL"]
    assert ml.COMPANY_METRICS == ml.funnel.COLUMNS


def test_sql_fires_the_whole_contact_funnel():
    """SQL is inside MEETING_DONE_STAGES, which is inside MEETING_BOOKED_STAGES,
    so an SQL contact must count at every level above it too."""
    assert _on("SQL (Sales Qualified Lead)") == {
        "Call Attempted", "Call Connected", "Meeting Booked", "Meeting Done", "SQL"}


def test_mql_is_connected_but_not_a_meeting():
    assert _on("MQL (Marketing Qualified Lead)") == {
        "Call Attempted", "Call Connected", "MQL"}


def test_booked_but_not_done():
    assert _on("Discovery Call Booked") == {
        "Call Attempted", "Call Connected", "Meeting Booked"}


def test_cnc_is_attempted_but_never_connected():
    for stage in ml.matrix.CNC_EXCLUDE_STAGES:
        assert _on(stage) == {"Call Attempted"}, stage


def test_unworked_stages_fire_nothing():
    for stage in ("Yet to Be Mined", ""):
        assert _on(stage) == set()


def test_agrees_with_the_matrix_stage_sets():
    """Whatever the matrix calls a booked/done/MQL stage, this must agree."""
    for stage in ml.matrix.MEETING_BOOKED_STAGES:
        assert "Meeting Booked" in _on(stage), stage
    for stage in ml.matrix.MEETING_DONE_STAGES:
        assert "Meeting Done" in _on(stage), stage
    for stage in ml.matrix.MQL_STAGES:
        assert "MQL" in _on(stage), stage


def test_attempted_is_a_superset_of_every_other_contact_metric():
    """Nothing can happen on a contact that was never attempted."""
    for stage in (list(ml.matrix.MEETING_BOOKED_STAGES)
                  + list(ml.matrix.MQL_STAGES)
                  + list(ml.matrix.CNC_EXCLUDE_STAGES)):
        fired = _on(stage)
        if fired:
            assert "Call Attempted" in fired, stage


def test_long_key_shape_is_unique_per_metric():
    """The Airtable Key must distinguish rep, day, group and metric, or rows
    would collide and overwrite each other."""
    keys = {f"{r} | {d} | {g} | {m}"
            for r in ("Aditi saini", "Mayra Singh")
            for d in ("2026-09-01", "2026-09-02")
            for g in ("Contact", "Company")
            for m in ("SQL", "Meeting Booked")}
    assert len(keys) == 2 * 2 * 2 * 2


# ── team digest: windowing + sort ────────────────────────────────────────────

def _row(rep, email, day, group, metric, value):
    return (rep, email, day, group, metric), value


@pytest.fixture(autouse=True)
def _no_roster_backfill(monkeypatch):
    """Default every test in this file to no roster backfill.

    Without this, team_digest_rows() would call the real funnel.bd_roster()
    (a live Airtable/network call) and read this repo's actual
    config/team.json, making test output depend on live data instead of each
    test's own fixture rows. Tests that want to exercise the backfill itself
    override this explicitly.
    """
    monkeypatch.setattr(ml, "_roster_names_and_emails", lambda: {})


def test_today_columns_use_only_todays_row():
    rows = dict([
        _row("Anjali Athya", "anjali.athya@enout.in", "2026-09-05", "Contact", "Call Attempted", 5),
        _row("Anjali Athya", "anjali.athya@enout.in", "2026-09-04", "Contact", "Call Attempted", 99),
    ])
    out = {r["rep"]: r for r in ml.team_digest_rows(rows, "2026-09-05")}
    assert out["Anjali Athya"]["Call Attempted"] == 5


def test_month_column_sums_the_whole_month_not_just_today():
    rows = dict([
        _row("Anjali Athya", "a@x", "2026-09-01", "Contact", "SQL", 2),
        _row("Anjali Athya", "a@x", "2026-09-04", "Contact", "SQL", 1),
        _row("Anjali Athya", "a@x", "2026-09-05", "Contact", "SQL", 3),
        _row("Anjali Athya", "a@x", "2026-08-30", "Contact", "SQL", 100),  # different month
    ])
    out = {r["rep"]: r for r in ml.team_digest_rows(rows, "2026-09-05")}
    assert out["Anjali Athya"]["SQL (This Month)"] == 6


def test_week_column_sums_the_iso_week_only():
    # 2026-08-31 and 2026-09-01 are the same ISO week (see test_bd_company_funnel).
    rows = dict([
        _row("Gaurav Kumar", "g@x", "2026-08-31", "Company", "Handoff Calls Held", 2),
        _row("Gaurav Kumar", "g@x", "2026-09-01", "Company", "Handoff Calls Held", 3),
        _row("Gaurav Kumar", "g@x", "2026-08-20", "Company", "Handoff Calls Held", 50),  # earlier week
    ])
    out = {r["rep"]: r for r in ml.team_digest_rows(rows, "2026-09-01")}
    assert out["Gaurav Kumar"]["Handoff Calls (This Week)"] == 5


def test_sorted_by_sql_this_month_descending():
    rows = dict([
        _row("Low SQL",  "l@x", "2026-09-05", "Contact", "SQL", 1),
        _row("High SQL", "h@x", "2026-09-05", "Contact", "SQL", 9),
        _row("Mid SQL",  "m@x", "2026-09-05", "Contact", "SQL", 4),
    ])
    out = ml.team_digest_rows(rows, "2026-09-05")
    assert [r["rep"] for r in out] == ["High SQL", "Mid SQL", "Low SQL"]


def test_rep_with_no_metrics_this_period_still_appears_with_zeros():
    rows = dict([_row("Quiet Rep", "q@x", "2026-08-01", "Contact", "Call Attempted", 1)])
    out = {r["rep"]: r for r in ml.team_digest_rows(rows, "2026-09-05")}
    assert out["Quiet Rep"].get("Call Attempted", 0) == 0
    assert out["Quiet Rep"].get("SQL (This Month)", 0) == 0


# ── daily digest subject line: the two sends must be tellable apart ─────────
#
# Both daily sends used to carry the identical subject "BD Daily Digest —
# <date>", so in an inbox the midday numbers were indistinguishable from the
# day's final figures.

def test_the_two_daily_sends_get_different_subject_lines():
    midday = ml.digest_title("2026-09-07", "daily", "midday")
    evening = ml.digest_title("2026-09-07", "daily", "evening")
    assert midday != evening
    assert midday == "BD Daily Digest (Midday) — 2026-09-07"
    assert evening == "BD Daily Digest (End of Day) — 2026-09-07"


def test_an_absent_or_unknown_slot_falls_back_to_the_plain_subject():
    """A missing or typo'd slot must not put a raw flag value in front of the
    whole team — it degrades to the original unlabelled subject."""
    plain = "BD Daily Digest — 2026-09-07"
    assert ml.digest_title("2026-09-07", "daily") == plain
    assert ml.digest_title("2026-09-07", "daily", "") == plain
    assert ml.digest_title("2026-09-07", "daily", "lunchtime") == plain


def test_slot_is_ignored_for_weekly_and_monthly():
    """Those run once per period, so there is nothing to disambiguate."""
    assert "Midday" not in ml.digest_title("2026-09-07", "weekly", "midday")
    assert "Midday" not in ml.digest_title("2026-09-07", "monthly", "midday")


def test_slot_reaches_the_rendered_html_subject_heading():
    html = ml.build_digest_html([], "2026-09-07", "daily", "evening")
    assert "BD Daily Digest (End of Day) — 2026-09-07" in html


def test_digest_html_contains_every_rep_and_is_valid_enough():
    rows = ml.team_digest_rows(
        dict([_row("Anjali Athya", "a@x", "2026-09-05", "Contact", "SQL", 3)]),
        "2026-09-05")
    html = ml.build_digest_html(rows, "2026-09-05")
    assert "Anjali Athya" in html
    assert html.startswith("<!DOCTYPE html>")
    assert html.count("<table") == 1


# ── team digest: active-roster backfill on a zero-activity day ──────────────
#
# The bug this section guards: on 2026-09-06 (a Sunday) the stage-change log
# had exactly 0 entries, so long_rows came back completely empty. The digest
# email that went out showed nothing but a single zeroed "TEAM TOTAL" row --
# all 12 active reps had silently vanished instead of showing up idle.

def test_active_roster_members_appear_even_with_zero_stage_changes(monkeypatch):
    monkeypatch.setattr(ml, "_roster_names_and_emails",
                        lambda: {"Idle Rep": "idle@x", "Another Rep": "another@x"})
    out = {r["rep"]: r for r in ml.team_digest_rows({}, "2026-09-06")}
    assert set(out) == {"Idle Rep", "Another Rep"}
    assert out["Idle Rep"]["email"] == "idle@x"
    assert out["Idle Rep"].get("Call Attempted", 0) == 0
    assert out["Idle Rep"].get("SQL (This Month)", 0) == 0


def test_roster_backfill_does_not_duplicate_a_rep_already_in_long_rows(monkeypatch):
    """A rep with real activity must get exactly one row, not a second
    zeroed one just because they're also on the active roster."""
    monkeypatch.setattr(ml, "_roster_names_and_emails",
                        lambda: {"Anjali Athya": "anjali.athya@enout.in"})
    rows = dict([_row("Anjali Athya", "anjali.athya@enout.in", "2026-09-05",
                      "Contact", "SQL", 3)])
    out = ml.team_digest_rows(rows, "2026-09-05")
    assert len(out) == 1
    assert out[0]["SQL (This Month)"] == 3


def test_roster_names_and_emails_filters_to_the_active_set(tmp_path, monkeypatch):
    # Undo the autouse fixture above -- this test exercises the real function.
    monkeypatch.setattr(ml, "_roster_names_and_emails", _real_roster_names_and_emails)
    monkeypatch.setattr(ml.funnel, "bd_roster", lambda: {"active@x"})
    monkeypatch.setattr(ml, "__file__", str(tmp_path / "scripts" / "bd_metrics_long.py"))
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "team.json").write_text(
        '{"kylas_user_emails": {"Active Rep": "active@x", "Inactive Rep": "gone@x"}}')
    assert ml._roster_names_and_emails() == {"Active Rep": "active@x"}


def test_roster_names_and_emails_returns_empty_when_roster_unavailable(monkeypatch):
    monkeypatch.setattr(ml, "_roster_names_and_emails", _real_roster_names_and_emails)
    def _boom():
        raise RuntimeError("Airtable down")
    monkeypatch.setattr(ml.funnel, "bd_roster", _boom)
    assert ml._roster_names_and_emails() == {}


# ── read_stage_changes: an unusable row must be reported, never silent ──────
# This filter hid a total attribution failure once: 06_account_health.py logged
# every move it detected with a blank BD Associate, each row was discarded here
# without a word, and the snapshot had already advanced past them. The digest
# read 0 for the whole team while 92 real moves sat unattributed in the log.

class _FakeCacheAt:
    def __init__(self, rows):
        self._cache = {str(i): {"fields": f} for i, f in enumerate(rows)}

    def build_cache(self, _key):
        return None


def _patch_at(monkeypatch, rows):
    import utils.airtable_client as ac
    monkeypatch.setattr(ac, "AirtableClient", lambda *a, **k: _FakeCacheAt(rows))


def test_read_stage_changes_keeps_a_fully_attributed_row(monkeypatch):
    _patch_at(monkeypatch, [{"Date": "2026-09-10", "BD Associate": "Aditi saini",
                             "BD Email": "aditi.saini@enout.in",
                             "Company Id": "501", "Current Stage": "Follow-up (1)"}])
    rows = ml.read_stage_changes()
    assert len(rows) == 1 and rows[0]["rep"] == "Aditi saini"


def test_read_stage_changes_warns_loudly_about_a_blank_owner(monkeypatch, capsys):
    _patch_at(monkeypatch, [{"Date": "2026-09-10", "BD Associate": "",
                             "BD Email": "aditi.saini@enout.in",
                             "Current Stage": "Follow-up (1)"}])
    rows = ml.read_stage_changes()
    assert rows == [], "a row with no owner cannot be attributed"
    out = capsys.readouterr().out
    assert "1 row(s)" in out and "unusable" in out, \
        "an unusable row must be reported — silence is what made this invisible"


def test_read_stage_changes_stays_quiet_when_every_row_is_usable(monkeypatch, capsys):
    _patch_at(monkeypatch, [{"Date": "2026-09-10", "BD Associate": "Aditi saini",
                             "Current Stage": "Follow-up (1)"}])
    ml.read_stage_changes()
    assert "unusable" not in capsys.readouterr().out
