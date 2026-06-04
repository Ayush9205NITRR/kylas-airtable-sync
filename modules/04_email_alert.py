import argparse
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def send_alert(stats: dict, slot: str = "test"):
    import resend

    resend.api_key = os.environ["RESEND_API_KEY"]
    today      = date.today().strftime("%d %b %Y")
    slot_label = slot.replace("_", " ").title()

    c  = stats.get("companies", {})
    co = stats.get("contacts",  {})
    d  = stats.get("deals",     {})
    total_failed = c.get("failed", 0) + co.get("failed", 0) + d.get("failed", 0)

    body = (
        f"Kylas Airtable Sync Report\n"
        f"{'=' * 36}\n"
        f"Date  : {today}\n"
        f"Slot  : {slot_label}\n\n"
        f"Companies : {c.get('created', 0)} new  |  {c.get('updated', 0)} updated\n"
        f"Contacts  : {co.get('created', 0)} new  |  {co.get('updated', 0)} updated\n"
        f"Deals     : {d.get('created', 0)} new  |  {d.get('updated', 0)} updated\n\n"
        f"Errors    : {total_failed}\n"
        f"{'=' * 36}\n"
    )

    from_email = os.environ.get("RESEND_FROM_EMAIL", "onboarding@resend.dev")
    to_email   = os.environ.get("ALERT_EMAIL", "ayushtiwari9205@gmail.com")

    resend.Emails.send({
        "from":    from_email,
        "to":      [to_email],
        "subject": f"Kylas Sync — {today} | {slot_label}",
        "text":    body,
    })
    print(f"[Email] Alert sent to {to_email}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--slot", default="test")
    args = parser.parse_args()
    from dotenv import load_dotenv; load_dotenv()

    test_stats = {
        "companies": {"created": 3, "updated": 5, "failed": 0},
        "contacts":  {"created": 10, "updated": 2, "failed": 1},
        "deals":     {"created": 1,  "updated": 4, "failed": 0},
    }
    send_alert(test_stats if args.test else {}, args.slot)
