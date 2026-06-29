"""
BD daily email report — clean activity update, no motivation text.

Slots:
  first_half  → 1:30 PM IST run  (11 AM – 1 PM window numbers)
  full_day    → 6:30 PM IST run  (W1 + W2 breakdown + daily total)

Team source (priority):
  1. Airtable 'BD Members' table (Name / Email / Group / Active)
  2. config/team.json bd_team

Target source (priority):
  1. Airtable 'BD Config' table  (Key=daily_attempted, Value=100)
  2. Airtable 'BD Targets' table
  3. config/team.json bd_targets.daily

Monthly fixed targets (Discovery Calls, SQL) shown in every email:
  Airtable 'BD Config' monthly_fixed_dcb / monthly_fixed_sql
  OR config/team.json bd_targets.monthly_fixed
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

METRICS = ["attempted", "connected", "dcb", "sql"]
LABEL   = {
    "attempted": "Attempted",
    "connected": "Connected",
    "dcb":       "Discovery Calls",
    "sql":       "SQL",
}
_TARGET_KEY = {
    "attempted":      "attempted",
    "connected":      "connected",
    "discovery call": "dcb",
    "sql":            "sql",
    "mql":            "mql",
    "activation":     "activation",
}

# ── HTML style constants ──────────────────────────────────────────────────────
_TH  = ('style="background:#f2f2f2;text-align:left;padding:9px 14px;'
        'border:1px solid #cccccc;font-size:13px;"')
_THC = ('style="background:#f2f2f2;text-align:center;padding:9px 14px;'
        'border:1px solid #cccccc;font-size:13px;"')
_TD  = 'style="padding:9px 14px;border:1px solid #cccccc;font-size:13px;"'
_TDC = ('style="text-align:center;padding:9px 14px;'
        'border:1px solid #cccccc;font-size:13px;"')
_TDB = ('style="text-align:center;padding:9px 14px;'
        'border:1px solid #cccccc;font-size:13px;font-weight:bold;"')
_TABLE  = 'style="border-collapse:collapse;width:100%;margin:8px 0 16px;"'
_TABLE2 = 'style="border-collapse:collapse;margin:4px 0 16px;"'


# ── Loaders ───────────────────────────────────────────────────────────────────

def _load_bd_members() -> list:
    """
    Returns [{name, email}] for active BD team members.
    Reads Airtable 'BD Members' as the canonical list, then merges in any
    members from team.json bd_team not already present (added by sync_team.py).
    """
    airtable_members = []
    try:
        from utils.airtable_client import AirtableClient
        rows = AirtableClient("BD Members").table.all()
        airtable_members = [
            {"name": r["fields"]["Name"], "email": r["fields"]["Email"]}
            for r in rows
            if r["fields"].get("Active", True)
            and r["fields"].get("Group", "BD").strip().upper() == "BD"
            and r["fields"].get("Name") and r["fields"].get("Email")
        ]
    except Exception:
        pass

    try:
        with open(TEAM_PATH) as fh:
            cfg = json.load(fh)
        json_members = cfg.get("bd_team", cfg.get("members", []))
    except Exception:
        json_members = []

    if not airtable_members:
        return json_members

    # Airtable wins on conflicts; add team.json members whose email isn't already there
    at_emails = {m["email"].lower() for m in airtable_members}
    extra = [m for m in json_members if m.get("email", "").lower() not in at_emails]
    return airtable_members + extra


def _load_daily_targets() -> dict:
    """
    {metric: daily_target} — same for all members.

    Priority:
      1. Airtable 'BD Config' table  (Key=daily_attempted, Value=100)
      2. Airtable 'BD Targets' table
      3. config/team.json bd_targets.daily
    """
    try:
        from utils.airtable_client import AirtableClient
        rows = AirtableClient("BD Config").table.all()
        out  = {}
        for r in rows:
            key = r["fields"].get("Key", "").strip()
            val = r["fields"].get("Value", 0) or 0
            if key.startswith("daily_"):
                metric = key[len("daily_"):]
                if metric in METRICS and val:
                    out[metric] = int(val)
        if out:
            return out
    except Exception:
        pass
    try:
        from utils.airtable_client import AirtableClient
        rows   = AirtableClient("BD Targets").table.all()
        merged = {}
        for r in rows:
            f     = r["fields"]
            m_raw = f.get("Metric", "").strip().lower()
            m_key = _TARGET_KEY.get(m_raw, "")
            daily = int(f.get("Daily Target", 0) or 0)
            if m_key and daily > 0:
                merged[m_key] = daily
        if merged:
            return merged
    except Exception:
        pass
    try:
        with open(TEAM_PATH) as fh:
            bt = json.load(fh).get("bd_targets", {})
        return {k: v for k, v in bt.get("daily", {}).items() if v}
    except Exception:
        return {}


_STAGE_SHORT = {
    "MQL (Marketing Qualified Lead)":               "MQL",
    "CNC (Could Not Connect) - 1":                  "CNC-1",
    "CNC (Could Not Connect) - 2":                  "CNC-2",
    "SQL (Sales Qualified Lead)":                   "SQL",
    "Discovery Call Booked":                        "DCB",
    "Discovery Call No-Show":                       "DCB-NS",
    "Discovery Call Done - Awaiting Client Inputs": "DCB-Done",
    "Yet to Be Mined":                              "YtBM",
    "Not Interested":                               "NOI",
    "Follow-up (1)":                                "FU-1",
    "Follow-up (2)":                                "FU-2",
    "Follow-up (3)":                                "FU-3",
    "Followup - CNC":                               "FU-CNC",
    "Activation":                                   "Activation",
    "Not a Decision Maker (NDM)":                   "NDM",
    "Invalid Contact":                              "Invalid",
    "Reschedule Pending":                           "Reschedule",
    "Closing Loops - Low Value":                    "Low Value",
    "Offsite Delayed":                              "Delayed",
    "POC - Organisation - Changed":                 "POC-Changed",
}

_STAGE_ORDER = [
    "MQL", "CNC-1", "CNC-2", "SQL", "DCB", "FU-1", "FU-2", "FU-3",
    "FU-CNC", "Activation", "DCB-NS", "DCB-Done", "Reschedule",
    "NDM", "Low Value", "Delayed", "NOI", "YtBM", "Invalid",
    "POC-Changed",
]


def _stages_summary(raw) -> str:
    """
    Compact stage breakdown from a multipleLookupValues list or comma-string.
    Returns e.g. "MQL: 5 · CNC-1: 3 · CNC-2: 2"
    """
    if not raw:
        return "—"
    items = raw if isinstance(raw, list) else [s.strip() for s in str(raw).split(",")]
    counts: dict = {}
    for s in items:
        short = _STAGE_SHORT.get(str(s).strip(), str(s).strip())
        counts[short] = counts.get(short, 0) + 1
    # Sort by predefined order, then alphabetically for unlisted stages
    def _rank(k):
        try:
            return _STAGE_ORDER.index(k)
        except ValueError:
            return len(_STAGE_ORDER)
    parts = [f"{k}: {v}" for k, v in sorted(counts.items(), key=lambda x: _rank(x[0]))]
    return " · ".join(parts) or "—"


def _load_account_activity_today() -> list:
    """
    Reads today's Account Activity Log from Airtable, joins with Companies CRM
    (for contact pipeline stage breakdown, Source of Data, Offsite Timeline).
    Returns rows sorted by Attempted POCs descending (only rows with ≥1 attempt).
    """
    today = date.today().isoformat()
    try:
        from utils.airtable_client import AirtableClient
        acc_rows = AirtableClient("Account Activity Log").table.all(
            formula=f"{{Date}}='{today}'"
        )
        if not acc_rows:
            return []

        def _norm_cid(raw) -> str:
            """Normalise company ID to plain integer string regardless of float/str format."""
            try:
                return str(int(float(raw))) if raw not in (None, "") else ""
            except (ValueError, TypeError):
                return str(raw).strip()

        # Companies CRM — company name (fallback), contact stage lookup, source, offsite
        co_rows = AirtableClient("Companies").table.all(
            fields=[
                "Kylas Company ID",
                "Company Name",
                "Pipeline Stage (from Contacts 2)",
                "Source of Data",
                "Offsite Timeline",
            ]
        )
        co_info: dict = {}
        for r in co_rows:
            f   = r["fields"]
            cid = _norm_cid(f.get("Kylas Company ID"))
            if cid:
                co_info[cid] = {
                    "name":     str(f.get("Company Name")                    or ""),
                    "stages":   f.get("Pipeline Stage (from Contacts 2)")    or [],
                    "source":   str(f.get("Source of Data")                  or ""),
                    "offsite":  str(f.get("Offsite Timeline")                or ""),
                }

        result = []
        for r in acc_rows:
            f         = r["fields"]
            attempted = int(f.get("Attempted POCs", 0) or 0)
            if attempted == 0:
                continue
            cid = _norm_cid(f.get("Kylas Company Id"))
            co  = co_info.get(cid, {})
            result.append({
                "company":   str(f.get("Company Name") or "") or co.get("name", ""),
                "stages":    _stages_summary(co.get("stages", [])),
                "attempted": attempted,
                "connected": int(f.get("Connected POCs", 0) or 0),
                "source":    co.get("source",  ""),
                "offsite":   co.get("offsite", ""),
            })

        result.sort(key=lambda x: x["attempted"], reverse=True)
        return result
    except Exception as exc:
        print(f"[Email] WARNING: could not load account activity: {exc}")
        return []


def _account_table_html(rows: list) -> str:
    if not rows:
        return ""
    hdr = (
        '<p style="font-weight:bold;font-size:14px;margin:24px 0 6px;">'
        'Accounts Tapped Today</p>'
        f'<table {_TABLE}><thead><tr>'
        f'<th {_TH}>Account Name</th>'
        f'<th {_TH}>Pipeline Stage</th>'
        f'<th {_TH}>Offsite Timeline</th>'
        f'<th {_TH}>Source of Data</th>'
        f'</tr></thead><tbody>'
    )
    body = "".join(
        f'<tr>'
        f'<td {_TD}>{r["company"]}</td>'
        f'<td {_TD} style="font-size:12px;">{r["stages"]}</td>'
        f'<td {_TD}>{r["offsite"] or "—"}</td>'
        f'<td {_TD}>{r["source"]  or "—"}</td>'
        f'</tr>'
        for r in rows
    )
    return hdr + body + "</tbody></table>"


# ── Contact validation (conditional required fields) ────────────────────────────
# Kylas can't block a save on a conditional rule, so we surface violations
# reactively in the EOD email. Each rule has a condition + the field(s) that must
# then be filled. A contact is checked against every rule and the missing fields
# are pooled. Conditions:
#   "when_stage": substring matched (case-insensitive) against Pipeline Stage
#   "when_set":   triggers when that field already has a value
# Field kinds:
#   "offsite"  -> the account's Offsite Timeline (Companies table)
#   "nextcall" -> the contact's Next Call Date   (Contacts table)
# Known contact stages: "MQL (Marketing Qualified Lead)", "SQL ...", "Activation",
# "Discovery Call Booked", "Offsite Delayed", "Follow-up (1..3)", "CNC ...".
_VALIDATION_RULES = [
    # Stage = MQL  -> Offsite Timeline + Next Call Date both required.
    {"when_stage": "mql", "require": [("Offsite Timeline", "offsite"),
                                      ("Next Call Date", "nextcall")]},
    # Offsite Timeline field is set -> a Next Call Date must be scheduled.
    {"when_set": "offsite", "require": [("Next Call Date", "nextcall")]},
]


def _norm_company_id(raw) -> str:
    """Company id as a plain integer string, regardless of float/str format."""
    try:
        return str(int(float(raw))) if raw not in (None, "") else ""
    except (ValueError, TypeError):
        return str(raw).strip()


def _load_validation_issues() -> list:
    """Scan Airtable Contacts against _VALIDATION_RULES.

    Returns violation rows {company, contact, owner, stage, missing} for every
    contact whose stage requires fields that are currently empty. Offsite
    Timeline is read from the contact's company (Companies table). Best-effort:
    returns [] if the tables can't be read.
    """
    try:
        from utils.airtable_client import AirtableClient
        contacts = AirtableClient("Contacts").table.all(fields=[
            "Contact Name", "Contact Owner", "Kylas Company Id",
            "Pipeline Stage", "Next Call Date",
        ])
    except Exception as exc:
        print(f"[Email] WARNING: could not load Contacts for validation: {exc}")
        return []

    offsite_by_cid, name_by_cid = {}, {}
    try:
        from utils.airtable_client import AirtableClient
        for r in AirtableClient("Companies").table.all(
                fields=["Kylas Company ID", "Company Name", "Offsite Timeline"]):
            f   = r["fields"]
            cid = _norm_company_id(f.get("Kylas Company ID"))
            if cid:
                offsite_by_cid[cid] = str(f.get("Offsite Timeline") or "").strip()
                name_by_cid[cid]    = str(f.get("Company Name") or "")
    except Exception as exc:
        print(f"[Email] WARNING: could not load Companies for validation: {exc}")

    issues = []
    for r in contacts:
        f     = r["fields"]
        stage = str(f.get("Pipeline Stage") or "").strip()
        if not stage:
            continue
        slo    = stage.lower()
        cid    = _norm_company_id(f.get("Kylas Company Id"))
        values = {
            "nextcall": str(f.get("Next Call Date") or "").strip(),
            "offsite":  offsite_by_cid.get(cid, ""),
        }
        missing = []
        for rule in _VALIDATION_RULES:
            applies = (("when_stage" in rule and rule["when_stage"] in slo) or
                       ("when_set" in rule and bool(values.get(rule["when_set"]))))
            if not applies:
                continue
            for label, kind in rule["require"]:
                if not values.get(kind) and label not in missing:
                    missing.append(label)
        if missing:
            issues.append({
                "company": name_by_cid.get(cid) or cid or "—",
                "contact": str(f.get("Contact Name") or "—"),
                "owner":   str(f.get("Contact Owner") or "Unassigned"),
                "stage":   stage,
                "missing": ", ".join(missing),
            })
    issues.sort(key=lambda x: (x["company"].lower(), x["contact"].lower()))
    print(f"[Email] Validation issues: {len(issues)} contacts")
    return issues


def _validation_table_html(rows: list) -> str:
    hdr = ('<p style="font-weight:bold;font-size:14px;margin:24px 0 6px;">'
           f'Contact Validation Issues ({len(rows)})</p>')
    if not rows:
        return (hdr + '<p style="font-size:13px;color:#2e7d32;margin:4px 0 16px;">'
                'All MQL / Offsite contacts have their required fields. &#10003;</p>')
    head = (f'<table {_TABLE}><thead><tr>'
            f'<th {_TH}>Account</th><th {_TH}>Contact</th><th {_TH}>Owner</th>'
            f'<th {_TH}>Stage</th><th {_TH}>Missing required field(s)</th>'
            f'</tr></thead><tbody>')
    body = "".join(
        f'<tr><td {_TD}>{r["company"]}</td><td {_TD}>{r["contact"]}</td>'
        f'<td {_TD}>{r["owner"]}</td>'
        f'<td {_TD} style="font-size:12px;">{r["stage"]}</td>'
        f'<td {_TD} style="color:#c62828;font-weight:bold;">{r["missing"]}</td></tr>'
        for r in rows
    )
    return hdr + head + body + "</tbody></table>"


def _load_monthly_fixed() -> dict:
    """
    {metric: monthly_target} for metrics with fixed monthly goals (DCB, SQL).
    These are shown in every daily email as a reminder.
    """
    try:
        from utils.airtable_client import AirtableClient
        rows = AirtableClient("BD Config").table.all()
        out  = {}
        for r in rows:
            key = r["fields"].get("Key", "").strip()
            val = r["fields"].get("Value", 0) or 0
            if key.startswith("monthly_fixed_"):
                metric = key[len("monthly_fixed_"):]
                if val:
                    out[metric] = int(val)
        if out:
            return out
    except Exception:
        pass
    try:
        with open(TEAM_PATH) as fh:
            bt = json.load(fh).get("bd_targets", {})
        return {k: v for k, v in bt.get("monthly_fixed", {}).items() if v}
    except Exception:
        return {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _friendly_date(d: date = None) -> str:
    """e.g. 'June 8' — used in subject lines."""
    d = d or date.today()
    return f"{d.strftime('%B')} {d.day}"

def _fmt_tgt(daily: int) -> str:
    return "—" if not daily else str(daily)

def _fmt_win(daily: int) -> str:
    return "—" if not daily else str(daily // 4)

def _monthly_goal_html(monthly_fixed: dict) -> str:
    if not monthly_fixed:
        return ""
    rows = ""
    if monthly_fixed.get("dcb"):
        rows += (f'<tr><td {_TD}>Discovery Calls</td>'
                 f'<td {_TDB}>{monthly_fixed["dcb"]}</td></tr>')
    if monthly_fixed.get("sql"):
        rows += (f'<tr><td {_TD}>SQL</td>'
                 f'<td {_TDB}>{monthly_fixed["sql"]}</td></tr>')
    if not rows:
        return ""
    return (
        '<p style="font-size:13px;margin:20px 0 6px;color:#555;">'
        'Your target for this month:</p>'
        f'<table {_TABLE2}><tbody>{rows}</tbody></table>'
    )

def _html_doc(name: str, subtitle: str, content: str) -> str:
    body_style = (
        'style="font-family:Arial,sans-serif;color:#333333;'
        'max-width:640px;margin:0 auto;padding:24px 20px;"'
    )
    return (
        f'<!DOCTYPE html><html><body {body_style}>'
        f'<p style="margin:0 0 16px;">Hi {name},</p>'
        f'<p style="font-weight:bold;font-size:14px;margin:0 0 12px;">{subtitle}</p>'
        + content
        + '<p style="color:#999;font-size:12px;margin:24px 0 0;">— Kylas Sync</p>'
        '</body></html>'
    )


# ── Email body builders ───────────────────────────────────────────────────────

def _build_first_half(name: str, today: str, bd: dict, targets: dict,
                      monthly_fixed: dict = None, account_rows: list = None) -> tuple:
    rows = "".join(
        f'<tr><td {_TD}>{LABEL[k]}</td>'
        f'<td {_TDB}>{bd.get(k, 0)}</td>'
        f'<td {_TDC}>{_fmt_tgt(targets.get(k, 0))}</td>'
        f'<td {_TDC}>{_fmt_win(targets.get(k, 0))}</td></tr>'
        for k in METRICS
    )
    table = (
        f'<table {_TABLE}><thead><tr>'
        f'<th {_TH}>Metric</th><th {_THC}>Done</th>'
        f'<th {_THC}>Daily</th><th {_THC}>Window</th>'
        f'</tr></thead><tbody>{rows}</tbody></table>'
    )
    note = ('<p style="font-size:13px;color:#666;margin:0 0 8px;">'
            'Afternoon window: 3:00 PM – 6:00 PM</p>')
    content = table + _monthly_goal_html(monthly_fixed or {}) + note
    if account_rows:
        content += _account_table_html(account_rows)
    subject = f"BD | {name} | {_friendly_date()} | 11 AM Window"
    subtitle = f"BD Activity &nbsp;&middot;&nbsp; {today} &nbsp;&middot;&nbsp; 11:00 AM – 1:00 PM"
    return subject, _html_doc(name, subtitle, content)


def _build_full_day(name: str, today: str, bd: dict, targets: dict,
                    monthly_fixed: dict = None, account_rows: list = None,
                    validation_rows: list = None) -> tuple:
    w1          = bd.get("w1", {})
    w2          = bd.get("w2", {})
    has_windows = any(w1.get(k, 0) for k in METRICS)

    if has_windows:
        hdr = (
            f'<th {_TH}>Metric</th>'
            f'<th {_THC}>W1 (11–1)</th>'
            f'<th {_THC}>W2 (3–6)</th>'
            f'<th {_THC}>Total</th>'
            f'<th {_THC}>Daily</th>'
        )
        rows = "".join(
            f'<tr><td {_TD}>{LABEL[k]}</td>'
            f'<td {_TDC}>{w1.get(k, 0)}</td>'
            f'<td {_TDC}>{w2.get(k, 0)}</td>'
            f'<td {_TDB}>{bd.get(k, 0)}</td>'
            f'<td {_TDC}>{_fmt_tgt(targets.get(k, 0))}</td></tr>'
            for k in METRICS
        )
    else:
        hdr = (
            f'<th {_TH}>Metric</th>'
            f'<th {_THC}>Done</th>'
            f'<th {_THC}>Daily</th>'
            f'<th {_THC}>Window</th>'
        )
        rows = "".join(
            f'<tr><td {_TD}>{LABEL[k]}</td>'
            f'<td {_TDB}>{bd.get(k, 0)}</td>'
            f'<td {_TDC}>{_fmt_tgt(targets.get(k, 0))}</td>'
            f'<td {_TDC}>{_fmt_win(targets.get(k, 0))}</td></tr>'
            for k in METRICS
        )

    table   = f'<table {_TABLE}><thead><tr>{hdr}</tr></thead><tbody>{rows}</tbody></table>'
    content = table + _monthly_goal_html(monthly_fixed or {})
    if account_rows:
        content += _account_table_html(account_rows)
    # Section 2: contact validation issues (compiled across all accounts).
    if validation_rows is not None:
        content += _validation_table_html(validation_rows)
    subject  = f"BD | {name} | {_friendly_date()} | EOD"
    subtitle = f"BD Activity &nbsp;&middot;&nbsp; {today} &nbsp;&middot;&nbsp; End of Day"
    return subject, _html_doc(name, subtitle, content)


# ── SMTP send ─────────────────────────────────────────────────────────────────

def _send(smtp_user: str, smtp_pass: str, to: str, subject: str, body: str, cc: list):
    msg            = MIMEMultipart("alternative")
    msg["From"]    = smtp_user
    msg["To"]      = to
    msg["Subject"] = subject
    if cc:
        msg["CC"] = ", ".join(cc)
    msg.attach(MIMEText(body, "html", "utf-8"))
    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.ehlo(); s.starttls()
        s.login(smtp_user, smtp_pass)
        s.sendmail(smtp_user, [to] + cc, msg.as_string())


# ── Public entry point ────────────────────────────────────────────────────────

def _member_bd(name: str, bd_enriched: dict) -> dict:
    lo = name.lower()
    for owner, stats in bd_enriched.items():
        owner_lo = owner.lower()
        # Match either direction: "riya" in "riya singh" OR "riya singh" in "riya"
        if lo in owner_lo or owner_lo in lo:
            return stats
    return {}


def send_alert(stats: dict, slot: str = "test", bd_enriched: dict = None,
               demo_recipients: list = None):
    """
    Send BD emails to the BD team.

    demo_recipients: if set, send one sample email to these addresses instead
                     of the full team (used for testing the template).
    """
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    if not smtp_user or not smtp_pass:
        print("[Email] SMTP_USER / SMTP_PASS not set — skipping")
        return

    today         = date.today().strftime("%d %b %Y")
    targets       = _load_daily_targets()
    monthly_fixed = _load_monthly_fixed()
    account_rows  = _load_account_activity_today()
    print(f"[Email] Account activity today: {len(account_rows)} companies")
    # Validation issues only go in the End-of-Day email (Section 2).
    validation_rows = _load_validation_issues() if slot != "first_half" else None

    with open(TEAM_PATH) as fh:
        cfg = json.load(fh)
    cc_list = cfg.get("cc", [])

    # Demo mode — send one sample to the provided addresses, no CC
    if demo_recipients:
        sample_bd = next(iter((bd_enriched or {}).values()), {})
        if slot == "first_half":
            subject, body = _build_first_half(
                "Team", today, sample_bd, targets, monthly_fixed, account_rows=account_rows)
        else:
            subject, body = _build_full_day(
                "Team", today, sample_bd, targets, monthly_fixed,
                account_rows=account_rows, validation_rows=validation_rows)
        for addr in demo_recipients:
            try:
                _send(smtp_user, smtp_pass, addr, subject, body, [])
                print(f"[Email] Demo sent → {addr}")
            except Exception as exc:
                print(f"[Email] WARNING demo {addr}: {exc}")
        return

    # Normal mode — send to each BD member
    bd_team = _load_bd_members()
    print(f"[Email] BD team ({len(bd_team)}): {[m['name'] for m in bd_team]}")
    print(f"[Email] bd_enriched owners: {list((bd_enriched or {}).keys())}")

    for member in bd_team:
        name  = member["name"]
        email = member["email"]
        bd    = _member_bd(name, bd_enriched or {})

        if not bd:
            print(f"[Email] No BD data for {name} — sending with zeros")

        if slot == "first_half":
            subject, body = _build_first_half(
                name, today, bd, targets, monthly_fixed, account_rows=account_rows)
        else:
            my_issues = None
            if validation_rows is not None:
                lo = name.lower()
                my_issues = [r for r in validation_rows
                             if lo in r["owner"].lower() or r["owner"].lower() in lo]
            subject, body = _build_full_day(
                name, today, bd, targets, monthly_fixed,
                account_rows=account_rows, validation_rows=my_issues)

        eff_cc = [a for a in cc_list if a.lower() != email.lower()]
        try:
            _send(smtp_user, smtp_pass, email, subject, body, eff_cc)
            cc_s = f"  (cc: {', '.join(eff_cc)})" if eff_cc else ""
            print(f"[Email] Sent → {name} <{email}>{cc_s}")
        except Exception as exc:
            print(f"[Email] WARNING: {name} <{email}> — {exc}")


def _fallback_stats(member_name: str, stats: dict, module: str) -> dict:
    per_user = stats.get(module, {}).get("per_user", {})
    result   = {"created": 0, "updated": 0}
    for kylas_name, s in per_user.items():
        if member_name.lower() in kylas_name.lower():
            result["created"] += s.get("created", 0)
            result["updated"] += s.get("updated", 0)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot", default="first_half")
    parser.add_argument("--demo", nargs="+", metavar="EMAIL",
                        help="Send a sample email to these addresses (no team blast)")
    args = parser.parse_args()
    from dotenv import load_dotenv; load_dotenv()

    test_bd = {
        "Bhaumik": {
            "attempted": 45, "connected": 18, "dcb": 2, "sql": 1, "mql": 3, "activation": 1,
            "w1": {"attempted": 28, "connected": 11, "dcb": 1, "sql": 0, "mql": 2, "activation": 0},
            "w2": {"attempted": 17, "connected":  7, "dcb": 1, "sql": 1, "mql": 1, "activation": 1},
        },
        "Rubal": {
            "attempted": 62, "connected": 24, "dcb": 3, "sql": 0, "mql": 1, "activation": 0,
            "w1": {"attempted": 35, "connected": 14, "dcb": 2, "sql": 0, "mql": 1, "activation": 0},
            "w2": {"attempted": 27, "connected": 10, "dcb": 1, "sql": 0, "mql": 0, "activation": 0},
        },
    }
    send_alert({}, args.slot, bd_enriched=test_bd, demo_recipients=args.demo)
