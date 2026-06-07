"""
BD daily email report — clean activity update, no motivation text.

Slots:
  first_half  → 1:30 PM IST run  (11 AM – 1 PM window numbers)
  full_day    → 6:30 PM IST run  (W1 + W2 breakdown + daily total)

Groups:
  bd_team      → gets BD activity emails (this module)
  revenue_team → gets deal-focused emails (future module)

Targets: read from Airtable BD Config table (Key/Value rows) first,
         then fall back to config/team.json bd_targets.daily.
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


# ── Target loading ────────────────────────────────────────────────────────────

def _load_daily_targets() -> dict:
    """
    {metric: daily_target} — same for all members.

    Priority:
      1. Airtable 'BD Config' table  (Key=daily_attempted, Value=100)
      2. Airtable 'BD Targets' table (any non-zero Daily Target row)
      3. config/team.json bd_targets.daily
    """
    # 1. BD Config table (simplest for non-technical users to edit)
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

    # 2. BD Targets table
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

    # 3. team.json fallback
    try:
        with open(TEAM_PATH) as fh:
            bt = json.load(fh).get("bd_targets", {})
        return {k: v for k, v in bt.get("daily", {}).items() if v}
    except Exception:
        return {}


# ── Email body builders ───────────────────────────────────────────────────────

def _fmt_tgt(daily: int) -> str:
    return "—" if not daily else str(daily)

def _fmt_win(daily: int) -> str:
    return "—" if not daily else str(daily // 4)


def _build_first_half(name: str, today: str, bd: dict, targets: dict) -> tuple:
    """Returns (subject, body) for the 1:30 PM window report."""
    sep = "─" * 52
    rows = []
    for key in METRICS:
        done = bd.get(key, 0)
        d    = targets.get(key, 0)
        rows.append(f"  {LABEL[key]:<22} {done:>5}    {_fmt_tgt(d):>6}    {_fmt_win(d):>6}")

    body = "\n".join([
        f"Hi {name},",
        "",
        f"BD Activity  ·  {today}  ·  11:00 AM – 1:00 PM",
        sep,
        f"  {'Metric':<22} {'Done':>5}    {'Daily':>6}    {'Window':>6}",
        sep,
        *rows,
        sep,
        "",
        "Afternoon window: 3:00 PM – 6:00 PM",
        "",
        "— Kylas Sync",
    ])
    subject = f"BD | {name} | {today} | 11 AM Window"
    return subject, body


def _build_full_day(name: str, today: str, bd: dict, targets: dict) -> tuple:
    """Returns (subject, body) for the 6:30 PM end-of-day report."""
    w1          = bd.get("w1", {})
    w2          = bd.get("w2", {})
    has_windows = any(w1.get(k, 0) for k in METRICS)
    sep = "─" * 62

    if has_windows:
        hdr  = f"  {'Metric':<22} {'W1 (11–1)':>9}  {'W2 (3–6)':>8}  {'Total':>6}  {'Daily':>6}"
        rows = []
        for key in METRICS:
            d    = targets.get(key, 0)
            rows.append(
                f"  {LABEL[key]:<22} {w1.get(key,0):>9}  {w2.get(key,0):>8}"
                f"  {bd.get(key,0):>6}  {_fmt_tgt(d):>6}"
            )
    else:
        hdr  = f"  {'Metric':<22} {'Done':>5}  {'Daily':>6}  {'Window':>6}"
        rows = []
        for key in METRICS:
            d = targets.get(key, 0)
            rows.append(f"  {LABEL[key]:<22} {bd.get(key,0):>5}  {_fmt_tgt(d):>6}  {_fmt_win(d):>6}")

    body = "\n".join([
        f"Hi {name},",
        "",
        f"BD Activity  ·  {today}  ·  End of Day",
        sep,
        hdr,
        sep,
        *rows,
        sep,
        "",
        "— Kylas Sync",
    ])
    subject = f"BD | {name} | {today} | EOD"
    return subject, body


# ── SMTP send ─────────────────────────────────────────────────────────────────

def _send(smtp_user: str, smtp_pass: str, to: str, subject: str, body: str, cc: list):
    msg            = MIMEMultipart()
    msg["From"]    = smtp_user
    msg["To"]      = to
    msg["Subject"] = subject
    if cc:
        msg["CC"] = ", ".join(cc)
    msg.attach(MIMEText(body, "plain"))
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


def send_alert(stats: dict, slot: str = "test", bd_enriched: dict = None):
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    if not smtp_user or not smtp_pass:
        print("[Email] SMTP_USER / SMTP_PASS not set — skipping")
        return

    today   = date.today().strftime("%d %b %Y")
    targets = _load_daily_targets()

    with open(TEAM_PATH) as fh:
        cfg = json.load(fh)

    cc_list = cfg.get("cc", [])
    # BD team gets BD activity emails
    bd_team = cfg.get("bd_team", cfg.get("members", []))

    for member in bd_team:
        name  = member["name"]
        email = member["email"]
        bd    = _member_bd(name, bd_enriched or {})

        if not bd:
            print(f"[Email] No BD data for {name} — skipping")
            continue

        if slot == "first_half":
            subject, body = _build_first_half(name, today, bd, targets)
        else:
            subject, body = _build_full_day(name, today, bd, targets)

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
    send_alert({}, args.slot, bd_enriched=test_bd)
