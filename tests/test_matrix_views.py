"""
Company / Contact Matrix — week-by-week views rolled up from the base table.

The risk here is quiet: a week-of-month that disagrees with the weekly digest,
or a month that loses its last days because the table stopped at W4. Most of
these tests are about the calendar, not the arithmetic.

Run: python -m pytest tests/test_matrix_views.py -q
"""
import importlib.util
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("KYLAS_API_KEY", "test:1")
os.environ.setdefault("AIRTABLE_PAT", "test")
os.environ.setdefault("AIRTABLE_BASE_ID", "app_test")


def _mod(name):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", f"{name}.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


mv = _mod("bd_matrix_views")


# ── week of month: the part that can be quietly wrong ────────────────────────

def test_the_first_falls_in_w1_whatever_weekday_it_is():
    for month in range(1, 13):
        assert mv.week_of_month(f"2026-{month:02d}-01") == 1


def test_weeks_follow_calendar_boundaries_not_seven_day_blocks():
    """2026-09-01 is a Tuesday. Sun 06 Sep closes W1; Mon 07 Sep opens W2.
    A naive 'days 1-7 are W1' would put the 7th in W1 and split the calendar
    week across two columns, disagreeing with the weekly digest."""
    assert mv.week_of_month("2026-09-06") == 1     # Sunday, still W1
    assert mv.week_of_month("2026-09-07") == 2     # Monday, new week
    assert mv.week_of_month("2026-09-13") == 2
    assert mv.week_of_month("2026-09-14") == 3


def test_a_month_can_span_five_weeks():
    """Truncating at W4 would drop the end of most months."""
    assert mv.week_of_month("2026-09-30") == 5


def test_week_index_never_exceeds_the_columns_available():
    """Walk every day of several years — no day may fall outside W1..W6."""
    d, end = date(2026, 1, 1), date(2029, 12, 31)
    seen = set()
    while d <= end:
        w = mv.week_of_month(d.isoformat())
        assert 1 <= w <= mv.MAX_WEEKS, f"{d} -> W{w}"
        seen.add(w)
        d += timedelta(days=1)
    assert 5 in seen, "five-week months must occur"


def test_days_in_the_same_calendar_week_share_a_column():
    monday = date(2026, 9, 14)
    assert {mv.week_of_month((monday + timedelta(days=i)).isoformat())
            for i in range(7)} == {3}


def test_week_of_month_agrees_with_the_iso_week_used_elsewhere():
    """Two days share a matrix column exactly when they share an ISO week —
    otherwise 'W3' here and the weekly digest would mean different things."""
    d, end = date(2026, 9, 1), date(2026, 11, 30)
    while d < end:
        nxt = d + timedelta(days=1)
        if d.strftime("%Y-%m") == nxt.strftime("%Y-%m"):
            same_col = mv.week_of_month(d.isoformat()) == mv.week_of_month(nxt.isoformat())
            same_iso = (mv.funnel._iso_week(d.isoformat())
                        == mv.funnel._iso_week(nxt.isoformat()))
            assert same_col == same_iso, f"{d} vs {nxt}"
        d = nxt


# ── rolling up ───────────────────────────────────────────────────────────────

def _base(*specs):
    """specs: (day, group, metric, value) as stored in BD Metrics Daily."""
    return {("Anjali Athya", "a@enout.in", day, grp, metric): v
            for day, grp, metric, v in specs}


def test_values_land_in_the_right_week_columns_and_total():
    cells = mv.build_matrix(_base(
        ("2026-09-02", "Contact", "Call Attempted", 12),   # W1
        ("2026-09-08", "Contact", "Call Attempted", 19),   # W2
        ("2026-09-16", "Contact", "Call Attempted", 8),    # W3
    ), "Contact")
    row = cells[("Anjali Athya", "a@enout.in", "2026-09", "Call Attempted")]
    assert row["W1"] == 12 and row["W2"] == 19 and row["W3"] == 8
    assert row[mv.TOTAL_COL] == 39


def test_same_week_values_accumulate():
    cells = mv.build_matrix(_base(
        ("2026-09-07", "Contact", "SQL", 1),
        ("2026-09-09", "Contact", "SQL", 2),
    ), "Contact")
    row = cells[("Anjali Athya", "a@enout.in", "2026-09", "SQL")]
    assert row["W2"] == 3 and row[mv.TOTAL_COL] == 3


def test_months_stay_separate_so_history_is_not_merged():
    cells = mv.build_matrix(_base(
        ("2026-08-31", "Contact", "SQL", 5),
        ("2026-09-01", "Contact", "SQL", 7),
    ), "Contact")
    assert cells[("Anjali Athya", "a@enout.in", "2026-08", "SQL")][mv.TOTAL_COL] == 5
    assert cells[("Anjali Athya", "a@enout.in", "2026-09", "SQL")][mv.TOTAL_COL] == 7


def test_each_view_takes_only_its_own_metric_group():
    rows = _base(("2026-09-02", "Contact", "Call Attempted", 1),
                 ("2026-09-02", "Company", "Companies Worked", 2))
    assert list(mv.build_matrix(rows, "Contact")) == [
        ("Anjali Athya", "a@enout.in", "2026-09", "Call Attempted")]
    assert list(mv.build_matrix(rows, "Company")) == [
        ("Anjali Athya", "a@enout.in", "2026-09", "Companies Worked")]


def test_unknown_metrics_are_ignored_not_charted():
    assert mv.build_matrix(
        _base(("2026-09-02", "Contact", "Something Invented", 9)), "Contact") == {}


def test_month_filter_selects_one_month():
    rows = _base(("2026-08-10", "Contact", "SQL", 1),
                 ("2026-09-10", "Contact", "SQL", 2))
    cells = mv.build_matrix(rows, "Contact", month="2026-09")
    assert {k[2] for k in cells} == {"2026-09"}


def test_a_malformed_date_is_skipped_not_fatal():
    assert mv.build_matrix(
        _base(("not-a-date", "Contact", "SQL", 1)), "Contact") == {}


def test_absent_weeks_stay_absent_so_blank_is_not_shown_as_zero():
    """A four-week month must leave W5/W6 empty rather than claiming 0 —
    'no week' and 'a week with no activity' are different statements."""
    cells = mv.build_matrix(_base(("2026-09-02", "Contact", "SQL", 1)), "Contact")
    row = cells[("Anjali Athya", "a@enout.in", "2026-09", "SQL")]
    assert "W1" in row and "W5" not in row and "W6" not in row


def test_both_views_are_configured_over_the_two_metric_groups():
    assert set(mv.VIEWS) == {"Contact", "Company"}
    assert mv.VIEWS["Contact"] == "Contact Matrix"
    assert mv.VIEWS["Company"] == "Company Matrix"
    assert mv.METRICS["Contact"] and mv.METRICS["Company"]


def test_a_closed_month_is_frozen_and_never_rewritten(monkeypatch):
    """The 'finalise the month' guarantee."""
    pushed, frozen = [], []

    class _FakeAT:
        def __init__(self, *a, **k):
            # An existing row for the closed month, none for the open one.
            self._cache = {"Anjali Athya | 2026-08 | SQL": {"id": "rec1", "fields": {}}}
        def build_cache(self, key_field): return len(self._cache)
        def upsert(self, key_field, key, fields, stamp, updated_at_field=""):
            pushed.append(key); return "updated", "rec"
        def flush(self): pass

    import utils.airtable_client as ac
    monkeypatch.setattr(ac, "AirtableClient", _FakeAT)
    monkeypatch.setattr(mv, "_ensure", lambda *a, **k: True)

    cells = mv.build_matrix(_base(("2026-08-10", "Contact", "SQL", 1),
                                  ("2026-09-10", "Contact", "SQL", 2)), "Contact")
    tally = mv.push_matrix(cells, "Contact Matrix", today="2026-09-15")

    assert tally["frozen"] == 1, "August is over and already written — freeze it"
    assert pushed == ["Anjali Athya | 2026-09 | SQL"], "only the open month is written"
