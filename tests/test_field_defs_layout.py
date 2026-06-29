"""Unit tests for _field_defs_from_layout and layout-merged get_custom_field_defs.

Covers:
  A) _field_defs_from_layout: correct parsing of nested layoutItems structure.
  B) get_custom_field_defs: layout options adopted when /entities returns nothing.
  C) cf_key_for_display: matches a key whose displayName came only from layout.

Run: python -m pytest tests/test_field_defs_layout.py -q
"""
import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("KYLAS_API_KEY", "test:1")

from utils.kylas_client import KylasClient  # noqa: E402

# ---------------------------------------------------------------------------
# Shared fixture — mimics the nested layoutItems structure from the Kylas API.
# The layout contains one custom picklist field at an arbitrary nesting depth.
# ---------------------------------------------------------------------------
_LAYOUT_FIXTURE = {
    "layoutItems": [
        {
            "type": "SECTION",
            "layoutItems": [
                {
                    "type": "FIELD",
                    "item": {
                        "id": 9001,
                        "internalName": "cfOffsiteTimelineBdNew",
                        "type": "PICK_LIST",
                        "displayName": "Offsite Timeline (BD - New)",
                        "multiValue": True,
                        "pickLists": [
                            {"id": 201, "value": "Jan - Mar"},
                            {"id": 202, "value": "Apr - Jun"},
                        ],
                    },
                },
                {
                    "type": "FIELD",
                    "item": {
                        "id": 9002,
                        "internalName": "cfOffsiteTimeline",
                        "type": "PICK_LIST",
                        "displayName": "Offsite Timeline",
                        "multiValue": False,
                        "pickLists": [
                            {"id": 201, "value": "Jan - Mar"},
                            {"id": 202, "value": "Apr - Jun"},
                            {"id": 203, "value": "Jul - Sep"},
                            {"id": 204, "value": "Oct - Dec"},
                        ],
                    },
                },
            ],
        }
    ]
}

# /entities response that returns fields with NO picklist data.
_ENTITIES_SPARSE = [
    {
        "fieldName": "cfOffsiteTimelineBdNew",
        "displayName": "",
        "type": "PICK_LIST",
        "multiValue": True,
        "pickLists": [],
    },
    {
        "fieldName": "cfOffsiteTimeline",
        "displayName": "",
        "type": "PICK_LIST",
        "multiValue": False,
        "pickLists": [],
    },
]


def _fresh_client():
    """Return a KylasClient with cleared caches."""
    client = KylasClient()
    client._cf_defs_cache = {}
    client._cf_cache = {}
    return client


# ---------------------------------------------------------------------------
# A) _field_defs_from_layout — fixture parsing
# ---------------------------------------------------------------------------

def test_layout_parsing_returns_correct_options():
    client = _fresh_client()
    client._get = lambda path, params=None: _LAYOUT_FIXTURE

    defs = client._field_defs_from_layout("company")

    assert "cfOffsiteTimelineBdNew" in defs, "Expected cfOffsiteTimelineBdNew in layout defs"
    defn = defs["cfOffsiteTimelineBdNew"]

    assert defn["type"] == "PICK_LIST"
    assert defn["multiValue"] is True
    assert defn["displayName"] == "Offsite Timeline (BD - New)"

    # options: label_lower -> id
    assert defn["options"] == {"jan - mar": 201, "apr - jun": 202}, (
        f"options mismatch: {defn['options']}"
    )
    # labels: id -> label
    assert defn["labels"] == {201: "Jan - Mar", 202: "Apr - Jun"}, (
        f"labels mismatch: {defn['labels']}"
    )


def test_layout_parsing_returns_second_field():
    client = _fresh_client()
    client._get = lambda path, params=None: _LAYOUT_FIXTURE

    defs = client._field_defs_from_layout("contact")
    assert "cfOffsiteTimeline" in defs
    defn = defs["cfOffsiteTimeline"]
    assert defn["multiValue"] is False
    assert len(defn["options"]) == 4
    assert defn["options"]["oct - dec"] == 204


def test_layout_error_returns_empty_dict():
    client = _fresh_client()

    def _bad_get(path, params=None):
        raise RuntimeError("network error")

    client._get = _bad_get
    result = client._field_defs_from_layout("company")
    assert result == {}, f"Expected {{}} on error, got {result}"


# ---------------------------------------------------------------------------
# B) get_custom_field_defs — layout options fill the gap left by /entities
# ---------------------------------------------------------------------------

def _get_stub_sparse_then_layout(path, params=None):
    """Stub: /entities returns sparse list; layout returns fixture."""
    if path.startswith("entities/"):
        return _ENTITIES_SPARSE      # list form — no picklist data
    if "layouts" in path:
        return _LAYOUT_FIXTURE
    return {}


def test_get_custom_field_defs_adopts_layout_options_when_entities_empty():
    client = _fresh_client()
    client._get = _get_stub_sparse_then_layout

    defs = client.get_custom_field_defs("company")

    assert "cfOffsiteTimelineBdNew" in defs
    defn = defs["cfOffsiteTimelineBdNew"]
    # Options must come from layout (entities returned none).
    assert defn["options"], "Expected non-empty options from layout"
    assert defn["options"].get("jan - mar") == 201
    assert defn["options"].get("apr - jun") == 202
    assert defn["labels"].get(201) == "Jan - Mar"


def test_get_custom_field_defs_adopts_display_name_from_layout():
    client = _fresh_client()
    client._get = _get_stub_sparse_then_layout

    defs = client.get_custom_field_defs("contact")
    defn = defs.get("cfOffsiteTimeline", {})
    assert defn.get("displayName") == "Offsite Timeline", (
        f"displayName should be filled from layout, got {defn.get('displayName')!r}"
    )


def test_get_custom_field_defs_layout_only_key_adopted():
    """A key that appears only in the layout (not /entities) must be included."""
    client = _fresh_client()

    entities_no_cf = []  # /entities returns nothing

    def _get_stub(path, params=None):
        if path.startswith("entities/"):
            return entities_no_cf
        if "layouts" in path:
            return _LAYOUT_FIXTURE
        return {}

    client._get = _get_stub

    defs = client.get_custom_field_defs("company")
    assert "cfOffsiteTimelineBdNew" in defs, (
        "Layout-only key must be present when /entities returns nothing"
    )


# ---------------------------------------------------------------------------
# C) cf_key_for_display — matches layout-sourced displayName
# ---------------------------------------------------------------------------

def test_cf_key_for_display_finds_layout_sourced_name():
    client = _fresh_client()
    client._get = _get_stub_sparse_then_layout

    # list_custom_field_keys will call /entities (returns sparse — no displayName).
    # cf_key_for_display must fall through to the defs scan and find the match.
    key = client.cf_key_for_display("company", "Offsite Timeline (BD - New)")
    assert key == "cfOffsiteTimelineBdNew", (
        f"Expected cfOffsiteTimelineBdNew, got {key!r}"
    )


def test_cf_key_for_display_contact_field():
    client = _fresh_client()
    client._get = _get_stub_sparse_then_layout

    key = client.cf_key_for_display("contact", "Offsite Timeline")
    assert key == "cfOffsiteTimeline", f"Expected cfOffsiteTimeline, got {key!r}"


def test_cf_key_for_display_returns_none_for_unknown():
    client = _fresh_client()
    client._get = _get_stub_sparse_then_layout

    key = client.cf_key_for_display("company", "Nonexistent Field XYZ")
    assert key is None, f"Expected None, got {key!r}"


if __name__ == "__main__":
    test_layout_parsing_returns_correct_options()
    test_layout_parsing_returns_second_field()
    test_layout_error_returns_empty_dict()
    test_get_custom_field_defs_adopts_layout_options_when_entities_empty()
    test_get_custom_field_defs_adopts_display_name_from_layout()
    test_get_custom_field_defs_layout_only_key_adopted()
    test_cf_key_for_display_finds_layout_sourced_name()
    test_cf_key_for_display_contact_field()
    test_cf_key_for_display_returns_none_for_unknown()
    print("\nALL TESTS PASSED")
