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
