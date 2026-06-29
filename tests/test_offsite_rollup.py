"""Unit tests for KylasClient.merge_company_multiselect.

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
    sent = set(calls[0]["customFieldValues"][CF_KEY])
    assert sent == {101, 102}, f"Expected {{101, 102}}, got {sent}"


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
    sent = set(calls[0]["customFieldValues"][CF_KEY])
    assert sent == {101, 102}, f"Expected {{101, 102}}, got {sent}"


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
        if all(isinstance(v, int) for v in val):   # bare ids -> fail
            raise requests.HTTPError("500 Server Error — bad encoding")
        return {}   # object list -> succeed

    client._put = fake_put
    result = client.merge_company_multiselect(1, CF_KEY, ["Apr - Jun"])
    assert result == "updated", result
    assert len(attempts) == 2, f"Expected 2 PUT attempts, got {len(attempts)}"
    assert all(isinstance(v, dict) for v in attempts[1]), (
        f"Second attempt should use object list, got {attempts[1]}"
    )


if __name__ == "__main__":
    test_already_present_returns_unchanged()
    test_new_label_returns_updated_and_puts()
    test_dedup_labels_no_duplicate_ids()
    test_unmapped_label_unchanged_no_put()
    test_as_id_set_normalises_formats()
    test_dry_run_no_put()
    test_retry_with_object_list_on_put_failure()
    print("\nALL TESTS PASSED")
