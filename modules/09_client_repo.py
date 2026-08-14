"""
Client Repository — the sales-intelligence view of Enout's actual clients.

Everything else in this repo tracks the *pipeline*. This module tracks the
*book*: the companies that have actually run an offsite with Enout, split
across ventures and macro industries so you can answer questions like "how much
of our book is Series A/B SaaS?" or "which macro industries have we never sold
into?".

  Airtable column         Source                       Meaning
  ────────────────────────────────────────────────────────────────────────────
  Client Name             Kylas / Company List         Company name
  Venture Class           venture_classifier           Funded Venture / Listed /
                                                       MNC-GCC / Agency /
                                                       Enterprise / Bootstrapped /
                                                       Public Sector / Non-profit
  Funded Venture          derived from Venture Class   Checkbox — the funded cut
  Funding Stage           round type, normalised       Pre-Seed … Series E+ / PE
  Round Size (USD)        round size, FX-normalised    Comparable cheque size
  Macro Industry          raw industry, collapsed      One of ~16 buckets
  Client Basis            client_signals               Why this counts as a client
  Venture/Industry Basis  classifier                   Audit trail for the verdict

A company enters the repository when an offsite actually happened — Account
Status "Offsite Done", a contact at an Offsite Done stage, or a won deal. The
exact signals are configurable in `config/venture_taxonomy.json`; nothing about
the taxonomy is hardcoded here.

Sources:
  --source airtable  (default) Read Company List from the Company Database base.
                     Account Status is already computed there by module 06 and
                     the sync runs twice daily, so this needs no Kylas calls.
  --source kylas     Pull companies (and their contact stages) straight from
                     Kylas. Slower, but independent of the Airtable sync state.

Usage:
  python modules/09_client_repo.py --dry-run          # classify + report, no writes
  python modules/09_client_repo.py --test             # first 5 clients only
  python modules/09_client_repo.py --source kylas
  python modules/09_client_repo.py --report-only      # just the split, no Airtable
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import venture_classifier as vc

TABLE_NAME = "Client Repository"

_FM = None


def _fm() -> dict:
    global _FM
    if _FM is None:
        p = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "field_map.json")
        with open(p) as f:
            _FM = json.load(f)["client_repo"]
    return _FM


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items()
            if v is not None and (v != "" if isinstance(v, str) else True)}


# ----------------------------------------------------------------------
# Sources — both return the same normalised company shape
# ----------------------------------------------------------------------

def _normalised(cid, name, industry, account_status, owner, batch, source_of_data,
                description="", contact_stages=None, deal_stages=None, extra=None) -> dict:
    record = {
        "id":              str(cid or "").strip(),
        "name":            name or "",
        "industry":        industry or "",
        "description":     description or "",
        "account_status":  account_status or "",
        "owner":           owner or "",
        "batch":           batch or "",
        "source_of_data":  source_of_data or "",
        "contact_stages":  contact_stages or [],
        "deal_stages":     deal_stages or [],
    }
    if extra:
        record.update(extra)
    return record


def _select_name(value) -> str:
    """Airtable single-selects come back as dicts via the REST API in places."""
    if isinstance(value, dict):
        return value.get("name", "")
    if isinstance(value, list):
        return ", ".join(_select_name(v) for v in value)
    return value or ""


def from_airtable(base_id: str = None) -> list:
    """Read Company List (Company Database base) and normalise it."""
    from utils.airtable_client import AirtableClient

    with open(os.path.join(os.path.dirname(os.path.dirname(__file__)),
                           "config", "field_map.json")) as fh:
        maps = json.load(fh)
    fm = maps["company"]
    # Company List has no rolled-up contact-stage column of its own; the CRM
    # Companies map names one. Use it when present — it is an optional client
    # signal, and Account Status carries the detection on its own.
    stage_col = maps["company_crm"].get("contactStages", "")

    base = base_id or os.environ.get("AIRTABLE_COMPANY_BASE_ID") or os.environ["AIRTABLE_BASE_ID"]
    client = AirtableClient("Company List", base_id=base)

    records = client.table.all()
    out = []
    for r in records:
        f = r["fields"]
        stages = f.get(stage_col) if stage_col else None
        out.append(_normalised(
            cid=f.get(fm["id"]),
            name=f.get(fm["name"]),
            industry=_select_name(f.get(fm["industry"])),
            account_status=_select_name(f.get(fm["accountStatus"])),
            owner=_select_name(f.get(fm["assignedTo"])),
            batch=_select_name(f.get(fm["batch"])),
            source_of_data=_select_name(f.get(fm["sourceOfData"])),
            contact_stages=[_select_name(s) for s in (stages or [])],
            # Any funding columns you add to Company List are picked up by the
            # classifier's signal_fields lookup, so pass the raw row through.
            extra={k: v for k, v in f.items() if k not in (fm["id"], fm["name"])},
        ))
    return out


def from_kylas(kylas=None) -> list:
    """Pull companies from Kylas, attaching contact stages for client detection."""
    from utils.kylas_client import KylasClient
    from utils.bd_metrics import company_info, contact_stage

    kylas = kylas or KylasClient()
    companies = kylas.get_companies()
    contacts = kylas.get_contacts()

    stages_by_company = defaultdict(list)
    for ct in contacts:
        cid, _ = company_info(ct)
        stage = contact_stage(ct)
        if cid and stage:
            stages_by_company[cid].append(stage)

    out = []
    for raw in companies:
        cf = raw.get("customFieldValues") or {}
        industry = raw.get("industry") or {}
        owner = raw.get("ownedBy") or raw.get("assignedTo") or {}
        health = cf.get("cfAccountHealthBd")
        out.append(_normalised(
            cid=raw.get("id"),
            name=raw.get("name"),
            industry=industry.get("name") if isinstance(industry, dict) else industry,
            account_status=health.get("name") if isinstance(health, dict) else health,
            owner=owner.get("name") if isinstance(owner, dict) else owner,
            batch=cf.get("cfBatch"),
            source_of_data=cf.get("cfSourceOfData"),
            description=raw.get("description") or "",
            contact_stages=stages_by_company.get(str(raw.get("id")), []),
            extra={"customFieldValues": cf},
        ))
    return out


# ----------------------------------------------------------------------
# Build
# ----------------------------------------------------------------------

def build_repository(companies: list, taxonomy: dict = None, overrides: dict = None) -> list:
    """Filter to clients, classify each, and return repository rows."""
    tax = taxonomy or vc.load_taxonomy()
    ovr = overrides if overrides is not None else vc.load_overrides()

    rows = []
    for company in companies:
        client, basis = vc.is_client(company, tax)
        if not client:
            continue
        profile = vc.classify(company, tax, ovr)
        rows.append({
            "id":            company["id"],
            "name":          company["name"],
            "client_basis":  basis,
            "account_status": company.get("account_status", ""),
            "owner":         company.get("owner", ""),
            "batch":         company.get("batch", ""),
            "source_of_data": company.get("source_of_data", ""),
            **profile,
        })
    rows.sort(key=lambda r: (r["venture_class"], r["macro_industry"], r["name"].lower()))
    return rows


def _fields_for(row: dict) -> dict:
    fm = _fm()
    return _clean({
        fm["id"]:                str(row["id"]),
        fm["name"]:              row["name"],
        fm["ventureClass"]:      row["venture_class"],
        fm["isFunded"]:          bool(row["is_funded"]),
        fm["fundingStage"]:      row["funding_stage"],
        fm["roundSizeUsd"]:      row["round_size_usd"],
        fm["roundSizeDisplay"]:  row["round_size_display"],
        fm["investors"]:         row["investors"],
        fm["fundingDate"]:       row["funding_date"],
        fm["employeeCount"]:     row["employee_count"],
        fm["macroIndustry"]:     row["macro_industry"],
        fm["rawIndustry"]:       row["raw_industry"],
        fm["accountStatus"]:     row["account_status"],
        fm["owner"]:             row["owner"],
        fm["batch"]:             row["batch"],
        fm["sourceOfData"]:      row["source_of_data"],
        fm["clientBasis"]:       row["client_basis"],
        fm["ventureConfidence"]: row["venture_confidence"],
        fm["ventureBasis"]:      row["venture_basis"],
        fm["industryBasis"]:     row["industry_basis"],
        fm["notes"]:             row["override_notes"],
        fm["classifiedAt"]:      datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })


def push(rows: list, base_id: str = None) -> dict:
    """Upsert repository rows into the Client Repository table."""
    from utils.airtable_client import AirtableClient

    fm = _fm()
    base = base_id or os.environ.get("AIRTABLE_COMPANY_BASE_ID") or os.environ["AIRTABLE_BASE_ID"]
    client = AirtableClient(TABLE_NAME, base_id=base)
    cached = client.build_cache(fm["id"])
    print(f"[client_repo] cached {cached} existing row(s) in {TABLE_NAME}")

    stats = Counter()
    for row in rows:
        # No timestamp to diff against — classification is cheap and can change
        # whenever the taxonomy does, so always write.
        action, _ = client.upsert(fm["id"], str(row["id"]), _fields_for(row), "", updated_at_field="")
        stats[action] += 1
    client.flush()
    return dict(stats)


# ----------------------------------------------------------------------
# Reporting — the venture / macro-industry split
# ----------------------------------------------------------------------

def split_report(rows: list) -> str:
    """Text report: clients by venture class, funding stage, and macro industry."""
    if not rows:
        return "No clients matched the client_signals in config/venture_taxonomy.json."

    lines = [f"Enout client repository — {len(rows)} client(s)", ""]

    by_class = Counter(r["venture_class"] for r in rows)
    lines.append("By venture class")
    lines.append("-" * 52)
    for name, count in by_class.most_common():
        lines.append(f"  {name:<34} {count:>4}  ({count / len(rows):.0%})")

    funded = [r for r in rows if r["is_funded"]]
    lines += ["", f"Funding-raised ventures: {len(funded)} of {len(rows)} ({len(funded) / len(rows):.0%})"]
    if funded:
        lines.append("-" * 52)
        by_stage = Counter(r["funding_stage"] for r in funded)
        for stage, count in by_stage.most_common():
            sized = [r["round_size_usd"] for r in funded
                     if r["funding_stage"] == stage and r["round_size_usd"]]
            median = ""
            if sized:
                sized.sort()
                median = f"   median {vc.format_usd(sized[len(sized) // 2])}"
            lines.append(f"  {stage:<34} {count:>4}{median}")

    lines += ["", "By macro industry"]
    lines.append("-" * 52)
    for name, count in Counter(r["macro_industry"] for r in rows).most_common():
        lines.append(f"  {name:<34} {count:>4}  ({count / len(rows):.0%})")

    lines += ["", "Funded ventures by macro industry"]
    lines.append("-" * 52)
    if funded:
        for name, count in Counter(r["macro_industry"] for r in funded).most_common():
            lines.append(f"  {name:<34} {count:>4}")
    else:
        lines.append("  (none)")

    weak = [r for r in rows if r["venture_confidence"] in ("None", "Low")]
    if weak:
        lines += ["", f"Low-confidence classifications: {len(weak)} — add them to "
                      f"config/venture_overrides.json to pin them down"]
        for r in weak[:15]:
            lines.append(f"  {r['name'][:38]:<40} {r['venture_class']} ({r['venture_basis']})")
        if len(weak) > 15:
            lines.append(f"  ... and {len(weak) - 15} more")

    return "\n".join(lines)


# ----------------------------------------------------------------------
# Entrypoints
# ----------------------------------------------------------------------

def run(source: str = "airtable", test_mode: bool = False, dry_run: bool = False,
        report_only: bool = False, kylas=None) -> dict:
    companies = from_kylas(kylas) if source == "kylas" else from_airtable()
    print(f"[client_repo] {len(companies)} company record(s) from {source}")

    rows = build_repository(companies)
    print(f"[client_repo] {len(rows)} of them are clients")

    if test_mode:
        rows = rows[:5]
        print("[TEST MODE] limited to first 5 clients")

    report = split_report(rows)
    print("\n" + report + "\n")

    stats = {}
    if not (dry_run or report_only):
        stats = push(rows)
        print(f"[client_repo] Airtable: {stats}")
    else:
        print("[client_repo] no writes (dry-run/report-only)")

    return {"clients": len(rows), "rows": rows, "report": report, "airtable": stats}


def main():
    parser = argparse.ArgumentParser(description="Build the Enout client repository")
    parser.add_argument("--source", choices=["airtable", "kylas"], default="airtable")
    parser.add_argument("--dry-run", action="store_true", help="Classify and report, write nothing")
    parser.add_argument("--report-only", action="store_true", help="Print the split only")
    parser.add_argument("--test", action="store_true", help="Process only the first 5 clients")
    parser.add_argument("--json", metavar="PATH", help="Also dump the classified rows as JSON")
    args = parser.parse_args()

    from dotenv import load_dotenv
    load_dotenv()

    result = run(source=args.source, test_mode=args.test,
                 dry_run=args.dry_run, report_only=args.report_only)

    if args.json:
        with open(args.json, "w") as f:
            json.dump(result["rows"], f, indent=2)
        print(f"[client_repo] wrote {len(result['rows'])} row(s) to {args.json}")


if __name__ == "__main__":
    main()
