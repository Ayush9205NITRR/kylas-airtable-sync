"""Offline unit tests for the client repository classifier (no network, no keys).

Covers the three things that decide what a repository row says:
  * client detection  — who gets into the repo at all
  * venture class     — the funded-venture cut and its evidence ladder
  * macro industry    — raw Kylas industry strings collapsed to buckets

Plus round-size parsing, which is the fiddliest bit (mixed currencies, Indian
crore/lakh magnitudes, Indian digit grouping).

Run: python -m pytest tests/test_client_repo.py -q
  or python tests/test_client_repo.py
"""
import importlib.util
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from utils import venture_classifier as vc  # noqa: E402

TAX = vc.load_taxonomy()


def _load_module():
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "modules", "09_client_repo.py")
    spec = importlib.util.spec_from_file_location("client_repo", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ----------------------------------------------------------------------
# Taxonomy integrity
# ----------------------------------------------------------------------

def test_taxonomy_is_self_consistent():
    """Every name in an `order` list must have a definition, and vice versa."""
    vclasses = TAX["venture_classes"]
    assert set(vclasses["order"]) == set(vclasses["rules"]), \
        "venture_classes.order and .rules disagree"
    assert vclasses["fallback"] not in vclasses["order"], \
        "fallback must not also be a rule — it would never be reached"

    macro = TAX["macro_industries"]
    assert set(macro["order"]) == set(macro["buckets"]), \
        "macro_industries.order and .buckets disagree"
    assert macro["fallback"] not in macro["order"]

    stages = TAX["funding_stages"]
    assert set(stages["order"]) == set(stages["keywords"])

    # Exactly one class may carry the funded flag — the repository's "funded
    # venture" cut is a single bucket, not a union.
    funded = [n for n, r in vclasses["rules"].items() if r.get("funded")]
    assert funded == ["Funded Venture"], funded


def test_no_raw_industry_claimed_by_two_buckets():
    """A raw Kylas industry string must map to exactly one macro bucket."""
    seen = {}
    for name in TAX["macro_industries"]["order"]:
        for raw in TAX["macro_industries"]["buckets"][name].get("raw", []):
            key = raw.strip().lower()
            assert key not in seen, f"{raw!r} claimed by both {seen[key]} and {name}"
            seen[key] = name


# ----------------------------------------------------------------------
# Client detection
# ----------------------------------------------------------------------

def test_offsite_done_account_status_is_a_client():
    ok, basis = vc.is_client({"account_status": "Offsite Done"}, TAX)
    assert ok and "Offsite Done" in basis


def test_hot_prospect_is_not_a_client():
    """SQL is the hottest pre-sale stage — hot, but not a client."""
    ok, _ = vc.is_client(
        {"account_status": "SQL", "contact_stages": ["SQL (Sales Qualified Lead)"]}, TAX)
    assert not ok


def test_late_reachout_contact_stage_counts():
    ok, basis = vc.is_client(
        {"account_status": "Active",
         "contact_stages": ["CNC (Could Not Connect) - 1", "Offsite Done (Late Reachout)"]}, TAX)
    assert ok and "Late Reachout" in basis


def test_won_deal_counts_even_without_account_status():
    ok, basis = vc.is_client({"deal_stages": ["Closed Won"]}, TAX)
    assert ok and "Closed Won" in basis


def test_client_detection_is_case_insensitive():
    ok, _ = vc.is_client({"account_status": "  offsite   done "}, TAX)
    assert ok


# ----------------------------------------------------------------------
# Round size parsing
# ----------------------------------------------------------------------

def test_round_size_usd_shorthand():
    usd, cur = vc.parse_round_size("$5M", TAX)
    assert (usd, cur) == (5_000_000.0, "USD")


def test_round_size_written_out():
    usd, cur = vc.parse_round_size("5.5 million USD", TAX)
    assert (usd, cur) == (5_500_000.0, "USD")


def test_round_size_crore_converts_to_usd():
    usd, cur = vc.parse_round_size("₹40 Cr", TAX)
    assert cur == "INR"
    assert abs(usd - (400_000_000 / TAX["fx"]["INR"])) < 1


def test_round_size_indian_digit_grouping():
    """3,50,00,000 is 3.5 crore, not 3.5 million — grouping must be stripped."""
    usd, cur = vc.parse_round_size("INR 3,50,00,000", TAX)
    assert cur == "INR"
    assert abs(usd - (35_000_000 / TAX["fx"]["INR"])) < 1


def test_round_size_lakh():
    usd, cur = vc.parse_round_size("Rs 12 lakh", TAX)
    assert cur == "INR"
    assert abs(usd - (1_200_000 / TAX["fx"]["INR"])) < 1


def test_round_size_euro():
    usd, cur = vc.parse_round_size("€2.5M", TAX)
    assert cur == "EUR"
    assert abs(usd - (2_500_000 / TAX["fx"]["EUR"])) < 1


def test_round_size_no_number_is_none():
    assert vc.parse_round_size("undisclosed", TAX) == (None, "")
    assert vc.parse_round_size("", TAX) == (None, "")
    assert vc.parse_round_size(None, TAX) == (None, "")


def test_magnitude_only_read_after_the_number():
    """A stray 'm' before the digits must not multiply the amount."""
    usd, _ = vc.parse_round_size("Series M round of 750000", TAX)
    assert usd == 750_000.0


def test_format_usd():
    assert vc.format_usd(5_000_000) == "$5.0M"
    assert vc.format_usd(750_000) == "$750K"
    assert vc.format_usd(2_400_000_000) == "$2.4B"
    assert vc.format_usd(None) == ""


# ----------------------------------------------------------------------
# Funding stage normalisation
# ----------------------------------------------------------------------

def test_pre_series_a_beats_series_a():
    assert vc.funding_stage("Pre-Series A bridge", TAX) == "Pre-Series A"


def test_pre_seed_beats_seed():
    assert vc.funding_stage("pre-seed", TAX) == "Pre-Seed"
    assert vc.funding_stage("Seed", TAX) == "Seed"


def test_series_variants():
    assert vc.funding_stage("Series-B", TAX) == "Series B"
    assert vc.funding_stage("SERIES C extension", TAX) == "Series C"
    assert vc.funding_stage("Series F", TAX) == "Series E+"


def test_unknown_round_type():
    assert vc.funding_stage("", TAX) == vc.UNCLASSIFIED_STAGE
    assert vc.funding_stage("mystery money", TAX) == vc.UNCLASSIFIED_STAGE


# ----------------------------------------------------------------------
# Venture classification
# ----------------------------------------------------------------------

def test_explicit_round_type_is_high_confidence_funded():
    p = vc.classify({"name": "Acme Labs", "industry": "SaaS", "cfRoundType": "Series A"}, TAX)
    assert p["venture_class"] == "Funded Venture"
    assert p["venture_confidence"] == "High"
    assert p["is_funded"] is True
    assert p["funding_stage"] == "Series A"


def test_round_size_alone_is_enough():
    p = vc.classify({"name": "Acme Labs", "cfRoundSize": "$12M"}, TAX)
    assert p["venture_class"] == "Funded Venture"
    assert p["venture_confidence"] == "High"
    assert p["round_size_usd"] == 12_000_000.0
    assert p["round_size_display"] == "$12.0M"


def test_batch_keyword_is_medium_confidence_funded():
    """A 'Funded' batch label is real evidence, but weaker than a round field."""
    p = vc.classify({"name": "Acme Labs", "batch": "Funded Startups Jan-26"}, TAX)
    assert p["venture_class"] == "Funded Venture"
    assert p["venture_confidence"] == "Medium"


def test_kylas_custom_field_values_are_read():
    """Raw Kylas payloads nest custom fields — the signal lookup must dig in."""
    p = vc.classify({"name": "Acme", "customFieldValues": {"cfRoundType": "Series B"}}, TAX)
    assert p["funding_stage"] == "Series B"
    assert p["is_funded"] is True


def test_non_profit_is_not_funded():
    p = vc.classify({"name": "Sahaya Foundation", "industry": "Non-profit Organization Management"}, TAX)
    assert p["venture_class"] == "Non-profit / Academic"
    assert p["is_funded"] is False


def test_llp_reads_as_agency():
    p = vc.classify({"name": "Mehta & Associates LLP", "industry": "Legal Services"}, TAX)
    assert p["venture_class"] == "Agency / Services Firm"


def test_headcount_promotes_to_enterprise():
    p = vc.classify({"name": "Vasudha Works", "cfEmployeeCount": "4200"}, TAX)
    assert p["venture_class"] == "Enterprise / Large Corporate"
    assert p["employee_count"] == 4200


def test_no_signal_is_unclassified():
    p = vc.classify({"name": "Zeta", "industry": ""}, TAX)
    assert p["venture_class"] == "Unclassified"
    assert p["venture_confidence"] == "None"
    assert p["is_funded"] is False


def test_bad_headcount_does_not_crash():
    p = vc.classify({"name": "Zeta", "cfEmployeeCount": "about 200"}, TAX)
    assert p["employee_count"] == 200
    p = vc.classify({"name": "Zeta", "cfEmployeeCount": "n/a"}, TAX)
    assert p["employee_count"] is None


# ----------------------------------------------------------------------
# Overrides
# ----------------------------------------------------------------------

def test_override_by_id_wins_over_inference():
    overrides = {"5381741": {"round_type": "Series C", "round_size": "$40M",
                             "notes": "press release"}}
    p = vc.classify({"id": "5381741", "name": "Acme"}, TAX, overrides)
    assert p["funding_stage"] == "Series C"
    assert p["round_size_usd"] == 40_000_000.0
    assert p["override_notes"] == "press release"


def test_override_by_name_is_case_insensitive():
    overrides = {"acme labs": {"venture_class": "Listed / Public Company"}}
    p = vc.classify({"id": "9", "name": "  ACME   Labs "}, TAX, overrides)
    assert p["venture_class"] == "Listed / Public Company"
    assert p["venture_confidence"] == "High"
    assert p["venture_basis"] == "manual override"


def test_override_macro_industry_beats_keyword():
    overrides = {"7": {"macro_industry": "Healthcare & Life Sciences"}}
    p = vc.classify({"id": "7", "name": "Nova SaaS", "industry": "Software"}, TAX, overrides)
    assert p["macro_industry"] == "Healthcare & Life Sciences"


def test_shipped_overrides_file_parses():
    """The committed overrides file must stay loadable — it is read every run."""
    flat = vc.load_overrides()
    assert isinstance(flat, dict)


# ----------------------------------------------------------------------
# Macro industry
# ----------------------------------------------------------------------

def test_exact_raw_industry_match():
    bucket, basis = vc.macro_industry({"industry": "Hospital & Health Care"}, TAX)
    assert bucket == "Healthcare & Life Sciences"
    assert "industry" in basis


def test_raw_match_beats_name_keyword():
    """A curated raw industry value must not be overridden by a name keyword."""
    bucket, _ = vc.macro_industry(
        {"name": "Fintech Logistics Pvt Ltd", "industry": "Farming"}, TAX)
    assert bucket == "Agriculture & AgriTech"


def test_keyword_fallback_when_industry_unknown():
    bucket, basis = vc.macro_industry(
        {"name": "Rapid Freight", "industry": "Unlisted Thing",
         "description": "last mile delivery network"}, TAX)
    assert bucket == "Logistics, Mobility & Supply Chain"
    assert "keyword" in basis


def test_unknown_industry_falls_back():
    bucket, basis = vc.macro_industry({"name": "Zeta", "industry": ""}, TAX)
    assert bucket == "Other / Unclassified"
    assert basis == "no industry signal"


# ----------------------------------------------------------------------
# Repository build + report
# ----------------------------------------------------------------------

_COMPANIES = [
    {"id": "1", "name": "Nova SaaS", "industry": "Software", "account_status": "Offsite Done",
     "owner": "Riya", "batch": "B1", "source_of_data": "Inbound", "cfRoundType": "Series A",
     "cfRoundSize": "$8M", "contact_stages": [], "deal_stages": []},
    {"id": "2", "name": "Sahaya Foundation", "industry": "Non-profit Organization Management",
     "account_status": "Active", "contact_stages": ["Offsite Done"], "deal_stages": []},
    {"id": "3", "name": "Hot Prospect Pvt Ltd", "industry": "Software",
     "account_status": "SQL", "contact_stages": ["SQL (Sales Qualified Lead)"], "deal_stages": []},
    {"id": "4", "name": "Kisan Agri Works", "industry": "Farming", "account_status": "Offsite Done",
     "cfRoundType": "Seed", "cfRoundSize": "₹8 Cr", "contact_stages": [], "deal_stages": []},
]


def test_build_repository_keeps_only_clients():
    mod = _load_module()
    rows = mod.build_repository(_COMPANIES, TAX, overrides={})
    assert {r["name"] for r in rows} == {"Nova SaaS", "Sahaya Foundation", "Kisan Agri Works"}
    assert "Hot Prospect Pvt Ltd" not in {r["name"] for r in rows}

    # Rows come out grouped by venture class, then macro industry, then name —
    # and the order is stable across runs, so diffs of a JSON dump stay readable.
    keys = [(r["venture_class"], r["macro_industry"], r["name"].lower()) for r in rows]
    assert keys == sorted(keys)
    rerun = mod.build_repository(_COMPANIES, TAX, overrides={})
    assert [r["id"] for r in rerun] == [r["id"] for r in rows]


def test_build_repository_classifies_each_row():
    rows = {r["name"]: r for r in _load_module().build_repository(_COMPANIES, TAX, overrides={})}
    nova = rows["Nova SaaS"]
    assert nova["venture_class"] == "Funded Venture"
    assert nova["macro_industry"] == "Software & SaaS"
    assert nova["is_funded"] is True
    assert nova["client_basis"].startswith("account status")

    kisan = rows["Kisan Agri Works"]
    assert kisan["macro_industry"] == "Agriculture & AgriTech"
    assert kisan["funding_stage"] == "Seed"

    sahaya = rows["Sahaya Foundation"]
    assert sahaya["is_funded"] is False
    assert sahaya["client_basis"] == "contact at 'Offsite Done'"


def test_airtable_fields_cover_the_field_map():
    """Every mapped column must be produced, so setup and writes stay in step."""
    mod = _load_module()
    rows = mod.build_repository(_COMPANIES, TAX, overrides={})
    fields = mod._fields_for(rows[0])
    fm_path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                           "config", "field_map.json")
    with open(fm_path) as f:
        mapped = set(json.load(f)["client_repo"].values())
    # _clean() drops empties, so allow a subset — but nothing unmapped may appear.
    assert set(fields) <= mapped, set(fields) - mapped


def test_field_map_matches_setup_script_columns():
    """field_map.json and setup_client_repo.py must define the same columns."""
    spec = importlib.util.spec_from_file_location(
        "setup_client_repo",
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "setup_client_repo.py"))
    setup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup)

    columns = {f["name"] for f in setup.field_defs()}
    fm_path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                           "config", "field_map.json")
    with open(fm_path) as f:
        mapped = set(json.load(f)["client_repo"].values())
    assert columns == mapped, f"missing in setup: {mapped - columns}; extra: {columns - mapped}"


def test_select_options_cover_every_value_the_classifier_can_emit():
    """Airtable rejects unknown single-select values — the dropdowns must be complete."""
    spec = importlib.util.spec_from_file_location(
        "setup_client_repo",
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "setup_client_repo.py"))
    setup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup)
    by_name = {f["name"]: f for f in setup.field_defs()}

    def choices(col):
        return {c["name"] for c in by_name[col]["options"]["choices"]}

    assert set(TAX["venture_classes"]["order"]) | {TAX["venture_classes"]["fallback"]} \
        <= choices("Venture Class")
    assert set(TAX["macro_industries"]["order"]) | {TAX["macro_industries"]["fallback"]} \
        <= choices("Macro Industry")
    assert set(TAX["funding_stages"]["order"]) | {vc.UNCLASSIFIED_STAGE} \
        <= choices("Funding Stage")
    assert set(vc.CONFIDENCE_ORDER) <= choices("Venture Confidence")


def test_split_report_renders():
    mod = _load_module()
    rows = mod.build_repository(_COMPANIES, TAX, overrides={})
    report = mod.split_report(rows)
    assert "By venture class" in report
    assert "Funding-raised ventures" in report
    assert "By macro industry" in report
    assert "Funded Venture" in report


def test_split_report_handles_empty():
    assert "No clients matched" in _load_module().split_report([])


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            passed += 1
            print(f"  ok  {name}")
    print(f"\nPASS — {passed} test(s)")
