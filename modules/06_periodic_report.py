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


def _pct(done: int, target: int) -> str:
    if target <= 0:
        return "  —"
    return f"{done / target * 100:>4.0f}%"


def _build_body(name: str, period: str, range_label: str,
                achieved: dict, bd_targets: dict) -> str:
    daily  = bd_targets.get("daily", {})
    w_mult = bd_targets.get("weekly_multiplier",  5.5)
    m_mult = bd_targets.get("monthly_multiplier", 22)
    mult   = w_mult if period == "weekly" else m_mult
    p_label = "WEEKLY" if period == "weekly" else "MONTHLY"
    opening = "weekly" if period == "weekly" else "monthly"

    sep = "=" * 54
    bar = "-" * 54

    lines = [
        f"Hi {name}!",
        "",
        f"Your {opening} BD report is ready.",
        "",
        f"  Period : {range_label}",
        "",
        sep,
        f"  {p_label} SUMMARY",
        sep,
        f"  {'Metric':<22} {'Done':>6}  {'Target':>8}  {'%':>5}",
        f"  {bar}",
    ]

    for key in METRICS:
        lbl   = METRIC_LABEL[key]
        done  = achieved.get(key, 0)
        d_tgt = daily.get(key, 0)
        p_tgt = round(d_tgt * mult) if d_tgt else 0
        tgt_s = f"/ {p_tgt}" if p_tgt else "—"
        pct_s = _pct(done, p_tgt)
        lines.append(f"  {lbl:<22} {done:>6}  {tgt_s:>8}  {pct_s}")

    lines.append(sep)
    lines.append("")

    # Encouragement
    conn_tgt  = round(daily.get("connected", 0) * mult)
    conn_done = achieved.get("connected", 0)
    if conn_tgt > 0:
        pct = conn_done / conn_tgt
        if pct >= 1.0:
            enc = "Target achieved! Exceptional work this period!"
        elif pct >= 0.75:
            enc = "So close to target — strong showing this period!"
        elif pct >= 0.5:
            enc = "Halfway there — keep the momentum going!"
        else:
            enc = "Every call builds the pipeline — next period is yours!"
    else:
        enc = "Keep building — every conversation moves the needle!"

    lines += [
        enc,
        "",
        "Cheers,",
        "Kylas Sync Bot",
        "(your data is live in the Airtable sales pipeline)",
    ]
    return "\n".join(lines)


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
        subject_sfx = f"Week of {start.strftime('%d %b %Y')}"
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
    members   = cfg["members"]
    cc_list   = cfg.get("cc", [])
    bd_targets = cfg.get("bd_targets", {})

    for member in members:
        name  = member["name"]
        email = member["email"]

        achieved = _find_member_stats(name, period_stats)
        body     = _build_body(name, period, range_label, achieved, bd_targets)
        subject  = f"Kylas BD {period.title()} Report — {subject_sfx}"
        eff_cc   = [a for a in cc_list if a.lower() != email.lower()]

        msg             = MIMEMultipart()
        msg["From"]     = smtp_user
        msg["To"]       = email
        msg["Subject"]  = subject
        if eff_cc:
            msg["CC"] = ", ".join(eff_cc)
        msg.attach(MIMEText(body, "plain"))

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
