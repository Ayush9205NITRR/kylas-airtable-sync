"""
Unit tests for the Account Pipeline Stage (BD) ranking.

The dangerous failure here is not a crash — it is a stage name that quietly
fails to match and drops the account a rank (or to blank). Most of these tests
are therefore about NAME MATCHING, not arithmetic.

Run: python -m pytest tests/test_account_pipeline.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from utils.account_pipeline import (  # noqa: E402
    _norm,
    _company_id,
    compute_account_pipeline,
    load_order,
)


@pytest.fixture
def order():
    return load_order()


# ── the order itself ─────────────────────────────────────────────────────────

def test_order_is_23_unique_contiguous_ranks(order):
    assert len(order.order) == 23
    assert sorted(order.label_by_rank) == list(range(1, 24))


def test_exact_order_as_specified(order):
    """Pins the business-specified order so a stray edit fails loudly here."""
    assert order.order == [
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
        "Offsite Delayed",
        "Offsite Done (Late Reachout)",
        "Not Interested",
        "Connect Later",
        "CNC (Could Not Connect) - 3",
        "CNC (Could Not Connect) - 2",
        "CNC (Could Not Connect) - 1",
        "Disqualified - Wrong POC",
        "Invalid Contact",
        "Not a Decision Maker (NDM)",
        "POC - Organization - Changed",
    ]


def test_no_show_outranks_booked_and_activation_outranks_offsite_delayed(order):
    """The two swaps the user made against their first draft."""
    assert order.rank_of("Discovery Call No-Show") < order.rank_of("Discovery Call Booked")
    assert order.rank_of("Activation") < order.rank_of("Offsite Delayed")


# ── normalization: the actual risk ───────────────────────────────────────────

@pytest.mark.parametrize("variant", [
    "Closing Loops - Low Value",
    "Closing Loops – Low Value",     # en dash, as Kylas often stores it
    "Closing Loops — Low Value",     # em dash
    "closing loops-low value",       # no spaces, lowercased
    "  Closing Loops  -  Low Value ",
])
def test_dash_and_spacing_variants_all_rank_the_same(order, variant):
    assert order.rank_of(variant) == 3


def test_british_and_american_organisation_agree(order):
    """bd_metrics' static map says 'Organisation'; the live picklist says 'Organization'."""
    assert order.rank_of("POC - Organisation - Changed") == 23
    assert order.rank_of("POC - Organization - Changed") == 23


def test_offsite_done_without_suffix_aliases_to_late_reachout(order):
    """Both spellings exist in the codebase; they must not split into two ranks."""
    assert order.rank_of("Offsite Done") == order.rank_of("Offsite Done (Late Reachout)") == 14


def test_misspelling_alias_is_tolerated(order):
    assert order.rank_of("Offsite Dealyed") == order.rank_of("Offsite Delayed") == 13


def test_norm_is_idempotent():
    for s in ["Closing Loops – Low Value", "Follow-up (1)", "CNC (Could Not Connect) - 3"]:
        assert _norm(_norm(s)) == _norm(s)


def test_follow_up_hyphen_is_not_eaten(order):
    """'Follow-up (1)' must stay distinct from 'Followup - CNC'."""
    assert order.rank_of("Follow-up (1)") == 7
    assert order.rank_of("Followup - CNC") == 10


# ── unranked vs unknown ──────────────────────────────────────────────────────

def test_yet_to_be_mined_is_unranked_and_silent(order):
    assert order.rank_of("Yet to Be Mined") == 0
    assert order.unknown_seen == {}, "a deliberately-unranked stage must not warn"


def test_unknown_stage_is_recorded_for_reporting(order):
    assert order.rank_of("Some Brand New Stage") == 0
    assert "some brand new stage" in order.unknown_seen


def test_blank_stage_ranks_zero_and_is_silent(order):
    for blank in ["", None, "   "]:
        assert order.rank_of(blank) == 0
    assert order.unknown_seen == {}


# ── best-of selection ────────────────────────────────────────────────────────

def test_best_picks_the_highest_ranked_contact(order):
    label, rank = order.best(["MQL (Marketing Qualified Lead)",
                              "SQL (Sales Qualified Lead)",
                              "Not Interested"])
    assert (label, rank) == ("SQL (Sales Qualified Lead)", 1)


def test_best_ignores_unranked_even_when_listed_first(order):
    label, rank = order.best(["Yet to Be Mined", "Invalid Contact"])
    assert (label, rank) == ("Invalid Contact", 21)


def test_best_of_nothing_is_blank(order):
    assert order.best([]) == ("", 0)
    assert order.best(["Yet to Be Mined", "Yet to Be Mined"]) == ("", 0)


def test_a_single_good_contact_beats_many_bad_ones(order):
    """One SQL contact should lift the whole account, not be outvoted."""
    stages = ["Invalid Contact"] * 20 + ["SQL (Sales Qualified Lead)"]
    assert order.best(stages)[1] == 1


# ── company id extraction ────────────────────────────────────────────────────

def test_company_id_handles_int_dict_and_missing():
    assert _company_id({"company": 123}) == "123"          # search results
    assert _company_id({"company": {"id": 456}}) == "456"  # detail reads
    assert _company_id({"company": None}) == ""
    assert _company_id({}) == ""


# ── end to end over contacts ─────────────────────────────────────────────────

def test_compute_groups_by_company_and_takes_the_best(order):
    contacts = [
        {"company": 1, "s": "MQL (Marketing Qualified Lead)"},
        {"company": 1, "s": "Discovery Call Booked"},
        {"company": 2, "s": "Yet to Be Mined"},
        {"company": 3, "s": "Closing Loops – Low Value"},   # en dash
        {"company": None, "s": "SQL (Sales Qualified Lead)"},  # dropped: no company
    ]
    out = compute_account_pipeline(contacts, order=order, stage_of=lambda c: c["s"])

    assert out["1"] == {"stage": "Discovery Call Booked", "rank": 6}
    assert out["2"] == {"stage": "", "rank": 0}
    assert out["3"] == {"stage": "Closing Loops - Low Value", "rank": 3}
    assert set(out) == {"1", "2", "3"}


def test_compute_is_order_independent(order):
    """Contact iteration order must not change the answer."""
    a = [{"company": 9, "s": "SQL (Sales Qualified Lead)"},
         {"company": 9, "s": "Not Interested"}]
    b = list(reversed(a))
    f = lambda c: c["s"]  # noqa: E731
    assert (compute_account_pipeline(a, order=order, stage_of=f) ==
            compute_account_pipeline(b, order=order, stage_of=f))


def test_every_ranked_stage_round_trips(order):
    """Each configured label must rank to itself — catches a bad alias target."""
    for rank, label in order.label_by_rank.items():
        assert order.rank_of(label) == rank
        assert order.best([label]) == (label, rank)
