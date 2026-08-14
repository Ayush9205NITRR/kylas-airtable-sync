"""Venture + macro-industry classification for the Enout client repository.

Pure functions, no network and no Airtable/Kylas imports — everything here is
driven by ``config/venture_taxonomy.json`` so the taxonomy can be tuned without
touching Python. ``modules/09_client_repo.py`` is the only caller.

The two questions this answers for every client company:

  * **Which venture bucket is it?** Funded Venture / Listed / MNC-GCC /
    Agency / Enterprise / Bootstrapped / Public Sector / Non-profit. Funded
    ventures additionally carry a normalised funding stage and a round size in
    USD, so "funding raised ventures" can be sliced by stage and cheque size.
  * **Which macro industry is it?** The raw Kylas ``Industry`` string is a long
    tail (hundreds of values, inconsistently typed); this collapses it to the
    ~16 buckets in the taxonomy.

Every verdict comes back with a confidence and a human-readable basis, so a row
in the repository can always be traced to the evidence that produced it:

    >>> profile = classify({"name": "Acme Labs", "industry": "SaaS",
    ...                     "round_type": "Series A"})
    >>> profile["venture_class"], profile["venture_confidence"]
    ('Funded Venture', 'High')
    >>> profile["venture_basis"]
    "round type 'Series A'"
"""
import json
import os
import re
from typing import Optional, Tuple

_TAXONOMY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "venture_taxonomy.json",
)

_OVERRIDES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "venture_overrides.json",
)

_TAXONOMY = None

# Confidence ladder, weakest first. Used to compare a rule's min_confidence
# against the evidence actually found.
CONFIDENCE_ORDER = ["None", "Low", "Medium", "High"]

UNCLASSIFIED_STAGE = "Unknown"

# Multipliers for the magnitude suffixes that show up in funding copy. Indian
# sources write "40 Cr" / "12 lakh"; western ones write "$5M" / "5 million".
_MAGNITUDES = [
    (r"\b(?:cr|crore|crores)\b", 10_000_000),
    (r"\b(?:lakh|lakhs|lac|lacs)\b", 100_000),
    (r"\b(?:bn|billion|b)\b", 1_000_000_000),
    (r"\b(?:mn|million|m)\b", 1_000_000),
    (r"\b(?:k|thousand)\b", 1_000),
]

# Currency detection: symbol or ISO code anywhere in the string.
_CURRENCY_MARKERS = [
    ("INR", [r"₹", r"\binr\b", r"\brs\.?\b", r"\brupees?\b", r"\bcr\b", r"\bcrore", r"\blakh", r"\blac"]),
    ("EUR", [r"€", r"\beur\b"]),
    ("GBP", [r"£", r"\bgbp\b"]),
    ("AED", [r"\baed\b", r"\bdirham"]),
    ("SGD", [r"\bsgd\b", r"\bs\$"]),
    ("USD", [r"\$", r"\busd\b", r"\bdollars?\b"]),
]


def load_taxonomy(path: str = None, force: bool = False) -> dict:
    """Load the taxonomy JSON, memoising the default one.

    An explicit `path` is read fresh and left out of the cache, so tests can
    load a fixture taxonomy without poisoning the module-level default.
    """
    global _TAXONOMY
    if path:
        with open(path) as f:
            return json.load(f)
    if _TAXONOMY is None or force:
        with open(_TAXONOMY_PATH) as f:
            _TAXONOMY = json.load(f)
    return _TAXONOMY


def load_overrides(path: str = None) -> dict:
    """Flatten `config/venture_overrides.json` into one lookup dict.

    The file is split into ``by_id`` (Kylas company id) and ``by_name`` for
    editing convenience; ``classify()`` wants a single dict keyed by either, so
    names are normalised on the way in. A missing file is not an error — the
    override layer is optional.
    """
    target = path or _OVERRIDES_PATH
    if not os.path.exists(target):
        return {}
    with open(target) as f:
        data = json.load(f)

    flat = {}
    for name, entry in (data.get("by_name") or {}).items():
        flat[_norm(name)] = entry
    for cid, entry in (data.get("by_id") or {}).items():
        flat[str(cid).strip()] = entry
    return flat


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------

def _norm(value) -> str:
    """Casefold + collapse whitespace. Non-strings degrade to ''."""
    if value is None:
        return ""
    if isinstance(value, dict):
        value = value.get("name") or value.get("value") or ""
    if isinstance(value, (list, tuple)):
        value = " ".join(str(v) for v in value)
    return re.sub(r"\s+", " ", str(value)).strip().lower()


def _any_keyword(haystack: str, keywords) -> str:
    """Return the first keyword found in haystack, or '' if none match."""
    for kw in keywords or []:
        k = _norm(kw)
        if k and k in haystack:
            return kw
    return ""


def _at_least(found: str, required: str) -> bool:
    """True when confidence `found` is >= `required` on the ladder."""
    try:
        return CONFIDENCE_ORDER.index(found) >= CONFIDENCE_ORDER.index(required)
    except ValueError:
        return False


def pick_signal(record: dict, candidates) -> Tuple[str, str]:
    """First non-empty value among `candidates` keys in `record`.

    Returns ``(value, key)``. Kylas nests its custom fields under
    ``customFieldValues``, so look there too — callers can hand us either a
    flattened dict or a raw Kylas payload.
    """
    cf = record.get("customFieldValues") or {}
    for key in candidates or []:
        for source in (record, cf):
            if key in source:
                raw = source[key]
                if isinstance(raw, dict):
                    raw = raw.get("name") or raw.get("value") or ""
                if raw not in (None, "", [], {}):
                    return (str(raw).strip() if not isinstance(raw, (int, float)) else raw), key
    return "", ""


# ----------------------------------------------------------------------
# Funding
# ----------------------------------------------------------------------

def funding_stage(round_type, taxonomy: dict = None) -> str:
    """Normalise a free-text round descriptor to a taxonomy stage.

    Order matters and comes from the taxonomy: "Pre-Series A" is tested before
    "Series A", which is tested before the bare "Seed" fallback, so
    "Pre-Series A bridge" does not get filed as a Series A.
    """
    tax = taxonomy or load_taxonomy()
    text = _norm(round_type)
    if not text:
        return UNCLASSIFIED_STAGE

    stages = tax["funding_stages"]
    for stage in stages["order"]:
        if _any_keyword(text, stages["keywords"].get(stage, [])):
            return stage
    return UNCLASSIFIED_STAGE


def parse_round_size(raw, taxonomy: dict = None) -> Tuple[Optional[float], str]:
    """Parse a round size into USD.

    Handles the shapes that actually turn up in funding notes: ``$5M``,
    ``5.5 million USD``, ``₹40 Cr``, ``INR 3,50,00,000``, ``Rs 12 lakh``,
    ``€2.5M``. Returns ``(usd_amount, detected_currency)``; ``(None, "")``
    when nothing numeric is present.
    """
    tax = taxonomy or load_taxonomy()
    text = _norm(raw)
    if not text:
        return None, ""

    # Strip digit grouping (both 1,000,000 and the Indian 3,50,00,000).
    cleaned = re.sub(r"(?<=\d),(?=\d)", "", text)
    m = re.search(r"\d+(?:\.\d+)?", cleaned)
    if not m:
        return None, ""
    amount = float(m.group(0))

    # Only look for a magnitude suffix *after* the number, so "Series 2" style
    # noise before it cannot inflate the value.
    tail = cleaned[m.end():]
    for pattern, multiplier in _MAGNITUDES:
        if re.search(pattern, tail):
            amount *= multiplier
            break

    currency = "USD"
    for code, patterns in _CURRENCY_MARKERS:
        if any(re.search(p, cleaned) for p in patterns):
            currency = code
            break

    rate = tax.get("fx", {}).get(currency)
    if currency != "USD" and rate:
        amount /= float(rate)
    return round(amount, 2), currency


def format_usd(amount: Optional[float]) -> str:
    """Compact display string for a USD amount ('$5.0M', '$750K', '')."""
    if amount is None:
        return ""
    if amount >= 1_000_000_000:
        return f"${amount / 1_000_000_000:.1f}B"
    if amount >= 1_000_000:
        return f"${amount / 1_000_000:.1f}M"
    if amount >= 1_000:
        return f"${amount / 1_000:.0f}K"
    return f"${amount:.0f}"


# ----------------------------------------------------------------------
# Macro industry
# ----------------------------------------------------------------------

def macro_industry(record: dict, taxonomy: dict = None) -> Tuple[str, str]:
    """Collapse a raw industry string to a macro bucket.

    Returns ``(bucket, basis)``. An exact match on the raw Kylas industry value
    is preferred — that is the curated path and it beats any keyword hit. Only
    when no bucket claims the raw string do we fall back to scanning the
    company name/description for keywords.
    """
    tax = (taxonomy or load_taxonomy())["macro_industries"]
    raw_industry = _norm(record.get("industry"))
    buckets = tax["buckets"]

    if raw_industry:
        for name in tax["order"]:
            for candidate in buckets.get(name, {}).get("raw", []):
                if _norm(candidate) == raw_industry:
                    return name, f"industry '{record.get('industry')}'"

    haystack = " ".join(filter(None, [
        raw_industry,
        _norm(record.get("name")),
        _norm(record.get("description")),
    ]))
    if haystack:
        for name in tax["order"]:
            hit = _any_keyword(haystack, buckets.get(name, {}).get("keywords", []))
            if hit:
                return name, f"keyword '{hit.strip()}'"

    return tax["fallback"], "no industry signal"


# ----------------------------------------------------------------------
# Venture class
# ----------------------------------------------------------------------

def _funding_evidence(record: dict, taxonomy: dict) -> dict:
    """Pull whatever funding signals the record carries."""
    fields = taxonomy["signal_fields"]
    round_type, rt_key = pick_signal(record, fields.get("round_type"))
    round_size, rs_key = pick_signal(record, fields.get("round_size"))
    investors, _       = pick_signal(record, fields.get("investors"))
    funding_date, _    = pick_signal(record, fields.get("funding_date"))
    headcount, _       = pick_signal(record, fields.get("employee_count"))

    usd, currency = parse_round_size(round_size, taxonomy)
    # Headcount arrives as anything from 240 to "about 200" to "1,200 employees",
    # so take the first integer rather than trusting the whole string.
    if isinstance(headcount, (int, float)):
        headcount = int(headcount)
    else:
        m = re.search(r"\d[\d,]*", str(headcount or ""))
        headcount = int(m.group(0).replace(",", "")) if m else None

    return {
        "round_type": round_type,
        "round_type_key": rt_key,
        "round_size_raw": round_size,
        "round_size_key": rs_key,
        "round_size_usd": usd,
        "round_currency": currency,
        "investors": investors,
        "funding_date": funding_date,
        "employee_count": headcount,
    }


def _text_haystack(record: dict) -> str:
    """Everything a text rule is allowed to look at, as one lowered string."""
    return " ".join(filter(None, [
        _norm(record.get("name")),
        _norm(record.get("description")),
        _norm(record.get("batch")),
        _norm(record.get("source_of_data")),
        _norm(record.get("industry")),
    ]))


def venture_class(record: dict, evidence: dict, taxonomy: dict = None) -> Tuple[str, str, str]:
    """Assign a venture class.

    Returns ``(class_name, confidence, basis)``. Rules are evaluated in the
    taxonomy's declared order and the first match wins, so specific classes
    must sit above catch-alls in the config.

    Evidence strength, strongest first:
      * **High**   — an explicit round type/size field on the record.
      * **Medium** — a funding keyword in the batch / source-of-data / name.
      * **Low**    — a legal-suffix or descriptive keyword ("LLP", "Foundation").
    """
    tax = (taxonomy or load_taxonomy())["venture_classes"]
    rules = tax["rules"]
    haystack = _text_haystack(record)

    for name in tax["order"]:
        rule = rules.get(name, {})
        signal = rule.get("signal", "text")

        if signal == "funding_field":
            if evidence.get("round_type"):
                basis = f"round type '{evidence['round_type']}'"
                confidence = "High"
            elif evidence.get("round_size_usd") is not None:
                basis = f"round size '{evidence['round_size_raw']}'"
                confidence = "High"
            else:
                hit = _any_keyword(haystack, rule.get("keywords"))
                if not hit:
                    continue
                basis = f"keyword '{hit.strip()}'"
                confidence = "Medium"
            if _at_least(confidence, rule.get("min_confidence", "Low")):
                return name, confidence, basis
            continue

        min_emp = rule.get("min_employees")
        if min_emp and (evidence.get("employee_count") or 0) >= min_emp:
            return name, "Medium", f"headcount {evidence['employee_count']}"

        hit = _any_keyword(haystack, rule.get("keywords"))
        if hit:
            return name, "Low", f"keyword '{hit.strip()}'"

    return tax["fallback"], "None", "no venture signal"


# ----------------------------------------------------------------------
# Client detection
# ----------------------------------------------------------------------

def is_client(record: dict, taxonomy: dict = None) -> Tuple[bool, str]:
    """True when the company has actually run an offsite with Enout.

    Returns ``(is_client, basis)``. This is what separates the client
    repository from the prospect pipeline — a company is a client because an
    offsite happened, not because it sits at a hot stage.
    """
    signals = (taxonomy or load_taxonomy())["client_signals"]

    status = _norm(record.get("account_status"))
    for wanted in signals.get("account_status", []):
        if status == _norm(wanted):
            return True, f"account status '{record.get('account_status')}'"

    contact_stages = {_norm(s) for s in (record.get("contact_stages") or [])}
    for wanted in signals.get("contact_stages", []):
        if _norm(wanted) in contact_stages:
            return True, f"contact at '{wanted}'"

    deal_stages = {_norm(s) for s in (record.get("deal_stages") or [])}
    for wanted in signals.get("deal_stages", []):
        if _norm(wanted) in deal_stages:
            return True, f"deal at '{wanted}'"

    return False, ""


# ----------------------------------------------------------------------
# Top-level
# ----------------------------------------------------------------------

def classify(record: dict, taxonomy: dict = None, overrides: dict = None) -> dict:
    """Full sales-intelligence profile for one company.

    ``overrides`` is the manual curation layer (``config/venture_overrides.json``),
    keyed by Kylas company id or by normalised company name. Anything it sets
    replaces the inferred value and is reported at High confidence — hand
    research beats every heuristic in here.
    """
    tax = taxonomy or load_taxonomy()

    override = {}
    if overrides:
        override = (overrides.get(str(record.get("id") or "").strip())
                    or overrides.get(_norm(record.get("name")))
                    or {})

    merged = dict(record)
    merged.update({k: v for k, v in override.items() if k != "notes"})

    evidence = _funding_evidence(merged, tax)
    v_class, v_conf, v_basis = venture_class(merged, evidence, tax)
    industry, i_basis = macro_industry(merged, tax)

    if override.get("venture_class"):
        v_class, v_conf, v_basis = override["venture_class"], "High", "manual override"
    if override.get("macro_industry"):
        industry, i_basis = override["macro_industry"], "manual override"

    stage = funding_stage(evidence["round_type"], tax)
    funded = bool(tax["venture_classes"]["rules"].get(v_class, {}).get("funded"))
    if stage == UNCLASSIFIED_STAGE and not funded:
        stage = "Bootstrapped" if v_class == "Bootstrapped Startup" else UNCLASSIFIED_STAGE

    return {
        "venture_class":       v_class,
        "venture_confidence":  v_conf,
        "venture_basis":       v_basis,
        "is_funded":           funded,
        "funding_stage":       stage,
        "round_size_usd":      evidence["round_size_usd"],
        "round_size_display":  format_usd(evidence["round_size_usd"]),
        "round_currency":      evidence["round_currency"],
        "investors":           evidence["investors"],
        "funding_date":        evidence["funding_date"],
        "employee_count":      evidence["employee_count"],
        "macro_industry":      industry,
        "industry_basis":      i_basis,
        "raw_industry":        record.get("industry") or "",
        "override_notes":      override.get("notes", ""),
    }
