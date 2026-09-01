"""
Tests for the BD Company Funnel.

Two classes of risk here:
  1. The rank BOUNDARIES. The funnel is defined by "rank <= 12" and "rank <= 3",
     which is only correct while rank 12 is Activation and rank 3 is Closing
     Loops - Low Value. Reordering account_pipeline_order.json would silently
     change what every column means, so those boundaries are pinned.
  2. Counting COMPANIES, not contacts. Six calls into one account is one
     company, taken at its best stage.
"""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "bd_funnel", os.path.join(_ROOT, "scripts", "bd_company_funnel.py"))
fn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fn)

from utils.account_pipeline import load_order  # noqa: E402

ORDER = load_order()


# ── the boundaries the column definitions rest on ────────────────────────────

def test_requirements_boundary_is_activation():
    """rank <= 12 must mean "Activation or better" — the agreed definition."""
    assert ORDER.label_by_rank[fn.REQUIREMENTS_MAX_RANK] == "Activation"
    assert ORDER.rank_of("MQL (Marketing Qualified Lead)") <= fn.REQUIREMENTS_MAX_RANK
    assert ORDER.rank_of("Not Interested") > fn.REQUIREMENTS_MAX_RANK


def test_handoff_boundary_is_closing_loops_low_value():
    assert ORDER.label_by_rank[fn.HANDOFF_MAX_RANK] == "Closing Loops - Low Value"
    for s in ("SQL (Sales Qualified Lead)",
              "Discovery Call Done - Awaiting Client Inputs",
              "Closing Loops - Low Value"):
        assert ORDER.rank_of(s) <= fn.HANDOFF_MAX_RANK
    assert ORDER.rank_of("Reschedule Pending") > fn.HANDOFF_MAX_RANK


def test_the_twelve_requirements_stages_are_exactly_as_specified():
    expected = [
        "SQL (Sales Qualified Lead)",
        "Discovery Call Done - Awaiting Client Inputs",
        "Closing Loops - Low Value",
        "Reschedule Pending",
        "Discovery Call No-Show",
        "Discovery Call Booked",
        "Follow-up (1)",
        "Follow-up (2)",
        "Follow-up (3)",
        "Followup - CNC",
        "MQL (Marketing Qualified Lead)",
        "Activation",
    ]
    assert [ORDER.label_by_rank[i] for i in range(1, 13)] == expected


def test_sql_and_rejected_stages_resolve():
    assert ORDER.rank_of(fn.SQL_STAGE) == 1
    assert ORDER.rank_of(fn.REJECTED_STAGE) == 3


# ── month parsing ────────────────────────────────────────────────────────────

def test_last_called_parses_both_kylas_formats():
    assert fn._parse_lc("2026-07-14T09:00:00Z") == "2026-07-14"
    assert fn._parse_lc("Jun 22, 2026 at 05:22 PM") == "2026-06-22"
    assert fn._parse_lc("") == ""
    assert fn._parse_lc(None) == ""


# ── the counting itself ──────────────────────────────────────────────────────

def _grid_from(rows):
    """rows: [(company_id, stage)] → the counts one rep/month cell would get."""
    cnc = {ORDER.rank_of(s) for s in fn.NOT_REACHED_STAGES} - {0}
    best = {}
    for cid, stage in rows:
        r = ORDER.rank_of(stage)
        if not r:
            continue
        if cid not in best or r < best[cid]:
            best[cid] = r
    ranks = list(best.values())
    return {
        "Companies Worked":    len(ranks),
        "Companies Reached":   sum(1 for r in ranks if r not in cnc),
        "Requirements Stated": sum(1 for r in ranks if r <= fn.REQUIREMENTS_MAX_RANK),
        "Handoff Calls Held":  sum(1 for r in ranks if r <= fn.HANDOFF_MAX_RANK),
        "SQLs Accepted":       sum(1 for r in ranks if r == ORDER.rank_of(fn.SQL_STAGE)),
        "SQLs Rejected":       sum(1 for r in ranks if r == ORDER.rank_of(fn.REJECTED_STAGE)),
    }


def test_many_contacts_at_one_company_count_once_at_the_best_stage():
    g = _grid_from([("1", "CNC (Could Not Connect) - 1"),
                    ("1", "MQL (Marketing Qualified Lead)"),
                    ("1", "Follow-up (2)")])
    assert g["Companies Worked"] == 1
    assert g["Companies Reached"] == 1        # best is MQL, not CNC
    assert g["Requirements Stated"] == 1


def test_an_account_stuck_at_cnc_is_worked_but_not_reached():
    g = _grid_from([("1", "CNC (Could Not Connect) - 2")])
    assert g["Companies Worked"] == 1
    assert g["Companies Reached"] == 0
    assert g["Requirements Stated"] == 0


def test_sql_account_counts_all_the_way_down_the_funnel():
    g = _grid_from([("1", "SQL (Sales Qualified Lead)")])
    assert g == {"Companies Worked": 1, "Companies Reached": 1,
                 "Requirements Stated": 1, "Handoff Calls Held": 1,
                 "SQLs Accepted": 1, "SQLs Rejected": 0}


def test_closing_loops_is_a_rejected_handoff_not_an_accepted_one():
    g = _grid_from([("1", "Closing Loops - Low Value")])
    assert g["Handoff Calls Held"] == 1
    assert g["SQLs Rejected"] == 1
    assert g["SQLs Accepted"] == 0


def test_yet_to_be_mined_account_is_worked_but_goes_no_further():
    g = _grid_from([("1", "Yet to Be Mined")])
    assert g["Companies Worked"] == 1
    assert g["Companies Reached"] == 1   # not a CNC stage
    assert g["Requirements Stated"] == 0


def test_followup_cnc_counts_as_reached():
    """It is rank 10, inside the Requirements Stated band, so treating it as
    not-reached would put Requirements Stated above Companies Reached."""
    g = _grid_from([("1", "Followup - CNC")])
    assert g["Companies Reached"] == 1
    assert g["Requirements Stated"] == 1
    assert "Followup - CNC" not in fn.NOT_REACHED_STAGES


def test_only_the_three_hard_cnc_stages_are_not_reached():
    assert fn.NOT_REACHED_STAGES == {
        "CNC (Could Not Connect) - 1",
        "CNC (Could Not Connect) - 2",
        "CNC (Could Not Connect) - 3",
    }
    for s in fn.NOT_REACHED_STAGES:
        assert _grid_from([("1", s)])["Companies Reached"] == 0


def test_funnel_nests_at_every_level():
    """Each column must be <= the one before it, for any mix of stages."""
    g = _grid_from([("1", "SQL (Sales Qualified Lead)"),
                    ("2", "MQL (Marketing Qualified Lead)"),
                    ("3", "CNC (Could Not Connect) - 1"),
                    ("4", "Followup - CNC"),
                    ("5", "Not Interested"),
                    ("6", "Yet to Be Mined"),
                    ("7", "Closing Loops - Low Value")])
    assert (g["Companies Worked"] >= g["Companies Reached"]
            >= g["Requirements Stated"] >= g["Handoff Calls Held"]
            >= g["SQLs Accepted"])
    assert g["Handoff Calls Held"] >= g["SQLs Accepted"] + g["SQLs Rejected"]


# ── daily grain ──────────────────────────────────────────────────────────────

def test_iso_week_labels_group_correctly():
    """Week labels must be sortable text and agree across a month boundary."""
    assert fn._iso_week("2026-09-01") == fn._iso_week("2026-08-31"), \
        "31 Aug and 1 Sep 2026 fall in the same ISO week"
    assert fn._iso_week("2026-01-05") == "2026-W02"
    assert fn._iso_week("") == ""
    assert fn._iso_week("not-a-date") == ""


def test_counts_helper_matches_the_column_definitions():
    o = ORDER
    r = [o.rank_of("SQL (Sales Qualified Lead)"),
         o.rank_of("Closing Loops - Low Value"),
         o.rank_of("CNC (Could Not Connect) - 1"),
         o.rank_of("Yet to Be Mined")]
    c = fn._counts(r, o)
    assert c["Companies Worked"] == 4
    assert c["Companies Reached"] == 3          # the CNC one drops out
    assert c["Requirements Stated"] == 2        # SQL + Closing Loops
    assert c["Handoff Calls Held"] == 2
    assert c["SQLs Accepted"] == 1
    assert c["SQLs Rejected"] == 1


def test_daily_rows_sum_higher_than_the_monthly_row():
    """The documented, intentional disagreement: one account worked on two days
    is 1 for the month but 2 across the daily rows. If these ever match, the
    two grains have been wrongly collapsed into one."""
    o = ORDER
    # same company, two different days, both in the same month
    month_ranks = [o.rank_of("Follow-up (1)")]                  # distinct → 1
    day_ranks   = [[o.rank_of("Follow-up (1)")],
                   [o.rank_of("Follow-up (1)")]]                # 1 per day → 2
    monthly = fn._counts(month_ranks, o)["Companies Worked"]
    daily   = sum(fn._counts(d, o)["Companies Worked"] for d in day_ranks)
    assert monthly == 1 and daily == 2
