"""
Send an iCal (.ics) calendar invite via SMTP for scheduled calls.

Attaches a text/calendar; method=REQUEST part so Gmail / Google Calendar
surfaces the accept/decline widget automatically. Uses the Kylas contact
ID as the VEVENT UID so a changed next-call date sends an *update* to the
same calendar event rather than creating a duplicate.
"""
import os
import smtplib
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

ADMIN_EMAIL   = "ayush@enout.in"
VEDANT_EMAIL  = "vedant@enout.in"


def _esc(text: str) -> str:
    """Escape characters that are special inside iCal property values."""
    return (str(text or "")
            .replace("\\", "\\\\")
            .replace(";", "\\;")
            .replace(",", "\\,")
            .replace("\n", "\\n")
            .replace("\r", ""))


def _build_ical(
    *,
    contact_id: str,
    contact_name: str,
    contact_email: str,
    contact_phone: str,
    company_name: str,
    remarks: str,
    call_date: date,
    owner_email: str,
    organizer_email: str,
    kylas_url: str = "",
) -> str:
    """Return an RFC 5545 VCALENDAR string (all-day event, METHOD:REQUEST)."""
    # UID includes the date so changing the call date creates a NEW event
    # rather than updating the old one — the old date's event stays intact.
    uid   = f"call-{contact_id}-{call_date.strftime('%Y%m%d')}@kylas-sync"
    dt_s  = call_date.strftime("%Y%m%d")
    dt_e  = (call_date + timedelta(days=1)).strftime("%Y%m%d")

    desc = (
        "hi you have a call scheduled with\\n"
        f"Kylas URL - {_esc(kylas_url)}\\n"
        f"Name - {_esc(contact_name)}\\n"
        f"Email - {_esc(contact_email)}\\n"
        f"Phone No. - {_esc(contact_phone)}\\n"
        f"Company - {_esc(company_name)}\\n"
        f"Remarks - {_esc(remarks)}"
    )
    summary = _esc(f"Call: {contact_name}" + (f" ({company_name})" if company_name else ""))

    base = {ADMIN_EMAIL.lower(), VEDANT_EMAIL.lower()}
    if owner_email:
        base.add(owner_email.lower())
    recipients = sorted(base)
    attendee_lines = "\r\n".join(
        "ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;"
        f"PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{em}"
        for em in recipients
    )

    parts = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Kylas Sync//EN",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTART;VALUE=DATE:{dt_s}",
        f"DTEND;VALUE=DATE:{dt_e}",
        f"SUMMARY:{summary}",
        f"DESCRIPTION:{desc}",
        f"ORGANIZER;CN=Kylas Sync:mailto:{organizer_email}",
        attendee_lines,
        "STATUS:CONFIRMED",
        "TRANSP:OPAQUE",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(parts)


def send_invite(
    *,
    contact_id: str,
    contact_name: str,
    contact_email: str,
    contact_phone: str,
    company_name: str,
    remarks: str,
    call_date: date,
    owner_email: str,
    kylas_url: str = "",
) -> bool:
    """
    Send a calendar invite to owner_email + ayush@enout.in + vedant@enout.in.
    Reads SMTP_USER / SMTP_PASS from the environment.
    Returns True on success.
    """
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    if not smtp_user or not smtp_pass:
        print("[CalendarInvite] SMTP not configured — skipping invite")
        return False
    if not owner_email:
        print(f"[CalendarInvite] No owner email for {contact_name!r} — sending to admin only")

    base_recipients = {ADMIN_EMAIL.lower(), VEDANT_EMAIL.lower()}
    if owner_email:
        base_recipients.add(owner_email.lower())
    recipients = sorted(base_recipients)
    cal_str    = _build_ical(
        contact_id=contact_id,
        contact_name=contact_name,
        contact_email=contact_email,
        contact_phone=contact_phone,
        company_name=company_name,
        remarks=remarks,
        call_date=call_date,
        owner_email=owner_email,
        organizer_email=smtp_user,
        kylas_url=kylas_url,
    )

    date_label = call_date.strftime("%b %d, %Y")
    subject    = f"Call Scheduled: {contact_name}" + (f" ({company_name})" if company_name else "")
    subject   += f" — {date_label}"

    body_text = (
        f"A call has been scheduled.\n\n"
        f"Name:    {contact_name}\n"
        f"Email:   {contact_email}\n"
        f"Phone:   {contact_phone}\n"
        f"Company: {company_name}\n"
        f"Date:    {date_label}\n"
        f"Kylas:   {kylas_url}\n"
        f"Remarks: {remarks}\n\n"
        "Accept the calendar invitation to block your calendar."
    )

    msg            = MIMEMultipart("mixed")
    msg["From"]    = smtp_user
    msg["To"]      = ", ".join(recipients)
    msg["Subject"] = subject
    msg.attach(MIMEText(body_text, "plain", "utf-8"))

    cal_part = MIMEText(cal_str, _subtype="calendar")
    cal_part.set_param("method", "REQUEST")
    cal_part.set_param("charset", "utf-8")
    cal_part.add_header("Content-Disposition", "attachment", filename="invite.ics")
    msg.attach(cal_part)

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as s:
            s.ehlo()
            s.starttls()
            s.login(smtp_user, smtp_pass)
            s.sendmail(smtp_user, recipients, msg.as_string())
        print(f"[CalendarInvite] Sent → {contact_name!r} on {date_label}, "
              f"recipients: {', '.join(recipients)}")
        return True
    except Exception as exc:
        print(f"[CalendarInvite] ERROR for {contact_name!r}: {exc}")
        return False
