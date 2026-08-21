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
# behaviour is live by sweeping with two seeds and comparing.

class _SweepClient(KylasClient):
    def __init__(self, by_seed):
        super().__init__()
        self._by_seed = by_seed
        self.pages = []

    def _call_log_page(self, entity_id, entity_type, page, size):
        self.pages.append((int(entity_id), page))
        return self._by_seed.get(int(entity_id), []) if page == 0 else []


def test_identical_results_from_two_seeds_means_filter_ignored():
    rows = [{"id": 1}, {"id": 2}]
    c = _SweepClient({10: list(rows), 20: list(rows)})
    got, ignored = c.get_all_call_logs([10, 20])
    assert ignored is True
    assert {r["id"] for r in got} == {1, 2}


def test_differing_results_means_the_filter_works_and_sweep_is_not_trusted():
    c = _SweepClient({10: [{"id": 1}], 20: [{"id": 2}]})
    got, ignored = c.get_all_call_logs([10, 20])
    assert ignored is False
    assert {r["id"] for r in got} == {1, 2}


def test_a_single_seed_cannot_prove_completeness():
    c = _SweepClient({10: [{"id": 1}]})
    got, ignored = c.get_all_call_logs([10])
    assert ignored is False and len(got) == 1


def test_no_seeds_returns_nothing_rather_than_guessing():
    c = _SweepClient({})
    assert c.get_all_call_logs([]) == ([], False)


def test_sweep_stops_when_a_page_repeats_ids():
    # The endpoint ignores entity filters, so paging cannot be trusted either.
    c = _SweepClient({10: [{"id": 1}], 20: [{"id": 1}]})
    c.get_all_call_logs([10, 20])
    assert max(p for _, p in c.pages) <= 1
