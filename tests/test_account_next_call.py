"""
Tests for the account-level next call date — the rollup behind
"Next Call Date (Contacts)" and the overlay's "Connect today" bucket.

Next Call Date is a CONTACT field in Kylas. An account has many contacts, so
the account-level value has to pick one, and which one is picked decides what
a BD sees in their queue:

  * EARLIEST, not latest. The queue answers "what do I owe today"; the
    soonest contact due is what answers it. Taking the latest would push an
    account due today behind one due next month and hide it.
  * A date in the PAST is kept, not skipped. A call that was missed is the
    most urgent thing on the account. Dropping it is how work disappears.
  * A cleared date must clear the rollup, or an account stays in "Connect
    today" forever after the BD has dealt with it.
"""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("AIRTABLE_PAT", "x")
os.environ.setdefault("AIRTABLE_BASE_ID", "app_test")

_spec = importlib.util.spec_from_file_location(
    "ah_next_call", os.path.join(_ROOT, "modules", "06_account_health.py"))
ah = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ah)


def _ct(next_call=None, cid=1, company=77, created="2026-01-01T00:00:00Z"):
    cf = {}
    if next_call is not None:
        cf["cfNextCallDate"] = next_call
    return {"id": cid, "company": company, "createdAt": created,
            "updatedAt": created, "customFieldValues": cf}


# ── the parser ────────────────────────────────────────────────────────────

def test_reads_an_iso_date():
    assert ah._next_call(_ct("2026-09-10")) == "2026-09-10"


def test_reads_a_kylas_timestamp():
    assert ah._next_call(_ct("2026-09-10T07:23:22.11Z")) == "2026-09-10"


def test_reads_the_long_form_kylas_writes():
    assert ah._next_call(_ct("Sep 10, 2026 at 4:30pm")) == "2026-09-10"


def test_no_next_call_is_blank_not_an_error():
    assert ah._next_call(_ct()) == ""
    assert ah._next_call(_ct("")) == ""
    assert ah._next_call({"id": 1}) == ""


# ── the rollup ────────────────────────────────────────────────────────────

def _rollup(*dates):
    contacts = [_ct(d, cid=i) for i, d in enumerate(dates)]
    return ah.compute_health(contacts)["77"]["next_call"]


def test_earliest_contact_wins():
    assert _rollup("2026-09-30", "2026-09-10", "2026-09-20") == "2026-09-10"


def test_a_missed_call_is_not_skipped_for_a_future_one():
    """The load-bearing case: overdue must not be hidden behind next month."""
    assert _rollup("2026-10-01", "2026-08-01") == "2026-08-01"


def test_contacts_with_no_date_do_not_blank_the_account():
    assert _rollup(None, "2026-09-10", None) == "2026-09-10"


def test_an_account_with_no_dates_at_all_is_blank():
    assert _rollup(None, None) == ""


def test_accounts_are_kept_apart():
    health = ah.compute_health([
        _ct("2026-09-10", cid=1, company=77),
        _ct("2026-12-25", cid=2, company=88),
    ])
    assert health["77"]["next_call"] == "2026-09-10"
    assert health["88"]["next_call"] == "2026-12-25"


# ── what reaches Airtable ─────────────────────────────────────────────────

def test_a_cleared_date_is_written_as_blank():
    """
    Written even when empty, unlike the last-called rollup. If a blank were
    skipped, Airtable would keep the old date and the account would sit in
    "Connect today" forever after the BD rescheduled it in Kylas.
    """
    fm = {"nextCallDateContacts": "Next Call Date (Contacts)"}
    health = ah.compute_health([_ct(None, company=77)])

    fm["id"] = "Kylas Company Id"

    class _Tbl:
        """Just enough AirtableClient for _write_table: one company row."""

        class table:
            @staticmethod
            def all():
                return [{"id": "rec1", "fields": {"Kylas Company Id": "77"}}]

        def __init__(self):
            self._cache = {}
            self._updates = []

        def flush(self):
            pass

    tbl = _Tbl()
    ah._write_table(tbl, health, fm)

    assert tbl._updates, "the account should have been queued for an update"
    _co_id, _rec_id, written = tbl._updates[0]
    assert written.get("Next Call Date (Contacts)") == ""


def test_a_real_date_reaches_airtable():
    fm = {"id": "Kylas Company Id",
          "nextCallDateContacts": "Next Call Date (Contacts)"}
    health = ah.compute_health([_ct("2026-09-10", company=77)])

    class _Tbl:
        """Just enough AirtableClient for _write_table: one company row."""

        class table:
            @staticmethod
            def all():
                return [{"id": "rec1", "fields": {"Kylas Company Id": "77"}}]

        def __init__(self):
            self._cache = {}
            self._updates = []

        def flush(self):
            pass

    tbl = _Tbl()
    ah._write_table(tbl, health, fm)
    _co_id, _rec_id, written = tbl._updates[0]
    assert written["Next Call Date (Contacts)"] == "2026-09-10"
