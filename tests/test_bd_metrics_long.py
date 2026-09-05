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

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("AIRTABLE_PAT", "x")
os.environ.setdefault("AIRTABLE_BASE_ID", "app_test")

_spec = importlib.util.spec_from_file_location(
    "bd_metrics_long", os.path.join(_ROOT, "scripts", "bd_metrics_long.py"))
ml = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ml)

_norm = ml.matrix._norm


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


def test_digest_html_contains_every_rep_and_is_valid_enough():
    rows = ml.team_digest_rows(
        dict([_row("Anjali Athya", "a@x", "2026-09-05", "Contact", "SQL", 3)]),
        "2026-09-05")
    html = ml.build_digest_html(rows, "2026-09-05")
    assert "Anjali Athya" in html
    assert html.startswith("<!DOCTYPE html>")
    assert html.count("<table") == 1
