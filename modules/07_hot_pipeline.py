"""
Hot Pipeline daily digest — emailed to management (Ayush + Vedant).

Lists every company that has at least one contact sitting in a hot
pipeline stage, grouped by stage:

    Activation
    Discovery Call Booked
    MQL
    SQL

Each row:  Company Name  |  Source  |  Industry  |  Offsite Timeline

Data is read straight from the Companies table — each company is grouped by
the pipeline stage(s) of its linked contacts (the "Pipeline Stage (from
Contacts 2)" lookup). Reading from Companies guarantees the COMPANY name is
shown (a contact name can never leak in).

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

from utils.redact import mask_emails

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

def _friendly_date(d: date = None) -> str:
    d = d or date.today()
    return f"{d.strftime('%B')} {d.day}"


def _tr(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _val(x) -> str:
    """Coerce an Airtable cell (string / number / list / dict) to a clean string."""
    if x is None:
        return ""
    if isinstance(x, list):
        parts = []
        for i in x:
            if isinstance(i, dict):
                parts.append(str(i.get("name") or i.get("value") or "").strip())
            else:
                parts.append(str(i).strip())
        return ", ".join(p for p in parts if p)
    if isinstance(x, dict):
        return str(x.get("name") or x.get("value") or "").strip()
    return str(x).strip()


def _pick_field(all_keys: set, configured: str, *needle_sets) -> str:
    """
    Return the best-matching field name actually present in the table.
    Tries the configured name first, then any key containing ALL needles
    of any needle_set (case-insensitive).
    """
    if configured and configured in all_keys:
        return configured
    for needles in needle_sets:
        for k in all_keys:
            kl = k.lower()
            if all(n in kl for n in needles):
                return k
    return ""


def _collect_hot():
    """
    Read the Companies table and group companies by the hot pipeline stage(s)
    of their linked contacts (via the "Pipeline Stage (from Contacts 2)"
    lookup). Returns {label: [ {name, source, industry, offsite}, ... ]}.

    Reading from Companies (not Contacts) guarantees we always show the
    COMPANY name — a contact name can never leak in.
    """
    from utils.airtable_client import AirtableClient
    with open(os.path.join(os.path.dirname(os.path.dirname(__file__)),
                           "config", "field_map.json")) as fh:
        fm = json.load(fh)["company_crm"]

    rows = AirtableClient("Companies").table.all()

    all_keys = set()
    for r in rows:
        all_keys.update(r["fields"].keys())

    # Detect the lookup field that carries the linked contacts' pipeline stages.
    # Must contain "contact" so we don't grab the company's own "Pipeline Stage BD".
    stage_field   = _pick_field(all_keys, fm.get("contactStages"),
                                ("pipeline stage", "contact"))
    source_field  = _pick_field(all_keys, fm.get("sourceOfData"),
                                ("source",))
    industry_field = _pick_field(all_keys, fm.get("industry"),
                                 ("industry",))
    offsite_field = _pick_field(all_keys, fm.get("offsiteTimeline"),
                                ("offsite",))
    # Name: prefer "Company Name"; never fall back to an Id/link field
    name_field    = _pick_field(all_keys, fm["name"], ("company name",), ("name",))

    print(f"[Hot Pipeline] Fields → stage='{stage_field}' source='{source_field}' "
          f"industry='{industry_field}' offsite='{offsite_field}' name='{name_field}'")

    if not stage_field:
        print("[Hot Pipeline] WARNING: no 'Pipeline Stage (from Contacts ...)' "
              "lookup field found on Companies — nothing to report")
        return {lbl: [] for lbl in ORDER}

    groups = {lbl: {} for lbl in ORDER}
    for r in rows:
        f      = r["fields"]
        stages = f.get(stage_field)
        if not stages:
            continue
        stages = stages if isinstance(stages, list) else [stages]

        labels = set()
        for s in stages:
            lbl = _STAGE_LABEL.get(_val(s).lower())
            if lbl:
                labels.add(lbl)
        if not labels:
            continue

        name = _val(f.get(name_field))
        if not name:
            continue

        info = {
            "name":     name,
            "source":   _val(f.get(source_field)),
            "industry": _val(f.get(industry_field)),
            "offsite":  _val(f.get(offsite_field)),
        }
        for lbl in labels:
            groups[lbl][r["id"]] = info   # dedupe: one company once per stage

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
            f'<td {_TD}>{_tr(it["source"], 22)}</td>'
            f'<td {_TD}>{_tr(it["industry"], 22)}</td>'
            f'<td {_TD}>{_tr(it.get("offsite", ""), 22) or "—"}</td>'
            f'</tr>'
            for it in items
        )
        sections += (
            f'<table {_TABLE}><thead><tr>'
            f'<th {_TH}>Company</th><th {_TH}>Source</th>'
            f'<th {_TH}>Industry</th><th {_TH}>Offsite Timeline (BD - New)</th>'
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
        + f'<p style="font-size:13px;color:#555;margin:8px 0 24px;">'
          f'Total: {total} company rows across stages</p>'
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
        groups = _collect_hot()
    except Exception as exc:
        print(f"[Hot Pipeline] WARNING: could not read Airtable — {exc}")
        return

    # Diagnostic: print the company names being reported, per stage.
    # If these are company names, the email is correct; if they look like
    # people, the Companies table's "Company Name" field holds person names.
    for lbl in ORDER:
        items  = groups.get(lbl, [])
        sample = " | ".join(i["name"] for i in items[:3])
        print(f"[Hot Pipeline] {lbl}: {len(items)} companies   e.g. {sample}")

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
        print(f"[Hot Pipeline] Sent → {mask_emails(to_list)}  ({total} company rows)")
    except Exception as exc:
        print(f"[Hot Pipeline] WARNING: send failed — {exc}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--to", nargs="+", metavar="EMAIL",
                        help="Override recipients (default: team.json hot_pipeline_to)")
    args = parser.parse_args()
    from dotenv import load_dotenv; load_dotenv()
    run(to_override=args.to)
