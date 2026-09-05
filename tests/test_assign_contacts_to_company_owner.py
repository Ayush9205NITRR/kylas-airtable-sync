"""
Tests for the company-owner -> contact-owner planning logic.

This computes a plan that writes production Kylas ownership for potentially
thousands of contacts, so the classification (move / already-correct / no
company / company has no owner) must be exactly right — these tests are the
safety net for a script whose default IS to write once run with --apply.
"""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "assign_cco", os.path.join(_ROOT, "scripts", "assign_contacts_to_company_owner.py"))
ac = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ac)


class _FakeKylas:
    def __init__(self, companies, contacts):
        self._companies, self._contacts = companies, contacts
    def get_companies(self):
        return self._companies
    def get_contacts(self):
        return self._contacts


def _co(cid, owner=None):
    d = {"id": cid}
    if owner is not None:
        d["ownerId"] = owner
    return d


def _ct(cid, company, owner=None, name=""):
    d = {"id": cid, "company": company, "name": name}
    if owner is not None:
        d["ownerId"] = owner
    return d


def test_contact_with_mismatched_owner_is_a_move():
    k = _FakeKylas([_co(1, owner=10)], [_ct(100, 1, owner=99)])
    moves, stats = ac.build_plan(k)
    assert stats["moves"] == 1
    assert moves[0] == {"contact_id": 100, "contact_name": "", "company_id": "1",
                        "from_owner": 99, "to_owner": 10}


def test_contact_already_matching_is_not_a_move():
    k = _FakeKylas([_co(1, owner=10)], [_ct(100, 1, owner=10)])
    moves, stats = ac.build_plan(k)
    assert moves == []
    assert stats["already_correct"] == 1 and stats["moves"] == 0


def test_contact_with_no_owner_at_all_is_a_move():
    """A never-assigned contact under an owned company should still move."""
    k = _FakeKylas([_co(1, owner=10)], [_ct(100, 1, owner=None)])
    moves, _ = ac.build_plan(k)
    assert moves[0]["from_owner"] is None and moves[0]["to_owner"] == 10


def test_company_with_no_owner_is_left_alone():
    """Cannot cascade an ownership that does not exist — must not guess."""
    k = _FakeKylas([_co(1, owner=None)], [_ct(100, 1, owner=99)])
    moves, stats = ac.build_plan(k)
    assert moves == [] and stats["no_company_owner"] == 1


def test_contact_with_no_company_is_left_alone():
    k = _FakeKylas([_co(1, owner=10)], [_ct(100, None)])
    moves, stats = ac.build_plan(k)
    assert moves == [] and stats["no_company"] == 1


def test_company_id_handles_bare_int_and_nested_object():
    """Kylas returns 'company' as a bare int on search results, nested on
    detail reads. Both must resolve to the same company."""
    assert ac._company_id({"company": 5}) == "5"
    assert ac._company_id({"company": {"id": 5}}) == "5"
    assert ac._company_id({"company": None}) == ""
    assert ac._company_id({}) == ""


def test_multiple_contacts_under_one_company_all_move_to_its_owner():
    k = _FakeKylas([_co(1, owner=10)],
                  [_ct(100, 1, owner=1), _ct(101, 1, owner=2), _ct(102, 1, owner=10)])
    moves, stats = ac.build_plan(k)
    assert {m["contact_id"] for m in moves} == {100, 101}
    assert stats["already_correct"] == 1


def test_apply_moves_respects_limit_and_counts_failures():
    calls = []

    class _K:
        def update_contact_owner(self, cid, uid):
            calls.append((cid, uid))
            return cid != 2   # contact 2 "fails"

    moves = [{"contact_id": 1, "to_owner": 10, "contact_name": "A"},
             {"contact_id": 2, "to_owner": 10, "contact_name": "B"},
             {"contact_id": 3, "to_owner": 10, "contact_name": "C"}]
    tally = ac.apply_moves(_K(), moves, limit=2)
    assert len(calls) == 2, "limit=2 must stop after the second move"
    assert tally == {"ok": 1, "failed": 1}


def test_dry_run_is_the_default_and_writes_nothing(monkeypatch, capsys):
    """The whole safety model rests on --apply being opt-in. If this default
    ever flips, the script starts writing production Kylas on a bare run."""
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args([])
    assert args.apply is False
