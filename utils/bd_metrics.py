"""Shared BD stage classification — imported by contact sync, BD stats, and backfill."""

_PIPELINE_STAGE = {
    2862826: "Yet to Be Mined",
    2862827: "CNC (Could Not Connect) - 1",
    2862828: "MQL (Marketing Qualified Lead)",
    2862829: "Activation",
    2862831: "Not Interested",
    2864173: "Yet to Be Mined",
    2864175: "Invalid Contact",
    2867816: "CNC (Could Not Connect) - 2",
    2867817: "MQL (Marketing Qualified Lead)",
    2870484: "SQL (Sales Qualified Lead)",
    2870485: "Not a Decision Maker (NDM)",
    2873316: "Follow-up (1)",
    2873317: "Follow-up (2)",
    2873318: "Follow-up (3)",
    2873321: "POC - Organisation - Changed",
    2873487: "Followup - CNC",
    2909379: "Discovery Call Booked",
    2909380: "Reschedule Pending",
    2909381: "Closing Loops - Low Value",
    2909382: "Discovery Call No-Show",
    2909383: "Offsite Delayed",
    2910918: "Discovery Call Done - Awaiting Client Inputs",
}

BD_KEYS = ["attempted", "connected", "dcb", "sql", "mql", "activation"]

CONNECTED_STAGES = {
    "MQL (Marketing Qualified Lead)",
    "SQL (Sales Qualified Lead)",
    "Activation",
    "Invalid Contact",
    "Connect Later",
    "Disqualified - Wrong POC",
    "Not a Decision Maker (NDM)",
    "Not Interested",
    "Follow-up (1)",
    "Follow-up (2)",
    "Follow-up (3)",
    "Discovery Call Booked",
}

DCB_STAGES = {
    "SQL (Sales Qualified Lead)",
    "Discovery Call Booked",
    "Offsite Delayed",
    "Discovery Call No-Show",
    "Reschedule Pending",
    "Closing Loops - Low Value",
    "Discovery Call Done - Awaiting Client Inputs",
}

SQL_STAGES        = {"SQL (Sales Qualified Lead)"}
MQL_STAGES        = {"MQL (Marketing Qualified Lead)"}
ACTIVATION_STAGES = {"Activation"}
ATTEMPTED_EXCLUDE = {"Yet to Be Mined", ""}


def refresh_stage_map(kylas) -> int:
    """Merge the live contact Pipeline Stage picklist into _PIPELINE_STAGE.

    The contact search API returns bare option ids for most contacts, so any
    id missing from the static map silently resolves to no stage (and the
    company falls through to Active/Fresh in account health). Pulling the
    live picklist means new/renamed options keep resolving. Returns how many
    ids were added; safe no-op on API failure.
    """
    added = 0
    try:
        defs = kylas.get_custom_field_defs("contact")
        key  = "cfPipelineStageBd"
        if key not in defs:
            key = kylas.cf_key_for_display("contact", "Pipeline Stage - BD") or key
        labels = (defs.get(key) or {}).get("labels") or {}
        for oid, label in labels.items():
            try:
                oid = int(oid)
            except (TypeError, ValueError):
                continue
            if oid not in _PIPELINE_STAGE and str(label).strip():
                _PIPELINE_STAGE[oid] = str(label).strip()
                added += 1
        if added:
            print(f"[bd_metrics] stage map: +{added} option id(s) from live picklist")
    except Exception as exc:
        print(f"[bd_metrics] WARN: live stage-picklist fetch failed ({exc}) — using static map")
    return added


def contact_stage(raw: dict) -> str:
    """Resolve pipeline stage ID/object to a name string."""
    psd = (raw.get("customFieldValues") or {}).get("cfPipelineStageBd")
    if isinstance(psd, dict):
        name = psd.get("name", "")
        if name:
            return name
        psd = psd.get("id")           # id-only dict → fall through to map lookup
        if psd is None:
            return ""
    if psd is not None:
        try:
            return _PIPELINE_STAGE.get(int(psd), str(psd))
        except (TypeError, ValueError):
            return str(psd)
    return ""


def classify_bd(stage: str) -> dict:
    """Return which BD metric categories a pipeline stage belongs to."""
    return {
        "attempted":  stage not in ATTEMPTED_EXCLUDE,
        "connected":  stage in CONNECTED_STAGES,
        "dcb":        stage in DCB_STAGES,
        "sql":        stage in SQL_STAGES,
        "mql":        stage in MQL_STAGES,
        "activation": stage in ACTIVATION_STAGES,
    }


def company_info(raw: dict) -> tuple:
    """Extract (kylas_company_id, company_name) from a contact's raw data."""
    co = raw.get("company")
    if isinstance(co, (int, float)):
        return str(int(co)), ""
    if isinstance(co, dict):
        return str(co.get("id", "")), co.get("name", "")
    return "", ""
