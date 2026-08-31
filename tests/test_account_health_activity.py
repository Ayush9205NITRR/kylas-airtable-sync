"""
Tests for _last_activity() — the composite date behind "Last Called At (Contacts)".

cfLastCalledAt is blank on a large share of contacts, which left accounts looking
untouched forever. The composite is max(createdAt, updatedAt, cfLastCalledAt).

The load-bearing test here is the last one: the composite must NOT feed the
called/called_apr19 counters. Every contact has a createdAt, so if it did, every
account would read as "called" and the Fresh/Active and Tapped/Stale splits would
collapse silently.
"""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("AIRTABLE_PAT", "x")
os.environ.setdefault("AIRTABLE_BASE_ID", "app_test")

_spec = importlib.util.spec_from_file_location(
    "ah_activity", os.path.join(_ROOT, "modules", "06_account_health.py"))
ah = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ah)


def _ct(created=None, updated=None, called=None, company=77, cid=1):
    return {"id": cid, "company": company,
            "createdAt": created, "updatedAt": updated,
            "customFieldValues": {"cfLastCalledAt": called} if called else {}}


def test_uses_updated_at_when_never_called():
    """The real case from production: cfLastCalledAt blank, timestamps present."""
    assert ah._last_activity(
        _ct("2026-04-08T17:33:29.90", "2026-06-05T07:23:22.11")) == "2026-06-05"


def test_real_call_beats_an_older_update():
    assert ah._last_activity(
        _ct("2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z", "2026-03-09")) == "2026-03-09"


def test_update_after_the_last_call_wins():
    assert ah._last_activity(
        _ct("2026-01-01T00:00:00Z", "2026-08-20T00:00:00Z", "2026-03-09")) == "2026-08-20"


def test_created_only_still_yields_a_date():
    assert ah._last_activity(_ct("2026-07-04T09:00:00Z")) == "2026-07-04"


def test_nothing_usable_is_blank():
    assert ah._last_activity(_ct()) == ""


def test_long_form_call_date_still_parses():
    """cfLastCalledAt also arrives as "Jun 05, 2026" — it must still compare."""
    assert ah._last_activity(
        _ct("2026-01-01T00:00:00Z", None, "Jun 05, 2026")) == "2026-06-05"


def test_composite_does_not_inflate_called_or_collapse_fresh():
    """A contact created today but never called: has an activity date, but is
    NOT 'called', and its account stays Fresh rather than flipping to Active."""
    health = ah.compute_health([_ct("2026-08-30T00:00:00Z", "2026-08-30T00:00:00Z")])
    e = health["77"]
    assert e["called"] == 0
    assert e["called_apr19"] == 0
    assert e["status"] == "Fresh"
    assert e["last_called"] == ""
    assert e["last_activity"] == "2026-08-30"


def test_account_takes_the_latest_activity_across_its_contacts():
    health = ah.compute_health([
        _ct("2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z", cid=1),
        _ct("2026-01-01T00:00:00Z", "2026-08-25T00:00:00Z", cid=2),
    ])
    assert health["77"]["last_activity"] == "2026-08-25"
