"""
Tests for the Companies Reached stage breakout.

The point of this script is the reconciliation, so the load-bearing test is
that a MISMATCH is actually reported when the two sides disagree. A check that
can only ever say "match" is worthless.
"""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("AIRTABLE_PAT", "x")
os.environ.setdefault("AIRTABLE_BASE_ID", "app_test")

_spec = importlib.util.spec_from_file_location(
    "funnel_stage_breakout",
    os.path.join(_ROOT, "scripts", "funnel_stage_breakout.py"))
fb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fb)

from utils.account_pipeline import load_order  # noqa: E402

_ORDER = load_order()
_CNC1 = "CNC (Could Not Connect) - 1"
_CNC1_RANK = _ORDER.rank_of(_CNC1)
_SQL_RANK = _ORDER.rank_of("SQL (Sales Qualified Lead)")


def _setup_funnel_module():
    """breakout() reads NOT_REACHED_STAGES off the funnel module it loads."""
    fb._FUNNEL = _load_real_funnel()


def _load_real_funnel():
    path = os.path.join(_ROOT, "scripts", "bd_company_funnel.py")
    spec = importlib.util.spec_from_file_location("bd_company_funnel", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cnc_stages_are_the_only_ones_not_counted_as_reached():
    """Reached is 'somebody got through' — only the three hard could-not-
    connect stages are excluded, per the funnel's own definition."""
    _setup_funnel_module()
    by_month = {("Aditi", "a@x.in", "2026-07"): {"c1": _SQL_RANK, "c2": _CNC1_RANK}}
    rows = fb.breakout(by_month, _ORDER, {"2026-07"})
    flags = {r["Stage"]: r["Counts As Reached"] for r in rows}
    assert flags[_CNC1] == "no"
    assert flags["SQL (Sales Qualified Lead)"] == "yes"


def test_each_company_appears_once_at_its_best_stage():
    _setup_funnel_module()
    by_month = {("Aditi", "a@x.in", "2026-07"):
                {"c1": _SQL_RANK, "c2": _SQL_RANK, "c3": _CNC1_RANK}}
    rows = fb.breakout(by_month, _ORDER, {"2026-07"})
    assert sum(r["Companies"] for r in rows) == 3
    sql = next(r for r in rows if r["Rank"] == _SQL_RANK)
    assert sql["Companies"] == 2


def test_months_outside_the_requested_set_are_excluded():
    _setup_funnel_module()
    by_month = {("Aditi", "a@x.in", "2026-07"): {"c1": _SQL_RANK},
                ("Aditi", "a@x.in", "2026-08"): {"c2": _SQL_RANK}}
    rows = fb.breakout(by_month, _ORDER, {"2026-07"})
    assert {r["Month"] for r in rows} == {"2026-07"}


def test_rows_come_back_ordered_best_stage_first():
    _setup_funnel_module()
    by_month = {("Aditi", "a@x.in", "2026-07"): {"c1": _CNC1_RANK, "c2": _SQL_RANK}}
    rows = fb.breakout(by_month, _ORDER, {"2026-07"})
    assert [r["Rank"] for r in rows] == sorted([r["Rank"] for r in rows])


def test_reconcile_reports_match_when_the_totals_agree():
    _setup_funnel_module()
    rows = [{"Month": "2026-07", "BD Associate": "Aditi", "Rank": _SQL_RANK,
             "Stage": "SQL", "Companies": 3, "Counts As Reached": "yes"},
            {"Month": "2026-07", "BD Associate": "Aditi", "Rank": _CNC1_RANK,
             "Stage": _CNC1, "Companies": 2, "Counts As Reached": "no"}]
    ref = {("Aditi", "2026-07"): {"Companies Worked": 5, "Companies Reached": 3}}
    out = fb.reconcile(rows, ref, {"2026-07"})
    assert out[0]["Match"] == "match"
    assert out[0]["Reached (stage breakout)"] == 3


def test_reconcile_reports_mismatch_when_they_disagree():
    """The load-bearing one. If this cannot fail, the reconciliation is
    decoration."""
    _setup_funnel_module()
    rows = [{"Month": "2026-07", "BD Associate": "Aditi", "Rank": _SQL_RANK,
             "Stage": "SQL", "Companies": 3, "Counts As Reached": "yes"}]
    ref = {("Aditi", "2026-07"): {"Companies Worked": 3, "Companies Reached": 99}}
    out = fb.reconcile(rows, ref, {"2026-07"})
    assert out[0]["Match"] == "MISMATCH"


def test_a_rep_in_the_funnel_but_absent_from_the_breakout_is_flagged():
    """Silently reporting nothing for a rep the funnel has a row for would
    hide exactly the gap this is meant to surface."""
    _setup_funnel_module()
    ref = {("Ghost", "2026-07"): {"Companies Worked": 4, "Companies Reached": 4}}
    out = fb.reconcile([], ref, {"2026-07"})
    assert out[0]["Match"] == "NO FUNNEL ROW" or out[0]["Match"] == "MISMATCH"
    assert out[0]["BD Associate"] == "Ghost"


def test_reached_plus_not_reached_equals_worked():
    _setup_funnel_module()
    by_month = {("Aditi", "a@x.in", "2026-07"):
                {f"c{i}": _SQL_RANK for i in range(4)} | {"x": _CNC1_RANK}}
    rows = fb.breakout(by_month, _ORDER, {"2026-07"})
    worked = sum(r["Companies"] for r in rows)
    reached = sum(r["Companies"] for r in rows if r["Counts As Reached"] == "yes")
    not_reached = sum(r["Companies"] for r in rows if r["Counts As Reached"] == "no")
    assert reached + not_reached == worked == 5


# ── --combine: a period is not the sum of its months ────────────────────────

def test_a_company_worked_in_both_months_is_counted_once():
    """The whole reason --combine exists. Adding the monthly rows would report
    this company twice — 'company-months', not companies."""
    _setup_funnel_module()
    by_month = {("Aditi", "a@x.in", "2026-07"): {"c1": _SQL_RANK},
                ("Aditi", "a@x.in", "2026-08"): {"c1": _CNC1_RANK}}
    merged = fb.combine_months(by_month, {"2026-07", "2026-08"}, "JulAug")
    rows = fb.breakout(merged, _ORDER, {"JulAug"})
    assert sum(r["Companies"] for r in rows) == 1


def test_the_best_stage_across_the_period_wins():
    """CNC in July, SQL in August — the company reached SQL in the period, and
    must not also appear as a CNC row."""
    _setup_funnel_module()
    by_month = {("Aditi", "a@x.in", "2026-07"): {"c1": _CNC1_RANK},
                ("Aditi", "a@x.in", "2026-08"): {"c1": _SQL_RANK}}
    merged = fb.combine_months(by_month, {"2026-07", "2026-08"}, "JulAug")
    rows = fb.breakout(merged, _ORDER, {"JulAug"})
    assert len(rows) == 1
    assert rows[0]["Rank"] == _SQL_RANK
    assert rows[0]["Counts As Reached"] == "yes"


def test_distinct_companies_stay_distinct():
    _setup_funnel_module()
    by_month = {("Aditi", "a@x.in", "2026-07"): {"c1": _SQL_RANK},
                ("Aditi", "a@x.in", "2026-08"): {"c2": _SQL_RANK}}
    merged = fb.combine_months(by_month, {"2026-07", "2026-08"}, "JulAug")
    rows = fb.breakout(merged, _ORDER, {"JulAug"})
    assert sum(r["Companies"] for r in rows) == 2


def test_combine_keeps_reps_separate():
    """Two reps working the same company each keep it — the funnel attributes
    per rep, so merging them would silently reassign work."""
    _setup_funnel_module()
    by_month = {("Aditi", "a@x.in", "2026-07"): {"c1": _SQL_RANK},
                ("Gurnoor", "g@x.in", "2026-08"): {"c1": _SQL_RANK}}
    merged = fb.combine_months(by_month, {"2026-07", "2026-08"}, "JulAug")
    assert len(merged) == 2
    rows = fb.breakout(merged, _ORDER, {"JulAug"})
    assert sum(r["Companies"] for r in rows) == 2


def test_combine_ignores_months_outside_the_period():
    _setup_funnel_module()
    by_month = {("Aditi", "a@x.in", "2026-06"): {"c9": _SQL_RANK},
                ("Aditi", "a@x.in", "2026-07"): {"c1": _SQL_RANK}}
    merged = fb.combine_months(by_month, {"2026-07"}, "JulOnly")
    rows = fb.breakout(merged, _ORDER, {"JulOnly"})
    assert sum(r["Companies"] for r in rows) == 1
