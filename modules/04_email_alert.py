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
    win_tgt     = lambda m: targets.get(m, 0) // 4

    opening = (
        "Morning sync done! Here is where you stand at midday:"
        if slot == "first_half"
        else "End of day sync complete! Here is your full BD report:"
    )
    sep = "=" * 48
    bar = "-" * 48

    lines = [
        f"Hi {name}!",
        "",
        opening,
        "",
        f"  Date  : {today}",
        f"  Slot  : {slot_label}",
        "",
        sep,
        f"  {'ACTIVITY SUMMARY':^44}",
        sep,
    ]

    if has_windows:
        lines.append(f"  {'Metric':<22}{'W1 (11-1)':>8}{'W2 (3-6)':>9}{'Total':>7}{'Target':>7}")
        lines.append(f"  {bar}")
        for key in DISPLAY_ORDER:
            lbl   = METRIC_LABEL[key]
            total = bd.get(key, 0)
            w1v   = w1.get(key, 0)
            w2v   = w2.get(key, 0)
            dt    = targets.get(key, 0)
            tgt   = f"/{dt}" if dt else ""
            lines.append(f"  {lbl:<22}{w1v:>8}{w2v:>9}{total:>7}{tgt:>7}")
    else:
        lines.append(f"  {'Metric':<26}{'Count':>6}{'Win.Target':>12}")
        lines.append(f"  {bar}")
        for key in DISPLAY_ORDER:
            lbl = METRIC_LABEL[key]
            cnt = bd.get(key, 0)
            wt  = win_tgt(key)
            wts = f"/ {wt}" if wt else ""
            lines.append(f"  {lbl:<26}{cnt:>6}{wts:>12}")

    lines += [
        sep,
        "",
        _encouragement(bd.get("connected", 0), targets.get("connected", 0)),
        "",
    ]

    if slot == "first_half":
        lines.append("The afternoon window (3:00 - 6:00 PM) is ahead — let's make it count!")
    else:
        lines.append("Another day building the pipeline. Rest well — tomorrow is a fresh start!")

    lines += [
        "",
        "Cheers,",
        "Kylas Sync Bot",
        "(your data is live in the Airtable sales pipeline)",
    ]

    return "\n".join(lines)


def _send_smtp(from_addr: str, password: str, to_addr: str, subject: str, body: str):
    msg = MIMEMultipart()
    msg["From"]    = from_addr
    msg["To"]      = to_addr
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.ehlo()
        server.starttls()
        server.login(from_addr, password)
        server.send_message(msg)


def send_alert(stats: dict, slot: str = "test", bd_enriched: dict = None):
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")

    if not smtp_user or not smtp_pass:
        print("[Email] SMTP_USER / SMTP_PASS secrets not set — skipping email alert")
        return

    today      = date.today().strftime("%d %b %Y")
    slot_label = slot.replace("_", " ").title()

    with open(TEAM_PATH) as f:
        team = json.load(f)["members"]

    all_targets = _read_targets()

    for member in team:
        name  = member["name"]
        email = member["email"]

        bd = _member_bd(name, bd_enriched or {})
        if bd:
            targets = all_targets.get(name.lower(), {})
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

        try:
            _send_smtp(smtp_user, smtp_pass, email, subject, body)
            print(f"[Email] Sent to {name} <{email}>")
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
