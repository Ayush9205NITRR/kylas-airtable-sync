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
    Reads Airtable 'BD Members' first, falls back to team.json bd_team.
    """
    try:
        from utils.airtable_client import AirtableClient
        rows = AirtableClient("BD Members").table.all()
        members = [
            {"name": r["fields"]["Name"], "email": r["fields"]["Email"]}
            for r in rows
            if r["fields"].get("Active", True)
            and r["fields"].get("Group", "BD") == "BD"
            and r["fields"].get("Name") and r["fields"].get("Email")
        ]
        if members:
            return members
    except Exception:
        pass
    with open(TEAM_PATH) as fh:
        cfg = json.load(fh)
    return cfg.get("bd_team", cfg.get("members", []))


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


def _load_account_activity_today() -> list:
    """
    Reads today's Account Activity Log from Airtable, joins with Companies CRM
    (for Pipeline Stage BD + Source of Data), and returns rows sorted by
    Attempted POCs descending.  Only rows with at least 1 attempted POC included.
    """
    today = date.today().isoformat()
    try:
        from utils.airtable_client import AirtableClient
        acc_rows = AirtableClient("Account Activity Log").table.all(
            formula=f"{{Date}}='{today}'"
        )
        if not acc_rows:
            return []

        # Build a company-info lookup from the CRM Companies table
        co_rows = AirtableClient("Companies").table.all(
            fields=["Kylas Company Id", "Pipeline Stage BD", "Source of Data"]
        )
        co_info: dict = {}
        for r in co_rows:
            f   = r["fields"]
            cid = str(f.get("Kylas Company Id") or "").strip()
            if cid:
                co_info[cid] = {
                    "stage":  str(f.get("Pipeline Stage BD") or ""),
                    "source": str(f.get("Source of Data")    or ""),
                }

        result = []
        for r in acc_rows:
            f         = r["fields"]
            attempted = int(f.get("Attempted POCs", 0) or 0)
            if attempted == 0:
                continue
            cid  = str(f.get("Kylas Company Id") or "").strip()
            co   = co_info.get(cid, {})
            result.append({
                "company":   str(f.get("Company Name") or ""),
                "stage":     co.get("stage",  ""),
                "source":    co.get("source", ""),
                "attempted": attempted,
                "connected": int(f.get("Connected POCs", 0) or 0),
                "dcb":       int(f.get("DCB POCs",       0) or 0),
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
        'Account Activity Today</p>'
        f'<table {_TABLE}><thead><tr>'
        f'<th {_TH}>Account</th>'
        f'<th {_TH}>Pipeline Stage</th>'
        f'<th {_TH}>Source</th>'
        f'<th {_THC}>Attempted</th>'
        f'<th {_THC}>Connected</th>'
        f'<th {_THC}>Discovery Calls</th>'
        f'</tr></thead><tbody>'
    )
    body = "".join(
        f'<tr>'
        f'<td {_TD}>{r["company"]}</td>'
        f'<td {_TD}>{r["stage"]}</td>'
        f'<td {_TD}>{r["source"]}</td>'
        f'<td {_TDB}>{r["attempted"]}</td>'
        f'<td {_TDC}>{r["connected"]}</td>'
        f'<td {_TDC}>{r["dcb"] if r["dcb"] else "—"}</td>'
        f'</tr>'
        for r in rows
    )
    return hdr + body + "</tbody></table>"


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
                      monthly_fixed: dict = None) -> tuple:
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
    subject = f"BD | {name} | {_friendly_date()} | 11 AM Window"
    subtitle = f"BD Activity &nbsp;&middot;&nbsp; {today} &nbsp;&middot;&nbsp; 11:00 AM – 1:00 PM"
    return subject, _html_doc(name, subtitle, content)


def _build_full_day(name: str, today: str, bd: dict, targets: dict,
                    monthly_fixed: dict = None, account_rows: list = None) -> tuple:
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
        if lo in owner.lower():
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

    with open(TEAM_PATH) as fh:
        cfg = json.load(fh)
    cc_list = cfg.get("cc", [])

    # Demo mode — send one sample to the provided addresses, no CC
    if demo_recipients:
        sample_bd    = next(iter((bd_enriched or {}).values()), {})
        demo_acc     = _load_account_activity_today() if slot == "full_day" else []
        if slot == "first_half":
            subject, body = _build_first_half("Team", today, sample_bd, targets, monthly_fixed)
        else:
            subject, body = _build_full_day(
                "Team", today, sample_bd, targets, monthly_fixed, account_rows=demo_acc
            )
        for addr in demo_recipients:
            try:
                _send(smtp_user, smtp_pass, addr, subject, body, [])
                print(f"[Email] Demo sent → {addr}")
            except Exception as exc:
                print(f"[Email] WARNING demo {addr}: {exc}")
        return

    # Normal mode — send to each BD member
    bd_team = _load_bd_members()

    # Load account activity once for EOD run (shared across all member emails)
    account_rows = []
    if slot == "full_day":
        account_rows = _load_account_activity_today()
        print(f"[Email] Account activity today: {len(account_rows)} companies")

    for member in bd_team:
        name  = member["name"]
        email = member["email"]
        bd    = _member_bd(name, bd_enriched or {})

        if not bd:
            print(f"[Email] No BD data for {name} — skipping")
            continue

        if slot == "first_half":
            subject, body = _build_first_half(name, today, bd, targets, monthly_fixed)
        else:
            subject, body = _build_full_day(
                name, today, bd, targets, monthly_fixed, account_rows=account_rows
            )

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
