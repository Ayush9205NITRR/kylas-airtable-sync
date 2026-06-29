"""Unit tests for KylasClient.merge_company_multiselect and rollup helpers.

Stubs:
  - get_custom_field_defs("company") -> options {"jan - mar":101,"apr - jun":102,"jul - sep":103}
  - _get -> a company body with given customFieldValues
  - _put -> records the body it received; returns {}

Run: python -m pytest tests/test_offsite_rollup.py -q
"""
import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("KYLAS_API_KEY", "test:1")

from utils.kylas_client import KylasClient  # noqa: E402
from scripts.rollup_offsite_timeline import _company_key_from_config  # noqa: E402

CF_KEY = "cfOffsiteTimelineNew"

_COMPANY_DEFS = {
    CF_KEY: {
        "type": "PICK_LIST",
        "multiValue": True,
        "options": {"jan - mar": 101, "apr - jun": 102, "jul - sep": 103},
        "labels": {101: "Jan - Mar", 102: "Apr - Jun", 103: "Jul - Sep"},
    }
}


def _make_client(current_ids: list):
    """Return a KylasClient with stubbed _get/_put and the given current ids."""
    client = KylasClient()
    # Stub get_custom_field_defs so it returns our test defs (cached immediately).
    client._cf_defs_cache = {"company": _COMPANY_DEFS}

    cfv = {CF_KEY: current_ids} if current_ids is not None else {}
    client._get = lambda path, params=None: {
        "data": {"id": 1, "name": "TestCo", "customFieldValues": cfv}
    }
    return client


def _capture_put(client):
    """Attach a _put stub that records calls; return the calls list."""
    calls = []

    def fake_put(path, body):
        calls.append(body)
        return {}

    client._put = fake_put
    return calls


# ---------------------------------------------------------------------------
# Case 1: label already present → unchanged, no PUT.
# ---------------------------------------------------------------------------
def test_already_present_returns_unchanged():
    client = _make_client([101])
    calls  = _capture_put(client)
    result = client.merge_company_multiselect(1, CF_KEY, ["Jan - Mar"])
    assert result == "unchanged", result
    assert calls == [], "Expected _put NOT to be called"


# ---------------------------------------------------------------------------
# Case 2: new label → updated, PUT called with union {101, 102}.
# ---------------------------------------------------------------------------
def test_new_label_returns_updated_and_puts():
    client = _make_client([101])
    calls  = _capture_put(client)
    result = client.merge_company_multiselect(1, CF_KEY, ["Apr - Jun"])
    assert result == "updated", result
    assert len(calls) == 1, f"Expected exactly 1 PUT call, got {len(calls)}"
    # First attempt uses {id, name} format matching Kylas GET shape.
    sent = calls[0]["customFieldValues"][CF_KEY]
    sent_ids = {v["id"] for v in sent}
    assert sent_ids == {101, 102}, f"Expected {{101, 102}}, got {sent_ids}"
    assert all("name" in v for v in sent), f"Expected {{id,name}} dicts, got {sent}"


# ---------------------------------------------------------------------------
# Dedup: duplicate labels in add_labels → no duplicate ids in PUT.
# ---------------------------------------------------------------------------
def test_dedup_labels_no_duplicate_ids():
    client = _make_client([101])
    calls  = _capture_put(client)
    result = client.merge_company_multiselect(
        1, CF_KEY, ["Jan - Mar", "Apr - Jun", "Apr - Jun"]
    )
    assert result == "updated", result
    sent = calls[0]["customFieldValues"][CF_KEY]
    sent_ids = {v["id"] for v in sent}
    assert sent_ids == {101, 102}, f"Expected {{101, 102}}, got {sent_ids}"


# ---------------------------------------------------------------------------
# Unmapped label: skip it; if no new ids → unchanged, no PUT.
# ---------------------------------------------------------------------------
def test_unmapped_label_unchanged_no_put():
    client = _make_client([101])
    calls  = _capture_put(client)
    result = client.merge_company_multiselect(1, CF_KEY, ["Nonexistent"])
    assert result == "unchanged", result
    assert calls == [], "Expected _put NOT to be called for unmapped-only labels"


# ---------------------------------------------------------------------------
# _as_id_set: verify normalisation covers edge cases.
# ---------------------------------------------------------------------------
def test_as_id_set_normalises_formats():
    assert KylasClient._as_id_set(None) == set()
    assert KylasClient._as_id_set(101) == {101}
    assert KylasClient._as_id_set({"id": 101}) == {101}
    assert KylasClient._as_id_set([101, {"id": 102}]) == {101, 102}
    assert KylasClient._as_id_set([]) == set()


# ---------------------------------------------------------------------------
# dry_run: prints without calling _put; still returns "updated".
# ---------------------------------------------------------------------------
def test_dry_run_no_put(capsys):
    client = _make_client([101])
    calls  = _capture_put(client)
    result = client.merge_company_multiselect(1, CF_KEY, ["Apr - Jun"], dry_run=True)
    assert result == "updated", result
    assert calls == [], "Expected _put NOT to be called in dry_run mode"
    out = capsys.readouterr().out
    assert "DRY RUN" in out, "Expected DRY RUN output"


# ---------------------------------------------------------------------------
# Retry: if bare-id PUT raises, retry with object list and succeed.
# ---------------------------------------------------------------------------
def test_retry_with_object_list_on_put_failure():
    import requests
    client = _make_client([101])
    client._cf_defs_cache = {"company": _COMPANY_DEFS}

    attempts = []

    def fake_put(path, body):
        val = body["customFieldValues"][CF_KEY]
        attempts.append(val)
        # Fail the first attempt ({id,name} dicts); succeed on subsequent.
        if len(attempts) == 1:
            raise requests.HTTPError("500 Server Error — bad encoding")
        return {}

    client._put = fake_put
    result = client.merge_company_multiselect(1, CF_KEY, ["Apr - Jun"])
    assert result == "updated", result
    assert len(attempts) == 2, f"Expected 2 PUT attempts, got {len(attempts)}"
    # First attempt: {id, name} format; second: bare-id list.
    assert all(isinstance(v, dict) and "name" in v for v in attempts[0]), (
        f"First attempt should use {{id,name}} dicts, got {attempts[0]}"
    )
    assert all(isinstance(v, int) for v in attempts[1]), (
        f"Second attempt should use bare-id list, got {attempts[1]}"
    )


# ---------------------------------------------------------------------------
# _company_key_from_config: single-key config fallback resolution.
# ---------------------------------------------------------------------------
def test_company_key_from_config_single_key():
    """Returns the sole key when exactly one is present in the company block."""
    cfg = {"company": {"cfOffsiteTimelineBdNew": {"jan - mar": 501}}}
    assert _company_key_from_config(cfg) == "cfOffsiteTimelineBdNew"


def test_company_key_from_config_no_company_block():
    """Returns None when there is no company block."""
    assert _company_key_from_config({}) is None


def test_company_key_from_config_multiple_keys():
    """Returns None when there are multiple keys (ambiguous — caller must specify)."""
    cfg = {"company": {"cfKeyA": {"jan - mar": 501}, "cfKeyB": {"jan - mar": 502}}}
    assert _company_key_from_config(cfg) is None


def test_company_key_from_config_empty_company_block():
    """Returns None when the company block is present but empty."""
    assert _company_key_from_config({"company": {}}) is None


def test_company_key_from_config_via_monkeypatch():
    """Integration: monkeypatching _load_picklist_config on a real client picks the config key."""
    client = KylasClient()
    client._cf_defs_cache = {"company": {}}  # no API-sourced defs for company
    client._load_picklist_config = lambda: {
        "company": {"cfOffsiteTimelineBdNew": {"jan - mar": 501}}
    }
    cfg = client._load_picklist_config()
    resolved = _company_key_from_config(cfg)
    assert resolved == "cfOffsiteTimelineBdNew"


if __name__ == "__main__":
    test_already_present_returns_unchanged()
    test_new_label_returns_updated_and_puts()
    test_dedup_labels_no_duplicate_ids()
    test_unmapped_label_unchanged_no_put()
    test_as_id_set_normalises_formats()
    test_dry_run_no_put()
    test_retry_with_object_list_on_put_failure()
    test_company_key_from_config_single_key()
    test_company_key_from_config_no_company_block()
    test_company_key_from_config_multiple_keys()
    test_company_key_from_config_empty_company_block()
    test_company_key_from_config_via_monkeypatch()
    print("\nALL TESTS PASSED")
