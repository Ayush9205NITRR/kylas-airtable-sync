"""
Tests for 02_contact_sync.py's _stage_batch_row() -- the entry stage_history
.diff() reads per contact.

Until 2026-09-09, company_id was computed here and then left out of the dict
entirely (only the display name made it through), which silently zeroed
every Company-group metric for every stage move detected via this path,
while Contact-group metrics for the same moves were unaffected.
"""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "contact_sync", os.path.join(_ROOT, "modules", "02_contact_sync.py"))
cs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cs)


def test_stage_batch_row_carries_company_id_from_a_nested_company_object():
    row = cs._stage_batch_row({"company": {"id": 501, "name": "Acme"}},
                              "Follow-up (1)", "Jane Doe", "jane@enout.in")
    assert row["company_id"] == "501"
    assert row["company"] == "Acme"


def test_stage_batch_row_carries_company_id_from_a_bare_int_company():
    """Search results return company as a bare int, not {'id':...,'name':...}
    -- see BD_WORKFLOW.md's "Kylas returns bare integers" gotcha. Both shapes
    must resolve to a usable company_id or this is right back to zeros."""
    row = cs._stage_batch_row({"company": 777}, "CNC (Could Not Connect) - 1",
                              "Jane Doe", "jane@enout.in")
    assert row["company_id"] == "777"
    assert row["company"] == ""


def test_stage_batch_row_blank_company_stays_blank_not_missing():
    row = cs._stage_batch_row({}, "Follow-up (1)", "Jane Doe", "jane@enout.in")
    assert row["company_id"] == "" and row["company"] == ""


def test_stage_batch_row_carries_stage_owner_and_email_through_unchanged():
    row = cs._stage_batch_row({"company": 777}, "Follow-up (1)",
                              "Jane Doe", "jane@enout.in")
    assert row["stage"] == "Follow-up (1)"
    assert row["owner"] == "Jane Doe"
    assert row["email"] == "jane@enout.in"
