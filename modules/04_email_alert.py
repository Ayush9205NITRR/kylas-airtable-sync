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


def _member_stats(member_name: str, stats: dict, module: str) -> dict:
    """
    Variables pulled into the email per person:
      co['created']  → new companies linked to this owner in today's sync
      co['updated']  → companies updated
      ct['created']  → new contacts
      ct['updated']  → contacts updated
      d['created']   → new deals
      d['updated']   → deals updated
    All sourced from stats[module]['per_user'][kylas_owner_name].
    """
    per_user = stats.get(module, {}).get("per_user", {})
    result = {"created": 0, "updated": 0}
    for kylas_name, s in per_user.items():
        if member_name.lower() in kylas_name.lower():
            result["created"] += s.get("created", 0)
            result["updated"] += s.get("updated", 0)
    return result


def _encouragement(total: int) -> str:
    if total == 0:
        return (
            "The pipeline doesn't build itself — and you're the one who does.\n"
            "Even quiet days count. Tomorrow is yours to own."
        )
    elif total < 10:
        return (
            "Every single entry matters. Small steps, big results.\n"
            "You're putting in the work that compounds over time!"
        )
    elif total < 30:
        return (
            "Solid day! Consistency like this is what separates good from great.\n"
            "Keep that energy going!"
        )
    else:
        return (
            "Incredible hustle today! That kind of volume is how pipelines get built.\n"
            "The team is proud of the effort you're putting in!"
        )


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


def _build_body(name: str, today: str, slot_label: str,
                co: dict, ct: dict, d: dict) -> str:
    """
    Template variables:
      {name}        → member name from config/team.json
      {today}       → current date e.g. "05 Jun 2026"
      {slot_label}  → "First Half" (1:30 PM run) or "Full Day" (6:00 PM run)
      {co/ct/d}['created']  → new records added this sync
      {co/ct/d}['updated']  → existing records refreshed
      {total}       → sum of all creates + updates
    """
    total = (co["created"] + co["updated"]
             + ct["created"] + ct["updated"]
             + d["created"]  + d["updated"])

    return (
        f"Hi {name}!\n\n"
        f"Great work today — here's your Kylas sync summary:\n\n"
        f"  Date  : {today}\n"
        f"  Slot  : {slot_label}\n\n"
        f"{'=' * 40}\n"
        f"  ACTIVITY SUMMARY\n"
        f"{'=' * 40}\n"
        f"  Companies :  {co['created']:>3} new   |  {co['updated']:>3} updated\n"
        f"  Contacts  :  {ct['created']:>3} new   |  {ct['updated']:>3} updated\n"
        f"  Deals     :   {d['created']:>3} new   |   {d['updated']:>3} updated\n"
        f"{'─' * 40}\n"
        f"  Total     :  {total:>3} records synced to Airtable\n"
        f"{'=' * 40}\n\n"
        f"{_encouragement(total)}\n\n"
        f"Keep going — every contact is a future opportunity.\n"
        f"You've got this!\n\n"
        f"Cheers,\n"
        f"Kylas Sync Bot\n"
        f"(data live in Airtable — Sales Pipeline base)"
    )


def send_alert(stats: dict, slot: str = "test"):
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")

    if not smtp_user or not smtp_pass:
        print("[Email] SMTP_USER / SMTP_PASS secrets not set — skipping email alert")
        return

    today      = date.today().strftime("%d %b %Y")
    slot_label = slot.replace("_", " ").title()

    with open(TEAM_PATH) as f:
        team = json.load(f)["members"]

    for member in team:
        name  = member["name"]
        email = member["email"]

        co = _member_stats(name, stats, "companies")
        ct = _member_stats(name, stats, "contacts")
        d  = _member_stats(name, stats, "deals")

        body    = _build_body(name, today, slot_label, co, ct, d)
        subject = f"Kylas BD Update — {today} | {slot_label} | Keep Going!"

        try:
            _send_smtp(smtp_user, smtp_pass, email, subject, body)
            print(f"[Email] Sent to {name} <{email}>")
        except Exception as e:
            print(f"[Email] WARNING: could not send to {name} <{email}>: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot", default="test")
    args = parser.parse_args()
    from dotenv import load_dotenv; load_dotenv()

    test_stats = {
        "companies": {"created": 5, "updated": 3, "failed": 0, "per_user": {
            "Ayush":   {"created": 3, "updated": 2},
            "Bhaumik": {"created": 2, "updated": 1},
        }},
        "contacts": {"created": 12, "updated": 5, "failed": 0, "per_user": {
            "Ayush":   {"created": 8, "updated": 3},
            "Bhaumik": {"created": 4, "updated": 2},
        }},
        "deals": {"created": 4, "updated": 2, "failed": 0, "per_user": {
            "Ayush":   {"created": 3, "updated": 1},
            "Bhaumik": {"created": 1, "updated": 1},
        }},
    }
    send_alert(test_stats, args.slot)
