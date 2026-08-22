"""Unit tests for the EOD call-summary note builder.

Run: python -m pytest tests/test_eod_call_notes.py -q
"""
import os
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("KYLAS_API_KEY", "test:1")

from scripts.eod_call_notes import (  # noqa: E402
    MARKER, _fmt_duration, _ist_day_bounds, _parse_utc, already_noted,
    build_note,
)
from utils.kylas_client import KylasClient  # noqa: E402

DAY = date(2026, 8, 21)


def _call(start, who, outcome="connected", duration=None):
    return {"startTime": start, "outcome": outcome, "duration": duration,
            "createdBy": {"id": 1, "name": who}}


# ------------------------------------------------------------ the IST window

def test_ist_day_starts_at_1830_utc_the_day_before():
    # IST is UTC+5:30, so an IST day begins at 18:30 UTC the previous day.
    start, end = _ist_day_bounds(DAY)
    assert start == datetime(2026, 8, 20, 18, 30, tzinfo=timezone.utc)
    assert end == datetime(2026, 8, 21, 18, 30, tzinfo=timezone.utc)


def test_a_late_evening_ist_call_lands_on_the_right_day():
    # 20:00 UTC on the 21st is 1:30 am IST on the 22nd -- NOT the 21st.
    start, end = _ist_day_bounds(DAY)
    late = _parse_utc("2026-08-21T20:00:49.000Z")
    assert not (start <= late < end)
    s22, e22 = _ist_day_bounds(date(2026, 8, 22))
    assert s22 <= late < e22


# ------------------------------------------------------------- the note body

def test_note_lists_each_call_with_rep_time_outcome_duration():
    body = build_note([
        _call("2026-08-21T04:56:48.000Z", "Hrithik Rawat", "connected", 362),
        _call("2026-08-21T07:07:47.000Z", "Mayra Singh", "no_answer"),
    ], DAY)
    assert "Call log — 21 Aug 2026" in body
    assert "• Hrithik Rawat — 10:26 AM — connected — 6m02s" in body
    assert "• Mayra Singh — 12:37 PM — no answer" in body


def test_note_summary_counts_connected_and_talk_time():
    body = build_note([
        _call("2026-08-21T04:56:48.000Z", "A", "connected", 362),
        _call("2026-08-21T05:00:00.000Z", "B", "connected", 5),
        _call("2026-08-21T05:10:00.000Z", "C", "no_answer"),
    ], DAY)
    assert "3 calls · 2 connected · 6m07s talk time" in body


def test_single_call_is_not_pluralised():
    body = build_note([_call("2026-08-21T04:56:48.000Z", "A", "connected", 60)], DAY)
    assert "1 call · 1 connected" in body


def test_calls_are_ordered_by_time():
    body = build_note([
        _call("2026-08-21T09:00:00.000Z", "Later"),
        _call("2026-08-21T04:00:00.000Z", "Earlier"),
    ], DAY)
    assert body.index("Earlier") < body.index("Later")


def test_unconnected_call_shows_no_duration():
    body = build_note([_call("2026-08-21T04:00:00.000Z", "A", "no_answer")], DAY)
    assert "no answer" in body and "None" not in body and "0s" not in body


def test_missing_rep_name_falls_back_to_unknown():
    body = build_note([{"startTime": "2026-08-21T04:00:00.000Z",
                        "outcome": "connected", "createdBy": {}}], DAY)
    assert "• unknown —" in body


def test_note_carries_the_dated_marker():
    body = build_note([_call("2026-08-21T04:00:00.000Z", "A")], DAY)
    assert body.rstrip().endswith(MARKER.format(date="2026-08-21"))


# ------------------------------------------------------------- idempotency

class _NotesClient:
    def __init__(self, notes, boom=False):
        self._notes, self._boom = notes, boom

    def get_all_notes(self, max_pages=20):
        if self._boom:
            raise RuntimeError("429")
        return self._notes


def test_already_noted_finds_deals_carrying_todays_marker():
    marker = MARKER.format(date="2026-08-21")
    client = _NotesClient([
        {"description": f"Call log<br>{marker}",
         "relations": [{"entityType": "DEAL", "entityId": 4383813}]},
        {"description": "unrelated note",
         "relations": [{"entityType": "DEAL", "entityId": 999}]},
    ])
    assert already_noted(client, DAY, 20) == {4383813}


def test_yesterdays_marker_does_not_count_as_done():
    client = _NotesClient([
        {"description": MARKER.format(date="2026-08-20"),
         "relations": [{"entityType": "DEAL", "entityId": 4383813}]},
    ])
    assert already_noted(client, DAY, 20) == set()


def test_marker_on_a_contact_relation_is_ignored():
    client = _NotesClient([
        {"description": MARKER.format(date="2026-08-21"),
         "relations": [{"entityType": "CONTACT", "entityId": 5362056}]},
    ])
    assert already_noted(client, DAY, 20) == set()


def test_unreadable_notes_returns_none_so_the_caller_can_refuse_to_write():
    # Writing blind would double-post, so this must be distinguishable
    # from "nothing is marked yet".
    assert already_noted(_NotesClient([], boom=True), DAY, 20) is None


# ------------------------------------------------- grouping calls to deals
#
# The /call-logs endpoint ignores entityId/entityType and returns the whole
# tenant's call logs for any entity asked for. A dry run over 60 deals got the
# same two call logs for all 60, and those two belong to one deal -- live that
# would have posted an identical wrong note to every deal. Grouping therefore
# has to come from each record's own relatedTo, which is what these pin.

_CALL_ON_4383813 = {
    "id": 43843294,
    "relatedTo": [{"id": 5362056, "entity": "contact"},
                  {"id": 4383813, "entity": "deal"}],
    "associatedTo": [{"id": 5362056, "entity": "contact"}],
}


def test_relations_finds_the_deal_the_call_actually_names():
    assert KylasClient.call_log_relations(_CALL_ON_4383813, "deal") == {4383813}


def test_relations_does_not_attribute_a_call_to_an_unrelated_deal():
    assert 4676048 not in KylasClient.call_log_relations(_CALL_ON_4383813, "deal")


def test_relations_reads_contacts_from_both_lists():
    assert KylasClient.call_log_relations(_CALL_ON_4383813, "contact") == {5362056}


def test_relations_of_a_call_with_no_links_is_empty():
    assert KylasClient.call_log_relations({}, "deal") == set()


def test_relations_survives_malformed_entries():
    call = {"relatedTo": [{"entity": "deal"}, {"id": "x", "entity": "deal"},
                          None, {"id": 7, "entity": "deal"}]}
    assert KylasClient.call_log_relations(call, "deal") == {7}


def test_get_call_logs_drops_rows_the_server_wrongly_returned():
    # The server hands back everything; the client must keep only the asked-for
    # deal's own calls.
    other = {"id": 1, "relatedTo": [{"id": 9999, "entity": "deal"}]}
    c = KylasClient()
    c._get = lambda path, params=None: {"content": [_CALL_ON_4383813, other]}
    rows = c.get_call_logs(4383813, "deal")
    assert [r["id"] for r in rows] == [43843294]


@pytest.mark.parametrize("secs,expected", [
    (5, "5s"), (60, "1m00s"), (362, "6m02s"), (367, "6m07s")])
def test_duration_formatting(secs, expected):
    assert _fmt_duration(secs) == expected


# --------------------------------------------- the tenant-wide sweep guard
#
# /call-logs needs entityId+entityType to answer but ignores them when
# filtering, so one sweep returns everything. That is only safe while it holds:
# if Kylas fixes the filter, a single sweep becomes one deal's calls and an EOD
# run would silently skip every other deal. get_all_call_logs proves which
# behaviour is live by comparing PAGE 0 for two seeds -- page 0 is enough to
# show the property, and sweeping twice would double a dozens-of-pages read.

class _SweepClient(KylasClient):
    """Fakes the paged endpoint: pages[seed] is a list of pages."""

    def __init__(self, by_seed):
        super().__init__()
        self._by_seed = by_seed
        self.reads = []

    def _call_log_page(self, seed_id, seed_type, page, size=None):
        self.reads.append((int(seed_id), int(page)))
        pages = self._by_seed.get(int(seed_id), [])
        rows = pages[page] if page < len(pages) else []
        return rows, {"totalPages": len(pages),
                      "totalElements": sum(len(p) for p in pages)}


def test_identical_first_pages_means_filter_ignored():
    rows = [{"id": 1}, {"id": 2}]
    c = _SweepClient({10: [list(rows)], 20: [list(rows)]})
    got, ignored = c.get_all_call_logs([10, 20])
    assert ignored is True
    assert {r["id"] for r in got} == {1, 2}


def test_differing_results_means_the_filter_works_and_sweep_is_not_trusted():
    c = _SweepClient({10: [[{"id": 1}]], 20: [[{"id": 2}]]})
    got, ignored = c.get_all_call_logs([10, 20])
    assert ignored is False
    assert {r["id"] for r in got} == {1, 2}


def test_a_single_seed_cannot_prove_completeness():
    c = _SweepClient({10: [[{"id": 1}]]})
    got, ignored = c.get_all_call_logs([10])
    assert ignored is False and len(got) == 1


def test_no_seeds_returns_nothing_rather_than_guessing():
    c = _SweepClient({})
    assert c.get_all_call_logs([]) == ([], False)


def test_proving_the_filter_is_ignored_costs_one_page_per_seed():
    # Not one full sweep per seed: the property shows on page 0.
    c = _SweepClient({10: [[{"id": 1}], [{"id": 3}]], 20: [[{"id": 1}]]})
    c.get_all_call_logs([10, 20])
    assert c.reads.count((20, 0)) == 1
    assert not any(s == 20 and p > 0 for s, p in c.reads)


# ------------------------------------------------------- paging the listing
#
# The listing IS paged: `page` and `size` work and the envelope carries
# totalElements/totalPages. This client claimed otherwise until 2026-08-22,
# after an early probe tried page, size and sort together and blamed all three
# for sort's 404. The cost was not theoretical: the default page size is 10
# against 5,399 call logs, so every summary was built from the ten most recent
# calls in the tenant and was silently incomplete on every date. Deal 4676048
# got no note because its contact's calls sat on page 1 and beyond.

def _row(id_, iso):
    return {"id": id_, "startTime": iso}


def test_the_sweep_reads_past_page_zero():
    c = _SweepClient({10: [[_row(1, "2026-08-21T05:00:00.000Z")],
                           [_row(2, "2026-08-20T05:00:00.000Z")],
                           [_row(3, "2026-08-19T05:00:00.000Z")]]})
    got = c._sweep_call_logs(10, "deal")
    assert [r["id"] for r in got] == [1, 2, 3]


def test_the_sweep_stops_at_the_last_page_rather_than_the_cap():
    c = _SweepClient({10: [[_row(1, "2026-08-21T05:00:00.000Z")]]})
    c._sweep_call_logs(10, "deal")
    assert c.reads == [(10, 0)]


def test_a_descending_sweep_stops_once_it_is_past_the_window():
    day_start, _ = _ist_day_bounds(DAY)
    c = _SweepClient({10: [[_row(1, "2026-08-21T05:00:00.000Z")],
                           [_row(2, "2026-08-19T05:00:00.000Z")],
                           [_row(3, "2026-08-18T05:00:00.000Z")]]})
    got = c._sweep_call_logs(10, "deal", stop_before=day_start)
    # Page 1 is entirely before the window, so page 2 is never fetched.
    assert [r["id"] for r in got] == [1, 2]
    assert (10, 2) not in c.reads


def test_an_unordered_sweep_keeps_paging_instead_of_trusting_the_order():
    # If the rows are not descending, an early exit could drop the day's calls.
    day_start, _ = _ist_day_bounds(DAY)
    c = _SweepClient({10: [[_row(1, "2026-08-18T05:00:00.000Z"),
                            _row(2, "2026-08-21T05:00:00.000Z")],
                           [_row(3, "2026-08-10T05:00:00.000Z")],
                           [_row(4, "2026-08-21T09:00:00.000Z")]]})
    got = c._sweep_call_logs(10, "deal", stop_before=day_start)
    assert [r["id"] for r in got] == [1, 2, 3, 4]


def test_the_window_is_passed_through_from_get_all_call_logs():
    day_start, _ = _ist_day_bounds(DAY)
    c = _SweepClient({10: [[_row(1, "2026-08-21T05:00:00.000Z")],
                           [_row(2, "2026-08-19T05:00:00.000Z")],
                           [_row(3, "2026-08-18T05:00:00.000Z")]],
                      20: [[_row(1, "2026-08-21T05:00:00.000Z")]]})
    got, ignored = c.get_all_call_logs([10, 20], stop_before=day_start)
    assert ignored is True
    assert [r["id"] for r in got] == [1, 2]


def test_the_missing_record_is_found_once_paging_is_real():
    # The exact shape of the reported bug: the record the sweep could not see
    # sits on page 1, and page 0 alone never reaches it.
    c = _SweepClient({10: [[_row(43843303, "2026-08-22T05:00:00.000Z")],
                           [_row(43734306, "2026-08-19T06:47:34.000Z")]],
                      20: [[_row(43843303, "2026-08-22T05:00:00.000Z")]]})
    got, _ = c.get_all_call_logs([10, 20])
    assert 43734306 in {r["id"] for r in got}


def test_get_call_logs_pages_and_then_filters_to_the_asked_for_entity():
    mine = {"id": 7, "startTime": "2026-08-19T06:47:34.000Z",
            "relatedTo": [{"id": 5927888, "entity": "contact"}]}
    other = {"id": 8, "startTime": "2026-08-19T07:00:00.000Z",
             "relatedTo": [{"id": 111, "entity": "contact"}]}
    c = _SweepClient({5927888: [[other], [mine]]})
    assert [r["id"] for r in c.get_call_logs(5927888, "contact")] == [7]


# ------------------------------------------- bridging calls to deals by contact
#
# Reps log calls against the CONTACT, not the deal: of the tenant's ten call
# logs the only two naming a deal were written through the API with an explicit
# relatedTo. So a deal-scoped summary has to bridge contact -> deal itself, or
# it summarises nothing the team actually did.

from scripts.eod_call_notes import (  # noqa: E402
    _is_open, contact_to_deals, deals_for_call,
)

_DEALS = [
    {"id": 100, "pipelineStage": {"name": "Introductory Call"},
     "associatedContacts": [{"id": 1}, {"id": 2}]},
    {"id": 200, "pipelineStage": {"name": "Proposal Sent"},
     "associatedContacts": [{"id": 2}]},
    {"id": 300, "pipelineStage": {"name": "Closed Lost"},
     "associatedContacts": [{"id": 3}]},
]


def _idx():
    return contact_to_deals(_DEALS)


def test_a_call_naming_its_own_deal_is_taken_at_its_word():
    call = {"relatedTo": [{"id": 999, "entity": "deal"}]}
    dids, how = deals_for_call(call, *_idx())
    assert dids == {999} and how == "direct"


def test_a_contact_only_call_reaches_the_contacts_open_deal():
    call = {"relatedTo": [{"id": 1, "entity": "contact"}]}
    dids, how = deals_for_call(call, *_idx())
    assert dids == {100} and how == "via-contact-open"


def test_a_contact_on_two_open_deals_yields_both():
    # Under-reporting a rep's work is worse than the call showing on two deals.
    call = {"relatedTo": [{"id": 2, "entity": "contact"}]}
    dids, how = deals_for_call(call, *_idx())
    assert dids == {100, 200} and how == "via-contact-open"


def test_a_contact_with_only_a_closed_deal_still_gets_the_call():
    # Better on a closed deal than silently dropped.
    call = {"relatedTo": [{"id": 3, "entity": "contact"}]}
    dids, how = deals_for_call(call, *_idx())
    assert dids == {300} and how == "via-contact-closed"


def test_a_call_with_no_usable_link_is_reported_as_unmapped():
    dids, how = deals_for_call({"relatedTo": [{"id": 77, "entity": "contact"}]}, *_idx())
    assert dids == set() and how == "unmapped"


def test_index_separates_open_from_all():
    open_by, all_by = _idx()
    assert sorted(open_by[2]) == [100, 200]
    assert open_by.get(3) in (None, [])
    assert all_by[3] == [300]


def test_index_tolerates_deals_without_contacts_or_ids():
    open_by, all_by = contact_to_deals(
        [{"id": 1}, {"associatedContacts": [{"id": 9}]}, {"id": 2,
         "pipelineStage": {"name": "Open"}, "associatedContacts": [5, {"id": 6}]}])
    assert all_by[5] == [2] and all_by[6] == [2] and 9 not in all_by
