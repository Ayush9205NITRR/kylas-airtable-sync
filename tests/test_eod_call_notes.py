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
# filtering, so one read returns everything. That is only safe while it holds:
# if Kylas fixes the filter, one read becomes one deal's calls and an EOD run
# would silently skip every other deal. get_all_call_logs proves which
# behaviour is live by comparing a small read for two seeds.

class _SweepClient(KylasClient):
    """
    Fakes /call-logs: `by_seed[seed]` is the full row list for that seed, and a
    request for `size=N` returns the first N of it -- which is what the real
    endpoint does, newest first.
    """

    def __init__(self, by_seed, total=None):
        super().__init__()
        self._by_seed = by_seed
        self._total = total
        self.reads = []

    def _call_log_page(self, seed_id, seed_type, size):
        self.reads.append((int(seed_id), int(size)))
        rows = self._by_seed.get(int(seed_id), [])
        total = self._total if self._total is not None else len(rows)
        return rows[:size], {"totalElements": total}


def _row(id_, iso):
    return {"id": id_, "startTime": iso}


def test_identical_reads_from_two_seeds_means_filter_ignored():
    rows = [_row(1, "2026-08-21T05:00:00.000Z")]
    c = _SweepClient({10: list(rows), 20: list(rows)})
    got, usable = c.get_all_call_logs([10, 20])
    assert usable is True
    assert {r["id"] for r in got} == {1}


def test_differing_reads_mean_the_filter_works_and_the_read_is_not_trusted():
    c = _SweepClient({10: [_row(1, "2026-08-21T05:00:00.000Z")],
                      20: [_row(2, "2026-08-21T05:00:00.000Z")]})
    got, usable = c.get_all_call_logs([10, 20])
    assert usable is False
    assert {r["id"] for r in got} == {1, 2}


def test_a_single_seed_cannot_prove_the_read_is_tenant_wide():
    c = _SweepClient({10: [_row(1, "2026-08-21T05:00:00.000Z")]})
    got, usable = c.get_all_call_logs([10])
    assert usable is False and len(got) == 1


def test_no_seeds_returns_nothing_rather_than_guessing():
    assert _SweepClient({}).get_all_call_logs([]) == ([], False)


# ------------------------------------------------------ reaching far enough
#
# The default response is TEN rows against this tenant's 5,399, and reading
# that and calling it the tenant is the whole bug: deal 4676048 got no note
# because its contact's 19 and 20 Aug calls sat outside the newest ten, while
# GET /call-logs/43734306 read the record back by its own id without trouble.
#
# `page` and `size` each work but 404 together, and `sort` 404s alone -- the
# original "no paging exists" claim came from sending all three at once. So the
# read is one sized request, escalated until it PROVES it covered the day.

def test_the_read_escalates_until_it_reaches_past_the_window():
    day_start, _ = _ist_day_bounds(DAY)
    rows = ([_row(i, "2026-08-21T05:00:00.000Z") for i in range(100)]
            + [_row(999, "2026-08-19T05:00:00.000Z")])
    c = _SweepClient({10: rows}, total=5399)
    got, covered = c._sweep_call_logs(10, "deal", day_start)
    assert covered is True
    assert 999 in {r["id"] for r in got}
    assert [sz for _, sz in c.reads] == [100, 500]


def test_a_read_that_never_reaches_the_window_is_reported_as_not_covered():
    # Every row is inside the day and the server claims far more exist, so the
    # read cannot show it saw the whole day. Summarising here would invent a
    # quiet day out of a truncated response.
    day_start, _ = _ist_day_bounds(DAY)
    rows = [_row(i, "2026-08-21T05:00:00.000Z") for i in range(2000)]
    c = _SweepClient({10: rows}, total=5399)
    _, covered = c._sweep_call_logs(10, "deal", day_start)
    assert covered is False


def test_one_read_is_enough_when_it_already_spans_the_window():
    day_start, _ = _ist_day_bounds(DAY)
    c = _SweepClient({10: [_row(1, "2026-08-21T05:00:00.000Z"),
                           _row(2, "2026-08-18T05:00:00.000Z")]}, total=5399)
    _, covered = c._sweep_call_logs(10, "deal", day_start)
    assert covered is True
    assert c.reads == [(10, 100)]


def test_having_every_record_counts_as_covered_even_inside_the_window():
    day_start, _ = _ist_day_bounds(DAY)
    c = _SweepClient({10: [_row(1, "2026-08-21T05:00:00.000Z")]}, total=1)
    _, covered = c._sweep_call_logs(10, "deal", day_start)
    assert covered is True


def test_an_unusable_read_is_reported_even_when_the_filter_is_ignored():
    # Both halves of `usable` matter: a tenant-wide read that stops short of
    # the day is just as wrong as a read that is not tenant-wide.
    day_start, _ = _ist_day_bounds(DAY)
    rows = [_row(i, "2026-08-21T05:00:00.000Z") for i in range(2000)]
    c = _SweepClient({10: rows, 20: rows}, total=5399)
    _, usable = c.get_all_call_logs([10, 20], stop_before=day_start)
    assert usable is False


def test_the_missing_record_is_found_once_the_read_is_sized():
    # The reported bug exactly: the record is the 40th newest, so the default
    # ten never reaches it and a sized read does.
    day_start, _ = _ist_day_bounds(date(2026, 8, 19))
    rows = ([_row(43843303 - i, f"2026-08-2{2 - i // 20}T05:00:00.000Z")
             for i in range(39)]
            + [_row(43734306, "2026-08-19T06:47:34.000Z"),
               _row(43700000, "2026-08-17T05:00:00.000Z")])
    c = _SweepClient({10: rows, 20: rows}, total=5399)
    got, usable = c.get_all_call_logs([10, 20], stop_before=day_start)
    assert usable is True
    assert 43734306 in {r["id"] for r in got}


def test_an_empty_read_is_never_called_covered():
    day_start, _ = _ist_day_bounds(DAY)
    c = _SweepClient({10: []}, total=5399)
    got, covered = c._sweep_call_logs(10, "deal", day_start)
    assert got == [] and covered is False


def test_get_call_logs_sizes_the_read_then_filters_to_the_entity():
    mine = {"id": 7, "startTime": "2026-08-19T06:47:34.000Z",
            "relatedTo": [{"id": 5927888, "entity": "contact"}]}
    other = {"id": 8, "startTime": "2026-08-19T07:00:00.000Z",
             "relatedTo": [{"id": 111, "entity": "contact"}]}
    c = _SweepClient({5927888: [other, mine]})
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
