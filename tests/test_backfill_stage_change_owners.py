"""
Tests for re-attributing BD Stage Changes rows logged with a blank owner.

The safety property here is that this NEVER invents attribution: a row is
repaired only when the snapshot holds a real owner for that exact contact id.
Guessing would be worse than the blank it replaces, since the digest treats
whatever lands in BD Associate as fact.
"""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("AIRTABLE_PAT", "x")
os.environ.setdefault("AIRTABLE_BASE_ID", "app_test")

_spec = importlib.util.spec_from_file_location(
    "backfill_owners",
    os.path.join(_ROOT, "scripts", "backfill_stage_change_owners.py"))
bf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bf)

_SNAP = {
    "100": {"owner": "Aditi saini", "email": "aditi.saini@enout.in"},
    "200": {"owner": "Gurnoor Kaur", "email": "gurnoor@enout.in"},
    "300": {"owner": "", "email": ""},          # known contact, no owner recorded
}


def _row(rid, cid="100", date="2026-09-10", assoc="", key=None):
    fields = {"Date": date, "BD Associate": assoc,
              "Key": key if key is not None else f"{cid} | {date}"}
    if cid:
        fields["Contact Id"] = cid
    return {"id": rid, "fields": fields}


def test_a_blank_owner_row_is_repaired_from_the_snapshot():
    fixable, unresolved = bf.plan([_row("rec1")], _SNAP)
    assert unresolved == []
    assert fixable == [("rec1", "100", "2026-09-10",
                        "Aditi saini", "aditi.saini@enout.in")]


def test_an_already_attributed_row_is_left_alone():
    fixable, unresolved = bf.plan([_row("rec1", assoc="Aditi saini")], _SNAP)
    assert fixable == [] and unresolved == []


def test_a_contact_the_snapshot_has_no_owner_for_is_never_guessed():
    """Better an honest blank than an invented name — the digest treats
    whatever sits in BD Associate as fact."""
    fixable, unresolved = bf.plan([_row("rec1", cid="300")], _SNAP)
    assert fixable == []
    assert unresolved == [("rec1", "300", "2026-09-10")]


def test_a_contact_absent_from_the_snapshot_is_unresolvable():
    fixable, unresolved = bf.plan([_row("rec1", cid="999")], _SNAP)
    assert fixable == [] and len(unresolved) == 1


def test_contact_id_falls_back_to_the_key_when_the_column_is_missing():
    row = {"id": "rec1", "fields": {"Date": "2026-09-10", "BD Associate": "",
                                    "Key": "200 | 2026-09-10"}}
    fixable, _ = bf.plan([row], _SNAP)
    assert fixable[0][3] == "Gurnoor Kaur"


def test_the_date_filter_scopes_the_repair():
    rows = [_row("rec1", date="2026-09-10"), _row("rec2", date="2026-09-09")]
    fixable, _ = bf.plan(rows, _SNAP, only_date="2026-09-10")
    assert [r[0] for r in fixable] == ["rec1"]


def test_contact_id_of_prefers_the_column_over_the_key():
    assert bf.contact_id_of({"Contact Id": "abc"}, "999 | 2026-09-10") == "abc"
    assert bf.contact_id_of({}, "999 | 2026-09-10") == "999"
    assert bf.contact_id_of({}, "malformed") == ""
