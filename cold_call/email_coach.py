"""
Daily coaching email (one per BD) via SMTP (Gmail), matching the Kylas sync.

Content (per the brief):
  - BD name + date
  - Calls analyzed, average score /100
  - Top miss of the day (most repeated issue)
  - 2-3 "better response" examples from weak/missed objections
  - Score breakdown bars (Hook / Objection / Pitch / Discovery)

BD email is resolved from config/team.json (bd_team) by name. If a BD has no
email, or SMTP_USER/SMTP_PASS are unset, the send is skipped with a log line.
"""
import json
import os
import sys
from collections import Counter
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cold_call import config

TEAM_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "team.json")


# ── Redaction (cold_call keeps its own dependency-free copy — see utils/redact.py;
# Actions logs may be public, so never print a raw email address) ──────────────
def mask_email(addr: str) -> str:
    """muskan@enout.in -> 'mu***@enout.in'. Non-emails returned unchanged."""
    addr = (addr or "").strip()
    if "@" not in addr:
        return addr
    local, _, domain = addr.partition("@")
    if len(local) <= 2:
        masked = "***"
    else:
        masked = local[:2] + "***"
    return f"{masked}@{domain}"


def mask_emails(value) -> str:
    """Mask a list of emails or a comma-separated string; return joined string."""
    if value is None:
        return ""
    items = value if isinstance(value, (list, tuple, set)) else str(value).split(",")
    return ", ".join(mask_email(str(x).strip()) for x in items if str(x).strip())


# Section -> (label, max points), in display order.
_SECTIONS = [
    ("hook", "Hook", 25),
    ("objection", "Objection Handling", 35),
    ("pitch", "Enout Pitch", 25),
    ("discovery", "Discovery Booked", 15),
]


def resolve_bd_email(bd_name: str) -> str:
    """Look up a BD's email from config/team.json (substring match on name)."""
    try:
        with open(TEAM_PATH) as fh:
            team = json.load(fh).get("bd_team", [])
    except Exception:
        return ""
    lo = bd_name.strip().lower()
    for m in team:
        nm = (m.get("name") or "").strip().lower()
        if nm and (nm in lo or lo in nm):
            return m.get("email", "")
    return ""


def _avg(calls: list, key: str) -> float:
    vals = [(c.get(key) or 0) for c in calls]
    return sum(vals) / len(vals) if vals else 0.0


def _bar(label: str, score: float, maxv: int) -> str:
    pct = int(round(100 * score / maxv)) if maxv else 0
    return (
        '<tr>'
        f'<td style="padding:5px 8px;font-size:13px;color:#333;white-space:nowrap;">{label}</td>'
        '<td style="padding:5px 8px;width:100%;">'
        '<div style="background:#eceff3;border-radius:4px;width:100%;height:14px;">'
        f'<div style="background:#2c7be5;height:14px;border-radius:4px;width:{pct}%;"></div>'
        '</div></td>'
        f'<td style="padding:5px 8px;font-size:13px;color:#333;white-space:nowrap;">'
        f'{score:.0f}/{maxv}</td></tr>'
    )


def _collect_better(calls: list, limit: int = 3) -> list:
    out = []
    for c in calls:
        for obj in c.get("objections_found", []) or []:
            if obj.get("handled") in ("weak", "missed") and obj.get("better_response"):
                out.append((obj.get("objection", "(objection)"), obj["better_response"]))
                if len(out) >= limit:
                    return out
    return out


def build_email_html(bd_name: str, calls_today: list) -> str:
    n = len(calls_today)
    avg_total = _avg(calls_today, "total_score")

    misses = [(c.get("top_miss") or "").strip() for c in calls_today if c.get("top_miss")]
    top_miss = Counter(misses).most_common(1)[0][0] if misses else "—"

    bars = "".join(
        _bar(label, _avg(calls_today, f"{key}_score"), maxv)
        for key, label, maxv in _SECTIONS
    )
    better = _collect_better(calls_today)

    better_html = ""
    if better:
        items = "".join(
            '<div style="margin:0 0 14px;">'
            f'<div style="font-size:13px;color:#888;">They said:</div>'
            f'<div style="font-size:14px;color:#333;margin:2px 0 6px;">"{obj}"</div>'
            f'<div style="font-size:13px;color:#888;">Better response:</div>'
            f'<div style="font-size:14px;color:#1a7f37;">{resp}</div>'
            '</div>'
            for obj, resp in better
        )
        better_html = (
            '<p style="font-weight:bold;font-size:14px;margin:22px 0 8px;">'
            'Better responses to practise</p>' + items
        )

    body_style = ('style="font-family:Arial,sans-serif;color:#333;'
                  'max-width:640px;margin:0 auto;padding:24px 20px;"')
    return (
        f'<!DOCTYPE html><html><body {body_style}>'
        f'<p style="margin:0 0 4px;">Hi {bd_name},</p>'
        f'<p style="color:#666;font-size:13px;margin:0 0 18px;">'
        f'Call coaching · {date.today().strftime("%d %b %Y")}</p>'
        '<table style="border-collapse:collapse;margin:0 0 18px;"><tbody>'
        f'<tr><td style="padding:4px 16px 4px 0;font-size:13px;color:#666;">Calls analyzed</td>'
        f'<td style="font-size:18px;font-weight:bold;">{n}</td></tr>'
        f'<tr><td style="padding:4px 16px 4px 0;font-size:13px;color:#666;">Average score</td>'
        f'<td style="font-size:18px;font-weight:bold;">{avg_total:.0f}/100</td></tr>'
        '</tbody></table>'
        '<p style="font-weight:bold;font-size:14px;margin:0 0 6px;">Top miss of the day</p>'
        f'<p style="font-size:14px;color:#333;margin:0 0 18px;">{top_miss}</p>'
        '<p style="font-weight:bold;font-size:14px;margin:0 0 6px;">Score breakdown</p>'
        '<table style="border-collapse:collapse;width:100%;margin:0 0 8px;"><tbody>'
        f'{bars}</tbody></table>'
        + better_html +
        '<p style="color:#999;font-size:12px;margin:26px 0 0;">— Enout Cold Call Coach</p>'
        '</body></html>'
    )


def _send_smtp(to: str, subject: str, html: str, cc: list = None) -> None:
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    cc = cc or []
    msg = MIMEMultipart("alternative")
    msg["From"] = config.EMAIL_FROM
    msg["To"] = to
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as s:
        s.ehlo()
        s.starttls()
        s.login(config.SMTP_USER, config.SMTP_PASS)
        s.sendmail(config.SMTP_USER, [to] + cc, msg.as_string())


def send_coaching_email(bd_name: str, bd_email: str, calls_today: list) -> bool:
    if not calls_today:
        return False
    bd_email = bd_email or resolve_bd_email(bd_name)
    if not bd_email:
        print(f"[email] No email for {bd_name} — skipping")
        return False
    if not config.SMTP_USER or not config.SMTP_PASS:
        print("[email] SMTP_USER / SMTP_PASS not set — skipping send")
        return False

    avg_total = _avg(calls_today, "total_score")
    subject = (f"Your call coaching — {date.today().strftime('%d %b')} "
               f"({len(calls_today)} calls, avg {int(avg_total)}/100)")
    _send_smtp(bd_email, subject, build_email_html(bd_name, calls_today))
    print(f"[email] Sent coaching email → {bd_name} <{mask_email(bd_email)}>")
    return True


if __name__ == "__main__":
    import argparse
    from dotenv import load_dotenv
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--to", help="send a sample email to this address")
    parser.add_argument("--out", default="/tmp/coaching_sample.html",
                        help="write the sample HTML here")
    args = parser.parse_args()

    sample = [
        {"total_score": 62, "hook_score": 18, "objection_score": 20, "pitch_score": 17,
         "discovery_score": 7, "top_miss": "Did not book a specific time for the demo",
         "objections_found": [
             {"objection": "Abhi budget nahi hai", "type": "price", "handled": "weak",
              "better_response": "I understand — that's exactly why I'd like to show you the ROI "
                                 "first, then you decide. Can we set a 10-minute demo?"}]},
        {"total_score": 71, "hook_score": 20, "objection_score": 26, "pitch_score": 18,
         "discovery_score": 7, "top_miss": "Pitch was a bit long-winded",
         "objections_found": [
             {"objection": "Hum already ek tool use karte hain", "type": "competitor",
              "handled": "missed", "better_response": "Absolutely — and teams often run us "
              "alongside their current tool. Let me show you where it fills the gaps."}]},
    ]
    html = build_email_html("Rubal", sample)
    with open(args.out, "w") as fh:
        fh.write(html)
    print(f"Sample written to {args.out}")
    if args.to:
        # Send the sample straight to --to (bypasses team.json lookup).
        if not config.SMTP_USER or not config.SMTP_PASS:
            print("[email] SMTP_USER / SMTP_PASS not set — cannot send sample")
        else:
            subject = "Your call coaching — sample (2 calls, avg 66/100)"
            _send_smtp(args.to, subject, html)
            print(f"[email] Sample sent → {mask_email(args.to)}")
