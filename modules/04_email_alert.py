import argparse
import json
import os
import sys
from datetime import date

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


def send_alert(stats: dict, slot: str = "test"):
    import resend
    resend.api_key = os.environ["RESEND_API_KEY"]

    today      = date.today().strftime("%d %b %Y")
    slot_label = slot.replace("_", " ").title()
    from_email = os.environ.get("RESEND_FROM_EMAIL", "onboarding@resend.dev")

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
            f"Tera Kylas Sync Report\n"
            f"{'=' * 36}\n"
            f"Date  : {today}\n"
            f"Slot  : {slot_label}\n\n"
            f"Companies : {co['created']} new  |  {co['updated']} updated\n"
            f"Contacts  : {ct['created']} new  |  {ct['updated']} updated\n"
            f"Deals     : {d['created']} new  |  {d['updated']} updated\n"
            f"{'=' * 36}\n"
        )

        try:
            resend.Emails.send({
                "from":    from_email,
                "to":      [email],
                "subject": f"Tera Kylas Report — {today} | {slot_label}",
                "text":    body,
            })
            print(f"[Email] Sent to {name} <{email}>")
        except Exception as e:
            print(f"[Email] WARNING: could not send to {name} <{email}>: {e}")
            if "verify a domain" in str(e) or "testing emails" in str(e):
                print("[Email] Fix: verify enout.in at resend.com/domains, then set")
                print("[Email]      RESEND_FROM_EMAIL secret to noreply@enout.in")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--slot", default="test")
    args = parser.parse_args()
    from dotenv import load_dotenv; load_dotenv()

    test_stats = {
        "companies": {"created": 5, "updated": 3, "failed": 0, "per_user": {
            "Ayush Tiwari":    {"created": 3, "updated": 2},
            "Bhamumik Patel": {"created": 2, "updated": 1},
        }},
        "contacts": {"created": 12, "updated": 5, "failed": 0, "per_user": {
            "Ayush Tiwari":    {"created": 8, "updated": 3},
            "Bhamumik Patel": {"created": 4, "updated": 2},
        }},
        "deals": {"created": 4, "updated": 2, "failed": 0, "per_user": {
            "Ayush Tiwari":    {"created": 3, "updated": 1},
            "Bhamumik Patel": {"created": 1, "updated": 1},
        }},
    }
    send_alert(test_stats if args.test else {}, args.slot)
