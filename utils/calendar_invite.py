"""
Send an iCal (.ics) calendar invite via SMTP for scheduled calls.

Attaches a text/calendar; method=REQUEST part so Gmail / Google Calendar
surfaces the accept/decline widget automatically. Uses the Kylas contact
ID as the VEVENT UID so a changed next-call date sends an *update* to the
same calendar event rather than creating a duplicate.
"""
import os
import smtplib
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from utils.redact import mask_emails

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
    call_time: str = "",
) -> str:
    """Return an RFC 5545 VCALENDAR string (timed or all-day, METHOD:REQUEST)."""
    # UID includes the date so changing the call date creates a NEW event
    # rather than updating the old one — the old date's event stays intact.
    uid = f"call-{contact_id}-{call_date.strftime('%Y%m%d')}@kylas-sync"

    if call_time:
        # Treat stored time as IST (Kylas stores local time with Z suffix)
        time_compact = call_time.replace(":", "")[:6]  # HHMMSS
        end_dt = datetime.strptime(f"{call_date}T{call_time}", "%Y-%m-%dT%H:%M:%S") + timedelta(hours=1)
        dtstart_line = f"DTSTART;TZID=Asia/Kolkata:{call_date.strftime('%Y%m%d')}T{time_compact}"
        dtend_line   = f"DTEND;TZID=Asia/Kolkata:{end_dt.strftime('%Y%m%dT%H%M%S')}"
        # Add VTIMEZONE block before VEVENT
        vtimezone = (
            "BEGIN:VTIMEZONE\r\n"
            "TZID:Asia/Kolkata\r\n"
            "BEGIN:STANDARD\r\n"
            "TZOFFSETFROM:+0530\r\n"
            "TZOFFSETTO:+0530\r\n"
            "TZNAME:IST\r\n"
            "DTSTART:19700101T000000\r\n"
            "END:STANDARD\r\n"
            "END:VTIMEZONE"
        )
    else:
        dtstart_line = f"DTSTART;VALUE=DATE:{call_date.strftime('%Y%m%d')}"
        dtend_line   = f"DTEND;VALUE=DATE:{(call_date + timedelta(days=1)).strftime('%Y%m%d')}"
        vtimezone    = ""

    if call_time:
        t = datetime.strptime(call_time, "%H:%M:%S")
        time_label = t.strftime("%I:%M %p").lstrip("0") + " IST"
        date_label_str = f"{call_date.strftime('%b %d, %Y')} at {time_label}"
    else:
        date_label_str = call_date.strftime("%b %d, %Y")

    desc = (
        "hi you have a call scheduled with\\n"
        f"Kylas URL - {_esc(kylas_url)}\\n"
        f"Name - {_esc(contact_name)}\\n"
        f"Date & Time - {_esc(date_label_str)}\\n"
        f"Owner - {_esc(owner_email or '(unassigned)')}\\n"
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

    parts = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Kylas Sync//EN", "METHOD:REQUEST"]
    if vtimezone:
        parts.append(vtimezone)
    parts += [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        dtstart_line,
        dtend_line,
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
    call_time: str = "",
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
        call_time=call_time,
    )

    if call_time:
        t = datetime.strptime(call_time, "%H:%M:%S")
        time_label = t.strftime("%I:%M %p").lstrip("0") + " IST"
        date_label = f"{call_date.strftime('%b %d, %Y')} at {time_label}"
    else:
        date_label = call_date.strftime("%b %d, %Y")

    subject = f"Call Scheduled: {contact_name}" + (f" ({company_name})" if company_name else "")
    subject += f" — {date_label}"

    body_text = (
        f"A call has been scheduled.\n\n"
        f"Name:    {contact_name}\n"
        f"Company: {company_name}\n"
        f"Date:    {date_label}\n"
        f"Owner:   {owner_email or '(unassigned)'}\n"
        f"Email:   {contact_email}\n"
        f"Phone:   {contact_phone}\n"
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
              f"recipients: {mask_emails(recipients)}")  # date_label already includes time if present
        return True
    except Exception as exc:
        print(f"[CalendarInvite] ERROR for {contact_name!r}: {exc}")
        return False
