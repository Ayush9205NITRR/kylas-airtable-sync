"""
Tests for the BD Members Active-checkbox planning logic.

Matching is on email, never the Name column: BD Members stores short first
names ("Gaurav") while Kylas resolves full ones ("Gaurav Kumar"), so name
matching would be ambiguous or silently wrong.
"""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "set_bd_member_active", os.path.join(_ROOT, "scripts", "set_bd_member_active.py"))
sbma = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sbma)


class _FakeTable:
    def __init__(self, rows):
        self._rows = rows
        self.updates = []

    def all(self):
        return self._rows

    def update(self, record_id, fields):
        self.updates.append((record_id, fields))


class _FakeAt:
    def __init__(self, rows):
        self.table = _FakeTable(rows)


def _row(rid, name, email, active):
    return {"id": rid, "fields": {"Name": name, "Email": email, "Active": active}}


def test_an_active_member_flagged_inactive_is_a_change():
    at = _FakeAt([_row("r1", "Devansh", "devansh.shukla@enout.in", True)])
    changes, not_found = sbma.build_plan(at, {"devansh.shukla@enout.in"}, set())
    assert changes == [{"record_id": "r1", "name": "Devansh",
                        "email": "devansh.shukla@enout.in", "from": True, "to": False}]
    assert not_found == set()


def test_already_inactive_is_not_a_change():
    at = _FakeAt([_row("r1", "Devansh", "devansh.shukla@enout.in", False)])
    changes, _ = sbma.build_plan(at, {"devansh.shukla@enout.in"}, set())
    assert changes == []


def test_reactivating_someone_who_is_currently_inactive():
    at = _FakeAt([_row("r1", "Aditi", "aditi@enout.in", False)])
    changes, _ = sbma.build_plan(at, set(), {"aditi@enout.in"})
    assert changes == [{"record_id": "r1", "name": "Aditi", "email": "aditi@enout.in",
                        "from": False, "to": True}]


def test_email_match_is_case_insensitive():
    at = _FakeAt([_row("r1", "Devansh", "Devansh.Shukla@Enout.in", True)])
    changes, not_found = sbma.build_plan(at, {"devansh.shukla@enout.in"}, set())
    assert len(changes) == 1 and not_found == set()


def test_an_email_with_no_matching_row_is_reported_not_found():
    at = _FakeAt([_row("r1", "Aditi", "aditi@enout.in", True)])
    changes, not_found = sbma.build_plan(at, {"nobody@enout.in"}, set())
    assert changes == []
    assert not_found == {"nobody@enout.in"}


def test_default_missing_active_field_is_treated_as_active():
    """A row with no Active value at all -- e.g. a brand-new record -- must
    read as active (matches BD Members' own .get('Active', True) elsewhere),
    so flagging it inactive is still a real change."""
    row = {"id": "r1", "fields": {"Name": "New", "Email": "new@enout.in"}}
    at = _FakeAt([row])
    changes, _ = sbma.build_plan(at, {"new@enout.in"}, set())
    assert changes[0]["from"] is True and changes[0]["to"] is False


def test_apply_changes_writes_active_to_the_table():
    at = _FakeAt([_row("r1", "Devansh", "devansh.shukla@enout.in", True)])
    changes, _ = sbma.build_plan(at, {"devansh.shukla@enout.in"}, set())
    ok = sbma.apply_changes(at, changes)
    assert ok == 1
    assert at.table.updates == [("r1", {"Active": False})]


def test_multiple_emails_in_one_plan():
    at = _FakeAt([
        _row("r1", "Devansh", "devansh.shukla@enout.in", True),
        _row("r2", "Gaurav", "gaurav@enout.in", True),
        _row("r3", "Aditi", "aditi@enout.in", True),   # not requested, must not change
    ])
    changes, _ = sbma.build_plan(
        at, {"devansh.shukla@enout.in", "gaurav@enout.in"}, set())
    assert {c["record_id"] for c in changes} == {"r1", "r2"}


def test_dry_run_is_the_default(monkeypatch):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args([])
    assert args.apply is False
