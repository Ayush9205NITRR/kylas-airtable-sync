"""
Tests for the time a calendar invite blocks.

Kylas returns the Next Call Date (Call Later) picker as UTC. The invites used
to read the clock digits as if they were already IST, so the picker's 9:00 AM
IST default (03:30Z) went out as 03:30 AM and 136 of 156 invites blocked the
wrong hour. What is pinned down here:

  * UTC is converted to IST, including when that moves the date
  * values with no time of day stay all-day, and an unreadable time is never
    guessed at
  * the invite's DTSTART is the IST wall-clock time, and every send carries a
    higher SEQUENCE so a corrected invite replaces the wrong one on the calendar
"""
import os
import re
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from utils import calendar_invite as cal  # noqa: E402


def test_the_picker_default_is_nine_am_ist_not_three_thirty_am():
    """03:30Z is the Kylas picker's default. It was going out as 3:30 AM."""
    assert cal.parse_kylas_datetime("2026-10-19T03:30:00.000Z") == ("2026-10-19", "09:00:00")


def test_a_real_picked_time_is_shifted_by_five_thirty():
    assert cal.parse_kylas_datetime("2026-10-29T06:30:00.000Z") == ("2026-10-29", "12:00:00")
    assert cal.parse_kylas_datetime("2026-10-19T12:30:00.000Z") == ("2026-10-19", "18:00:00")
    assert cal.parse_kylas_datetime("2026-10-19T05:49:00.000Z") == ("2026-10-19", "11:19:00")


def test_converting_to_ist_can_move_the_date_to_the_next_day():
    """18:45Z is 00:15 IST tomorrow. The date must come from the converted
    value, or the invite lands on the wrong day."""
    assert cal.parse_kylas_datetime("2026-10-18T18:45:00.000Z") == ("2026-10-19", "00:15:00")


def test_a_value_already_carrying_an_ist_offset_is_not_shifted_again():
    assert cal.parse_kylas_datetime("2026-10-19T09:00:00+05:30") == ("2026-10-19", "09:00:00")


def test_a_timestamp_with_no_zone_is_read_as_utc():
    assert cal.parse_kylas_datetime("2026-10-19T03:30:00") == ("2026-10-19", "09:00:00")


def test_a_date_only_value_stays_all_day():
    assert cal.parse_kylas_datetime("2026-10-19") == ("2026-10-19", "")


def test_an_unreadable_time_falls_back_to_all_day_rather_than_a_guess():
    assert cal.parse_kylas_datetime("2026-10-19Tgarbage") == ("2026-10-19", "")


def test_the_display_form_is_already_local_and_is_read_as_is():
    assert cal.parse_kylas_datetime("Oct 19, 2026 at 11:00 AM") == ("2026-10-19", "11:00:00")
    assert cal.parse_kylas_datetime("Oct 19, 2026") == ("2026-10-19", "")


def test_empty_and_junk_give_nothing():
    assert cal.parse_kylas_datetime("") == ("", "")
    assert cal.parse_kylas_datetime(None) == ("", "")
    assert cal.parse_kylas_datetime("not a date") == ("", "")


# -- what actually goes into the invite ------------------------------------

def _ical(call_date, call_time):
    return cal._build_ical(
        contact_id="42", contact_name="Jane Doe", contact_email="j@x.com",
        contact_phone="1", company_name="Acme", remarks="",
        call_date=call_date, owner_email="rep@enout.in",
        organizer_email="bot@enout.in", kylas_url="", call_time=call_time)


def test_a_picker_default_invite_blocks_nine_am_ist():
    d, t = cal.parse_kylas_datetime("2026-10-19T03:30:00.000Z")
    ics = _ical(date.fromisoformat(d), t)
    assert "DTSTART;TZID=Asia/Kolkata:20261019T090000" in ics
    assert "DTEND;TZID=Asia/Kolkata:20261019T100000" in ics
    assert "9:00 AM IST" in ics


def test_an_invite_whose_date_rolled_over_uses_the_ist_date_in_its_uid():
    d, t = cal.parse_kylas_datetime("2026-10-18T18:45:00.000Z")
    ics = _ical(date.fromisoformat(d), t)
    assert "UID:call-42-20261019@kylas-sync" in ics
    assert "DTSTART;TZID=Asia/Kolkata:20261019T001500" in ics


def test_every_send_carries_a_higher_sequence_so_a_correction_replaces_the_old_event(monkeypatch):
    sends_at = [datetime(2026, 10, 18, 18, 30, tzinfo=timezone.utc),
                datetime(2026, 10, 18, 18, 31, tzinfo=timezone.utc),
                datetime(2026, 10, 19, 20, 0, tzinfo=timezone.utc)]
    seq = []
    for when in sends_at:
        monkeypatch.setattr(cal, "_now_utc", lambda w=when: w)
        ics = _ical(date(2026, 10, 19), "09:00:00")
        seq.append(int(re.search(r"SEQUENCE:(\d+)", ics).group(1)))
    assert seq == sorted(seq) and len(set(seq)) == 3
    assert max(seq) < 2 ** 31, "must stay a valid 32-bit integer"


def test_the_invite_has_a_dtstamp():
    ics = _ical(date(2026, 10, 19), "09:00:00")
    assert re.search(r"DTSTAMP:\d{8}T\d{6}Z", ics)
