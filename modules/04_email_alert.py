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
    per_user = stats.get(module, {}).get("per_user", {})
    result = {"created": 0, "updated": 0}
    for kylas_name, s in per_user.items():
        if member_name.lower() in kylas_name.lower():
            result["created"] += s.get("created", 0)
            result["updated"] += s.get("updated", 0)
    return result


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

        body = (
            f"Hi {name},\n\n"
            f"Kylas Sync Report\n"
            f"{'=' * 36}\n"
            f"Date  : {today}\n"
            f"Slot  : {slot_label}\n\n"
            f"Companies : {co['created']} new  |  {co['updated']} updated\n"
            f"Contacts  : {ct['created']} new  |  {ct['updated']} updated\n"
            f"Deals     : {d['created']} new  |  {d['updated']} updated\n"
            f"{'=' * 36}\n\n"
            f"-- Kylas Sync Bot"
        )

        try:
            _send_smtp(smtp_user, smtp_pass, email,
                       f"Kylas Sync Report — {today} | {slot_label}", body)
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
            "Ayush Tiwari":   {"created": 3, "updated": 2},
            "Bhaumik Sachdeva": {"created": 2, "updated": 1},
        }},
        "contacts": {"created": 12, "updated": 5, "failed": 0, "per_user": {
            "Ayush Tiwari":   {"created": 8, "updated": 3},
            "Bhaumik Sachdeva": {"created": 4, "updated": 2},
        }},
        "deals": {"created": 4, "updated": 2, "failed": 0, "per_user": {
            "Ayush Tiwari":   {"created": 3, "updated": 1},
            "Bhaumik Sachdeva": {"created": 1, "updated": 1},
        }},
    }
    send_alert(test_stats, args.slot)
