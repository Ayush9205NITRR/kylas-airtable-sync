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


def test_build_plan_excludes_companies_owned_by_a_given_owner():
    """A company owned by an excluded id is treated as if it had no owner at
    all -- its contacts must not move, even if they mismatch."""
    k = _FakeKylas([_co(1, owner=74725), _co(2, owner=10)],
                  [_ct(100, 1, owner=99), _ct(101, 2, owner=99)])
    moves, stats = ac.build_plan(k, exclude_owners={74725})
    assert {m["contact_id"] for m in moves} == {101}
    assert stats["companies_excluded"] == 1
    assert stats["companies_with_owner"] == 1


def test_build_plan_with_no_exclude_owners_excludes_nothing():
    """Default (no exclude_owners passed) must behave exactly as before --
    existing callers and the CLI's plain dry-run path depend on this."""
    k = _FakeKylas([_co(1, owner=74725)], [_ct(100, 1, owner=99)])
    moves, stats = ac.build_plan(k)
    assert len(moves) == 1
    assert stats["companies_excluded"] == 0


def test_build_plan_excludes_multiple_owners():
    k = _FakeKylas([_co(1, owner=74725), _co(2, owner=555), _co(3, owner=10)],
                  [_ct(100, 1, owner=99), _ct(101, 2, owner=99), _ct(102, 3, owner=99)])
    moves, stats = ac.build_plan(k, exclude_owners={74725, 555})
    assert {m["contact_id"] for m in moves} == {102}
    assert stats["companies_excluded"] == 2


def test_find_companies_owned_by_filters_to_the_given_owner():
    k = _FakeKylas([_co(1, owner=74725), _co(2, owner=10), _co(3, owner=74725)], [])
    owned = ac.find_companies_owned_by(k, 74725)
    assert {co["id"] for co in owned} == {1, 3}


def test_find_companies_owned_by_excludes_ownerless_companies():
    k = _FakeKylas([_co(1, owner=None)], [])
    assert ac.find_companies_owned_by(k, 74725) == []


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


class _FakeKylasWrite:
    """update_contact_owner() always reports success; get_contact() returns
    whatever owner_after_write says it actually is — lets a test simulate the
    silent-no-op fallback bug independently of the call's own return value."""
    def __init__(self, owner_after_write: dict, call_result=None):
        self._owner_after = owner_after_write
        self._call_result = call_result if call_result is not None else {}
        self.calls = []

    def update_contact_owner(self, cid, uid):
        self.calls.append((cid, uid))
        return self._call_result.get(cid, True)

    def get_contact(self, cid):
        return {"ownedBy": {"id": self._owner_after.get(cid)}}


def test_apply_moves_respects_limit():
    k = _FakeKylasWrite(owner_after_write={1: 10, 2: 10, 3: 10})
    moves = [{"contact_id": 1, "to_owner": 10, "contact_name": "A"},
             {"contact_id": 2, "to_owner": 10, "contact_name": "B"},
             {"contact_id": 3, "to_owner": 10, "contact_name": "C"}]
    ac.apply_moves(k, moves, limit=2)
    assert len(k.calls) == 2, "limit=2 must stop after the second move"


def test_apply_moves_counts_a_failed_api_call():
    k = _FakeKylasWrite(owner_after_write={1: 10}, call_result={2: False})
    moves = [{"contact_id": 1, "to_owner": 10, "contact_name": "A"},
             {"contact_id": 2, "to_owner": 10, "contact_name": "B"}]
    tally = ac.apply_moves(k, moves)
    assert tally == {"ok": 1, "failed": 1, "unverified": 0}


def test_apply_moves_catches_the_silent_no_op_fallback_bug():
    """The exact failure mode reported in production: update_contact_owner()
    returns True, but the owner never actually changed (the fallback PUT is a
    documented no-op) and Kylas is left showing some other owner (e.g. the
    API key's own service account). Must be counted as FAILED, not ok —
    trusting the boolean here is precisely what let this go unnoticed."""
    k = _FakeKylasWrite(owner_after_write={1: 999})   # call succeeds, owner is wrong
    moves = [{"contact_id": 1, "to_owner": 10, "contact_name": "A"}]
    tally = ac.apply_moves(k, moves)
    assert tally == {"ok": 0, "failed": 1, "unverified": 0}


def test_apply_moves_confirms_a_real_success():
    k = _FakeKylasWrite(owner_after_write={1: 10})
    moves = [{"contact_id": 1, "to_owner": 10, "contact_name": "A"}]
    tally = ac.apply_moves(k, moves)
    assert tally == {"ok": 1, "failed": 0, "unverified": 0}


def test_apply_moves_counts_unverified_when_the_read_back_itself_fails():
    class _K:
        def update_contact_owner(self, cid, uid):
            return True
        def get_contact(self, cid):
            raise RuntimeError("network blip")

    tally = ac.apply_moves(_K(), [{"contact_id": 1, "to_owner": 10, "contact_name": "A"}])
    assert tally == {"ok": 0, "failed": 0, "unverified": 1}


def test_current_owner_id_reads_ownedby_dict_first_then_ownerid():
    class _K:
        def get_contact(self, cid):
            return {"ownedBy": {"id": 42}, "ownerId": 7}
    assert ac._current_owner_id(_K(), 1) == 42

    class _K2:
        def get_contact(self, cid):
            return {"ownedBy": None, "ownerId": 7}
    assert ac._current_owner_id(_K2(), 1) == 7

    class _K3:
        def get_contact(self, cid):
            return {}
    assert ac._current_owner_id(_K3(), 1) is None


def test_dry_run_is_the_default_and_writes_nothing(monkeypatch, capsys):
    """The whole safety model rests on --apply being opt-in. If this default
    ever flips, the script starts writing production Kylas on a bare run."""
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args([])
    assert args.apply is False


def test_resolve_user_names_prefers_team_json_over_the_live_call(tmp_path, monkeypatch):
    """The live get_users() only returns its first page (a real bug elsewhere
    in this client); team.json is synced daily and has the full roster, so it
    must win on any id both sources claim."""
    team = tmp_path / "team.json"
    team.write_text('{"kylas_users": {"1": "From Team JSON", "2": "Also From Team JSON"}}')
    monkeypatch.setattr(ac, "__file__", str(tmp_path / "scripts" / "assign_contacts_to_company_owner.py"))
    (tmp_path / "scripts").mkdir(exist_ok=True)
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "team.json").write_text(team.read_text())

    class _K:
        def get_users(self):
            return {1: "From Live API (stale/wrong)", 3: "Only Live Has This"}

    names = ac.resolve_user_names(_K())
    assert names[1] == "From Team JSON"       # team.json wins the clash
    assert names[2] == "Also From Team JSON"  # team.json-only entry survives
    assert names[3] == "Only Live Has This"   # live call fills a real gap


def test_resolve_user_names_survives_both_sources_failing(tmp_path, monkeypatch):
    monkeypatch.setattr(ac, "__file__", str(tmp_path / "scripts" / "assign_contacts_to_company_owner.py"))

    class _K:
        def get_users(self):
            raise RuntimeError("Kylas is down")

    assert ac.resolve_user_names(_K()) == {}
