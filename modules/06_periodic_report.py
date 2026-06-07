"""
Weekly and monthly BD summary email.

  python modules/06_periodic_report.py --period weekly
  python modules/06_periodic_report.py --period monthly

Reads BD Daily Stats (full_day rows) from Airtable, sums per owner,
and emails each team member their achievement vs target.

Targets: team.json → bd_targets.daily  (same for everyone)
Weekly  target = daily × weekly_multiplier  (default 5.5)
Monthly target = daily × monthly_multiplier (default 22)
"""
import json
import os
import smtplib
import sys
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEAM_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "team.json")

METRICS = ["attempted", "connected", "dcb", "sql"]
METRIC_LABEL = {
    "attempted": "Attempted",
    "connected": "Connected",
    "dcb":       "Discovery Calls",
    "sql":       "SQL",
}
AIRTABLE_FIELD = {
    "attempted": "Attempted",
    "connected": "Connected",
    "dcb":       "Discovery Calls",
    "sql":       "SQL",
}


def _load_team():
    with open(TEAM_PATH) as fh:
        return json.load(fh)


def _read_period_stats(start: str, end: str) -> dict:
    """Return {owner: {metric: total}} summed from BD Daily Stats full_day rows."""
    from utils.airtable_client import AirtableClient
    tbl     = AirtableClient("BD Daily Stats")
    formula = f"AND({{Slot}}='full_day', {{Date}}>='{start}', {{Date}}<='{end}')"
    records = tbl.table.all(formula=formula)

    totals: dict = {}
    for rec in records:
        f     = rec["fields"]
        owner = f.get("Owner", "").strip()
        if not owner:
            continue
        bucket = totals.setdefault(owner, {k: 0 for k in METRICS})
        for key, col in AIRTABLE_FIELD.items():
            bucket[key] += int(f.get(col, 0) or 0)
    return totals


def _find_member_stats(name: str, period_stats: dict) -> dict:
    lo = name.strip().lower()
    for owner, stats in period_stats.items():
        if lo in owner.lower() or owner.lower() in lo:
            return stats
    return {k: 0 for k in METRICS}


_TH  = ('style="background:#f2f2f2;text-align:left;padding:9px 14px;'
        'border:1px solid #cccccc;font-size:13px;"')
_THC = ('style="background:#f2f2f2;text-align:center;padding:9px 14px;'
        'border:1px solid #cccccc;font-size:13px;"')
_TD  = 'style="padding:9px 14px;border:1px solid #cccccc;font-size:13px;"'
_TDC = ('style="text-align:center;padding:9px 14px;'
        'border:1px solid #cccccc;font-size:13px;"')
_TDB = ('style="text-align:center;padding:9px 14px;'
        'border:1px solid #cccccc;font-size:13px;font-weight:bold;"')
_TABLE = 'style="border-collapse:collapse;width:100%;margin:8px 0 16px;"'


def _pct(done: int, target: int) -> str:
    if target <= 0:
        return "—"
    return f"{done / target * 100:.0f}%"


def _build_body(name: str, period: str, range_label: str,
                achieved: dict, bd_targets: dict) -> str:
    daily  = bd_targets.get("daily", {})
    w_mult = bd_targets.get("weekly_multiplier",  5.5)
    m_mult = bd_targets.get("monthly_multiplier", 22)
    mult   = w_mult if period == "weekly" else m_mult
    opening = "weekly" if period == "weekly" else "monthly"

    rows = ""
    for key in METRICS:
        lbl   = METRIC_LABEL[key]
        done  = achieved.get(key, 0)
        d_tgt = daily.get(key, 0)
        p_tgt = round(d_tgt * mult) if d_tgt else 0
        tgt_s = str(p_tgt) if p_tgt else "—"
        pct_s = _pct(done, p_tgt)
        rows += (
            f'<tr><td {_TD}>{lbl}</td>'
            f'<td {_TDB}>{done}</td>'
            f'<td {_TDC}>{tgt_s}</td>'
            f'<td {_TDC}>{pct_s}</td></tr>'
        )

    table = (
        f'<table {_TABLE}><thead><tr>'
        f'<th {_TH}>Metric</th>'
        f'<th {_THC}>Done</th>'
        f'<th {_THC}>Target</th>'
        f'<th {_THC}>%</th>'
        f'</tr></thead><tbody>{rows}</tbody></table>'
    )

    body_style = (
        'style="font-family:Arial,sans-serif;color:#333333;'
        'max-width:640px;margin:0 auto;padding:24px 20px;"'
    )
    return (
        f'<!DOCTYPE html><html><body {body_style}>'
        f'<p style="margin:0 0 16px;">Hi {name},</p>'
        f'<p style="font-weight:bold;font-size:14px;margin:0 0 12px;">'
        f'BD {opening.title()} Report &nbsp;&middot;&nbsp; {range_label}</p>'
        + table
        + '<p style="color:#999;font-size:12px;margin:24px 0 0;">— Kylas Sync</p>'
        '</body></html>'
    )


def send_report(period: str):
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    if not smtp_user or not smtp_pass:
        print(f"[{period.title()} Report] SMTP_USER/SMTP_PASS not set — skipping")
        return

    today = date.today()
    if period == "weekly":
        # Report covers Mon–Fri of the week that just ended (run on Saturday)
        end   = today - timedelta(days=1)          # Friday
        start = end - timedelta(days=4)            # Monday
        range_label = f"{start.strftime('%d %b')} – {end.strftime('%d %b %Y')}"
        subject_sfx = f"{start.strftime('%d %b')} – {end.strftime('%d %b %Y')}"
    else:
        # Report covers the entire previous month (run on 1st of new month)
        first_this_month = today.replace(day=1)
        last_month_end   = first_this_month - timedelta(days=1)
        start = last_month_end.replace(day=1)
        end   = last_month_end
        range_label = start.strftime("%B %Y")
        subject_sfx = start.strftime("%B %Y")

    print(f"[{period.title()} Report] Period: {start} → {end}")

    try:
        period_stats = _read_period_stats(start.isoformat(), end.isoformat())
        print(f"[{period.title()} Report] Stats: {len(period_stats)} owners")
    except Exception as exc:
        print(f"[{period.title()} Report] WARNING: could not read BD Daily Stats — {exc}")
        period_stats = {}

    cfg       = _load_team()
    members   = cfg.get("bd_team", cfg.get("members", []))
    cc_list   = cfg.get("cc", [])
    bd_targets = cfg.get("bd_targets", {})

    for member in members:
        name  = member["name"]
        email = member["email"]

        achieved = _find_member_stats(name, period_stats)
        body     = _build_body(name, period, range_label, achieved, bd_targets)
        subject  = f"BD {period.title()} | {subject_sfx}"
        eff_cc   = [a for a in cc_list if a.lower() != email.lower()]

        msg             = MIMEMultipart("alternative")
        msg["From"]     = smtp_user
        msg["To"]       = email
        msg["Subject"]  = subject
        if eff_cc:
            msg["CC"] = ", ".join(eff_cc)
        msg.attach(MIMEText(body, "html", "utf-8"))

        try:
            with smtplib.SMTP("smtp.gmail.com", 587) as srv:
                srv.ehlo(); srv.starttls()
                srv.login(smtp_user, smtp_pass)
                srv.sendmail(smtp_user, [email] + eff_cc, msg.as_string())
            cc_s = f"  (cc: {', '.join(eff_cc)})" if eff_cc else ""
            print(f"[{period.title()} Report] Sent → {name} <{email}>{cc_s}")
        except Exception as exc:
            print(f"[{period.title()} Report] WARNING: could not send to {name} — {exc}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--period", choices=["weekly", "monthly"], default="weekly")
    args = parser.parse_args()
    from dotenv import load_dotenv; load_dotenv()
    send_report(args.period)
