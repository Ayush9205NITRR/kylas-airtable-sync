"""
BD-focused daily email report.

Template variables (all auto-populated at runtime):
  name             → member name from config/team.json
  today            → current date, e.g. "05 Jun 2026"
  slot_label       → "First Half" (1:30 PM run) or "Full Day" (6:30 PM run)
  slot             → "first_half" or "full_day"
  bd               → dict with keys:
                       attempted, connected, dcb, sql, mql, activation  (totals)
                       w1: {same keys}   ← first-half counts (full_day email only)
                       w2: {same keys}   ← afternoon delta   (full_day email only)
  targets          → {metric: daily_target} from BD Targets Airtable table
                       window_target = daily_target // 4

BD metric definitions (sourced from Contacts pipeline stage):
  Attempted       → any stage except "Yet to Be Mined"
  Connected       → MQL / SQL / Activation / NDM / Not Interested / Follow-ups /
                    Discovery Call Booked / Invalid Contact / Connect Later / etc.
  Discovery Calls → SQL / Discovery Call Booked / Offsite Delayed / No-Show /
                    Reschedule Pending / Closing Loops
  MQL             → MQL (Marketing Qualified Lead)
  SQL             → SQL (Sales Qualified Lead)
  Activation      → Activation
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

DISPLAY_ORDER = ["attempted", "connected", "dcb", "mql", "sql", "activation"]
METRIC_LABEL  = {
    "attempted":  "Attempted",
    "connected":  "Connected",
    "dcb":        "Discovery Calls",
    "mql":        "MQL",
    "sql":        "SQL",
    "activation": "Activation",
}
# Map BD Targets singleSelect names → internal metric keys
_TARGET_KEY = {
    "attempted":      "attempted",
    "connected":      "connected",
    "mql":            "mql",
    "discovery call": "dcb",
    "sql":            "sql",
    "activation":     "activation",
}


def _read_targets() -> dict:
    """Read {owner_lower: {metric_key: daily_target}} from BD Targets table."""
    try:
        from utils.airtable_client import AirtableClient
        tbl     = AirtableClient("BD Targets")
        records = tbl.table.all()
        out = {}
        for rec in records:
            f      = rec["fields"]
            owner  = f.get("Owner", "").strip().lower()
            m_raw  = f.get("Metric", "").strip().lower()
            m_key  = _TARGET_KEY.get(m_raw, m_raw)
            daily  = int(f.get("Daily Target", 0) or 0)
            if owner and m_key:
                out.setdefault(owner, {})[m_key] = daily
        return out
    except Exception:
        return {}


def _member_targets(member_name: str, all_targets: dict) -> dict:
    """Find targets for a member by fuzzy name match.

    Handles cases where BD Targets Owner is 'Bhaumik Sachdeva' but
    team.json uses short name 'Bhaumik', and vice versa.
    """
    lo = member_name.strip().lower()
    if lo in all_targets:
        return all_targets[lo]
    # Partial match: 'bhaumik' matches 'bhaumik sachdeva'
    for key, val in all_targets.items():
        if lo in key or key in lo:
            return val
    return {}


def _member_bd(member_name: str, bd_enriched: dict) -> dict:
    """Find enriched BD stats for a team member by fuzzy name match."""
    lo = member_name.lower()
    for kylas_name, stats in bd_enriched.items():
        if lo in kylas_name.lower():
            return stats
    return {}


def _encouragement(connected: int, target: int) -> str:
    if target > 0:
        pct = connected / target
        if pct >= 1.0:
            return "Target smashed! You are absolutely on fire — incredible work today!"
        if pct >= 0.75:
            return "So close to target! Brilliant effort — that consistency adds up."
        if pct >= 0.5:
            return "Good progress! Push a little harder tomorrow and you will hit it."
        return "Every conversation counts. Keep showing up — the breakthrough is coming!"
    if connected == 0:
        return "Even quiet days build the foundation. Tomorrow is yours to own!"
    if connected < 5:
        return "Good start! Small, consistent steps build great pipelines."
    if connected < 15:
        return "Solid effort! Consistency like this is what separates good from great."
    return "Incredible hustle today! That kind of volume is how deals get closed!"


def _build_body(name: str, today: str, slot: str, slot_label: str,
                bd: dict, targets: dict) -> str:
    w1          = bd.get("w1", {})
    w2          = bd.get("w2", {})
    has_windows = slot == "full_day" and any(w1.values())

    def _win_tgt(m):
        d = targets.get(m)
        return None if d is None else d // 4

    def _fmt(t):
        return "—" if t is None else f"/ {t}"

    sep = "=" * 50
    bar = "-" * 50

    if slot == "full_day":
        lines = [
            f"Hi {name}!",
            "",
            f"End of day sync complete. Here is your full BD report for {today}.",
            "",
        ]

        if has_windows:
            lines += [
                sep,
                "  FIRST HALF  (11:00 AM – 1:00 PM)",
                sep,
                f"  {'Metric':<22} {'Count':>5}   {'Target':>8}",
                f"  {bar}",
            ]
            for key in DISPLAY_ORDER:
                lines.append(
                    f"  {METRIC_LABEL[key]:<22} {w1.get(key, 0):>5}   {_fmt(_win_tgt(key)):>8}"
                )
            lines += [
                "",
                sep,
                "  AFTERNOON  (3:00 PM – 6:00 PM)",
                sep,
                f"  {'Metric':<22} {'Count':>5}   {'Target':>8}",
                f"  {bar}",
            ]
            for key in DISPLAY_ORDER:
                lines.append(
                    f"  {METRIC_LABEL[key]:<22} {w2.get(key, 0):>5}   {_fmt(_win_tgt(key)):>8}"
                )
            lines.append("")

        lines += [
            sep,
            "  FULL DAY TOTAL",
            sep,
            f"  {'Metric':<22} {'Count':>5}   {'Daily Target':>12}",
            f"  {bar}",
        ]
        for key in DISPLAY_ORDER:
            dt  = targets.get(key)
            tgt = "—" if dt is None else f"/ {dt}"
            lines.append(
                f"  {METRIC_LABEL[key]:<22} {bd.get(key, 0):>5}   {tgt:>12}"
            )
        lines += [
            sep,
            "",
            _encouragement(bd.get("connected", 0), targets.get("connected") or 0),
            "",
            "Another day building the pipeline. Rest well — tomorrow is a fresh start!",
        ]

    else:
        lines = [
            f"Hi {name}!",
            "",
            f"Morning sync done. Here is where you stand at midday on {today}.",
            "",
            sep,
            "  FIRST HALF  (11:00 AM – 1:00 PM)",
            sep,
            f"  {'Metric':<22} {'Count':>5}   {'Win.Target':>10}",
            f"  {bar}",
        ]
        for key in DISPLAY_ORDER:
            lines.append(
                f"  {METRIC_LABEL[key]:<22} {bd.get(key, 0):>5}   {_fmt(_win_tgt(key)):>10}"
            )
        lines += [
            sep,
            "",
            _encouragement(bd.get("connected", 0), targets.get("connected") or 0),
            "",
            "The afternoon window (3:00 – 6:00 PM) is ahead — let's make it count!",
        ]

    if not targets:
        lines += [
            "",
            "(Tip: fill the BD Targets table in Airtable to unlock target comparisons.)",
        ]

    lines += [
        "",
        "Cheers,",
        "Kylas Sync Bot",
        "(your data is live in the Airtable sales pipeline)",
    ]

    return "\n".join(lines)


def _send_smtp(from_addr: str, password: str, to_addr: str, subject: str, body: str,
               cc: list = None):
    msg = MIMEMultipart()
    msg["From"]    = from_addr
    msg["To"]      = to_addr
    msg["Subject"] = subject
    if cc:
        msg["CC"] = ", ".join(cc)
    msg.attach(MIMEText(body, "plain"))
    recipients = [to_addr] + (cc or [])
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.ehlo()
        server.starttls()
        server.login(from_addr, password)
        server.sendmail(from_addr, recipients, msg.as_string())


def send_alert(stats: dict, slot: str = "test", bd_enriched: dict = None):
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")

    if not smtp_user or not smtp_pass:
        print("[Email] SMTP_USER / SMTP_PASS secrets not set — skipping email alert")
        return

    today      = date.today().strftime("%d %b %Y")
    slot_label = slot.replace("_", " ").title()

    with open(TEAM_PATH) as f:
        _team_cfg = json.load(f)
    team        = _team_cfg["members"]
    cc_list     = _team_cfg.get("cc", [])

    all_targets = _read_targets()

    for member in team:
        name  = member["name"]
        email = member["email"]

        bd = _member_bd(name, bd_enriched or {})
        if bd:
            targets = _member_targets(name, all_targets)
            body    = _build_body(name, today, slot, slot_label, bd, targets)
            subject = f"Kylas BD Report — {today} | {slot_label}"
        else:
            # Fallback to raw sync counts when BD metrics are unavailable
            co = _fallback_stats(name, stats, "companies")
            ct = _fallback_stats(name, stats, "contacts")
            d  = _fallback_stats(name, stats, "deals")
            body = (
                f"Hi {name}!\n\nKylas Sync Report\n{'='*36}\n"
                f"Date  : {today}\nSlot  : {slot_label}\n\n"
                f"Companies : {co['created']} new  |  {co['updated']} updated\n"
                f"Contacts  : {ct['created']} new  |  {ct['updated']} updated\n"
                f"Deals     : {d['created']} new  |  {d['updated']} updated\n"
                f"{'='*36}\n\nKeep it up!\n\n-- Kylas Sync Bot"
            )
            subject = f"Kylas Sync Report — {today} | {slot_label}"

        # CC everyone on the cc list except the primary recipient themselves
        effective_cc = [addr for addr in cc_list if addr.lower() != email.lower()]

        try:
            _send_smtp(smtp_user, smtp_pass, email, subject, body, cc=effective_cc)
            cc_str = f"  (cc: {', '.join(effective_cc)})" if effective_cc else ""
            print(f"[Email] Sent to {name} <{email}>{cc_str}")
        except Exception as e:
            print(f"[Email] WARNING: could not send to {name} <{email}>: {e}")


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
    args = parser.parse_args()
    from dotenv import load_dotenv; load_dotenv()

    test_bd = {
        "Ayush": {
            "attempted": 15, "connected": 8, "dcb": 2, "sql": 1, "mql": 3, "activation": 1,
            "w1": {"attempted": 10, "connected": 5, "dcb": 1, "sql": 0, "mql": 2, "activation": 0},
            "w2": {"attempted":  5, "connected": 3, "dcb": 1, "sql": 1, "mql": 1, "activation": 1},
        },
        "Bhaumik": {
            "attempted": 10, "connected": 5, "dcb": 1, "sql": 0, "mql": 2, "activation": 1,
            "w1": {"attempted":  6, "connected": 3, "dcb": 0, "sql": 0, "mql": 1, "activation": 0},
            "w2": {"attempted":  4, "connected": 2, "dcb": 1, "sql": 0, "mql": 1, "activation": 1},
        },
    }
    send_alert({}, args.slot, bd_enriched=test_bd)
