"""
Hot Pipeline daily digest — emailed to management (Ayush + Vedant).

Lists every company that has at least one contact sitting in a hot
pipeline stage, grouped by stage:

    Activation
    Discovery Call Booked
    MQL
    SQL

Each row:  Company Name  |  Source  |  Industry

Data is read straight from Airtable (Contacts + Companies), so it reflects
the full synced dataset — a true daily snapshot, not just today's changes.

Recipients: config/team.json  ->  hot_pipeline_to  (falls back to cc).
"""
import argparse
import json
import os
import smtplib
import sys
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEAM_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "team.json")

# Display order + label mapping for the hot stages
ORDER = ["Activation", "Discovery Call Booked", "MQL", "SQL"]

# Maps every Airtable "Pipeline Stage" spelling -> display label
_STAGE_LABEL = {
    "activation":                       "Activation",
    "discovery call booked":            "Discovery Call Booked",
    "mql (marketing qualified lead)":   "MQL",
    "mql":                              "MQL",
    "sql (sales qualified lead)":       "SQL",
    "sql":                              "SQL",
}

# Stage spellings to pull from Airtable (server-side filter)
_FILTER_STAGES = [
    "Activation",
    "Discovery Call Booked",
    "MQL (Marketing Qualified Lead)",
    "MQL",
    "SQL (Sales Qualified Lead)",
    "SQL",
]


def _friendly_date(d: date = None) -> str:
    d = d or date.today()
    return f"{d.strftime('%B')} {d.day}"


def _tr(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _load_company_map():
    """
    Returns two dicts from the Companies table:
      by_kid:  {kylas_company_id  -> {name, industry, source}}
      by_atid: {airtable_record_id -> {name, industry, source}}

    Having both means we can resolve a contact's company even when
    its Kylas Company Id text field is blank, as long as the linked
    record field ("Company") is set — which the sync always fills.
    """
    from utils.airtable_client import AirtableClient
    with open(os.path.join(os.path.dirname(os.path.dirname(__file__)),
                           "config", "field_map.json")) as fh:
        fm = json.load(fh)["company_crm"]

    rows    = AirtableClient("Companies").table.all()
    by_kid  = {}
    by_atid = {}
    for r in rows:
        f    = r["fields"]
        info = {
            "name":     f.get(fm["name"], ""),
            "industry": f.get(fm["industry"], ""),
            "source":   f.get(fm.get("sourceOfData", "Source of Data"), ""),
        }
        kid = str(f.get(fm["id"], "")).strip()
        if kid:
            by_kid[kid] = info
        by_atid[r["id"]] = info   # always index by Airtable record ID
    return by_kid, by_atid, fm


def _collect_hot(by_kid: dict, by_atid: dict):
    """Return {label: [ {name, source, industry}, ... ]} grouped + deduped by company."""
    from utils.airtable_client import AirtableClient
    with open(os.path.join(os.path.dirname(os.path.dirname(__file__)),
                           "config", "field_map.json")) as fh:
        cfm = json.load(fh)["contact"]

    clauses = ",".join("{%s}='%s'" % (cfm["pipelineStage"], s) for s in _FILTER_STAGES)
    formula = f"OR({clauses})"

    try:
        rows = AirtableClient("Contacts").table.all(formula=formula)
    except Exception:
        rows = AirtableClient("Contacts").table.all()

    groups = {lbl: {} for lbl in ORDER}
    for r in rows:
        f     = r["fields"]
        stage = str(f.get(cfm["pipelineStage"], "")).strip()
        label = _STAGE_LABEL.get(stage.lower())
        if not label:
            continue

        # Resolve company via Kylas Company Id first (fast path)
        kid = str(f.get(cfm["companyId"], "")).strip()
        co  = by_kid.get(kid, {})

        # Fallback: use the Airtable linked record field ("Company")
        if not co.get("name"):
            linked = f.get(cfm["companyLink"], [])   # list of Airtable record IDs
            if linked and isinstance(linked, list):
                co = by_atid.get(linked[0], {})

        # If still no company, skip the row — we only want company-level rows
        if not co.get("name"):
            continue

        name     = co["name"]
        industry = co.get("industry", "")
        source   = co.get("source", "") or f.get(cfm["source"], "")

        # Dedupe per company per stage; use Airtable linked ID or kid as key
        linked = f.get(cfm["companyLink"], [])
        key    = (linked[0] if linked else None) or kid or name.lower()

        if key not in groups[label]:
            groups[label][key] = {
                "name": name, "source": source, "industry": industry,
            }

    return {lbl: sorted(v.values(), key=lambda x: x["name"].lower())
            for lbl, v in groups.items()}


_TH  = ('style="background:#f2f2f2;text-align:left;padding:8px 14px;'
        'border:1px solid #cccccc;font-size:13px;"')
_TD  = 'style="padding:8px 14px;border:1px solid #cccccc;font-size:13px;"'
_TABLE = 'style="border-collapse:collapse;width:100%;margin:6px 0 20px;"'
_STAGE_HDR = ('style="font-size:14px;font-weight:bold;margin:20px 0 4px;'
              'color:#333333;"')


def _build_body(groups: dict, friendly: str) -> str:
    total = sum(len(v) for v in groups.values())
    body_style = (
        'style="font-family:Arial,sans-serif;color:#333333;'
        'max-width:660px;margin:0 auto;padding:24px 20px;"'
    )

    if total == 0:
        return (
            f'<!DOCTYPE html><html><body {body_style}>'
            '<p>Hi Ayush &amp; Vedant,</p>'
            f'<p style="font-weight:bold;font-size:14px;">Hot Pipeline Snapshot &nbsp;&middot;&nbsp; {friendly}</p>'
            '<p style="color:#666;">No companies in hot stages right now.</p>'
            '<p style="color:#999;font-size:12px;margin-top:24px;">— Kylas Sync</p>'
            '</body></html>'
        )

    sections = ""
    for label in ORDER:
        items = groups.get(label, [])
        count = len(items)
        sections += f'<p {_STAGE_HDR}>{label} ({count})</p>'
        if not items:
            sections += '<p style="color:#888;font-size:13px;margin:0 0 16px;">—</p>'
            continue
        rows = "".join(
            f'<tr>'
            f'<td {_TD}>{_tr(it["name"], 40)}</td>'
            f'<td {_TD}>{_tr(it["source"], 24)}</td>'
            f'<td {_TD}>{_tr(it["industry"], 24)}</td>'
            f'</tr>'
            for it in items
        )
        sections += (
            f'<table {_TABLE}><thead><tr>'
            f'<th {_TH}>Company</th><th {_TH}>Source</th><th {_TH}>Industry</th>'
            f'</tr></thead><tbody>{rows}</tbody></table>'
        )

    return (
        f'<!DOCTYPE html><html><body {body_style}>'
        '<p>Hi Ayush &amp; Vedant,</p>'
        f'<p style="font-weight:bold;font-size:14px;margin:0 0 4px;">'
        f'Hot Pipeline Snapshot &nbsp;&middot;&nbsp; {friendly}</p>'
        '<p style="font-size:13px;color:#666;margin:0 0 16px;">'
        'Companies with a contact in: Activation, Discovery Call Booked, MQL, SQL</p>'
        + sections
        + f'<p style="font-size:13px;color:#555;margin:8px 0 24px;">Total: {total} companies</p>'
        '<p style="color:#999;font-size:12px;">— Kylas Sync</p>'
        '</body></html>'
    )


def _recipients() -> list:
    with open(TEAM_PATH) as fh:
        cfg = json.load(fh)
    return cfg.get("hot_pipeline_to") or cfg.get("cc", [])


def run(to_override: list = None):
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    if not smtp_user or not smtp_pass:
        print("[Hot Pipeline] SMTP_USER / SMTP_PASS not set — skipping")
        return

    try:
        by_kid, by_atid, _ = _load_company_map()
        groups = _collect_hot(by_kid, by_atid)
    except Exception as exc:
        print(f"[Hot Pipeline] WARNING: could not read Airtable — {exc}")
        return

    friendly = _friendly_date()
    body     = _build_body(groups, friendly)
    subject  = f"Hot Pipeline | {friendly}"
    to_list  = to_override or _recipients()

    if not to_list:
        print("[Hot Pipeline] No recipients configured — skipping")
        return

    msg            = MIMEMultipart("alternative")
    msg["From"]    = smtp_user
    msg["To"]      = ", ".join(to_list)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "html", "utf-8"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as s:
            s.ehlo(); s.starttls()
            s.login(smtp_user, smtp_pass)
            s.sendmail(smtp_user, to_list, msg.as_string())
        total = sum(len(v) for v in groups.values())
        print(f"[Hot Pipeline] Sent → {', '.join(to_list)}  ({total} companies)")
    except Exception as exc:
        print(f"[Hot Pipeline] WARNING: send failed — {exc}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--to", nargs="+", metavar="EMAIL",
                        help="Override recipients (default: team.json hot_pipeline_to)")
    args = parser.parse_args()
    from dotenv import load_dotenv; load_dotenv()
    run(to_override=args.to)
