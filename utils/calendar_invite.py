"""
Send an iCal (.ics) calendar invite via SMTP for scheduled calls.

Attaches a text/calendar; method=REQUEST part so Gmail / Google Calendar
surfaces the accept/decline widget automatically. Uses the Kylas contact
ID as the VEVENT UID so a changed next-call date sends an *update* to the
same calendar event rather than creating a duplicate.
"""
import os
import smtplib
import time
from datetime import date, datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from utils.redact import mask_emails

ADMIN_EMAIL   = "ayush@enout.in"
VEDANT_EMAIL  = "vedant@enout.in"

IST = timezone(timedelta(hours=5, minutes=30))


def parse_kylas_datetime(raw) -> tuple:
    """Return (date_str, time_str) in IST from a Next Call Date value.

    time_str is 'HH:MM:SS', or '' when the value carries no time of day.

    Kylas DATETIME_PICKER fields come back as UTC ("2026-10-19T03:30:00.000Z").
    They were once read as if the clock digits were already IST, which put 136
    of 156 invites at 03:30 AM -- that is the picker's 9:00 AM IST default
    (03:30Z) shifted 5h30m early. The conversion can also move the DATE (18:45Z
    is 00:15 IST the next day), so the date is taken from the converted value.

    The "Oct 19, 2026 at 11:00 AM" display form carries no zone and is already
    local, so it is read as-is.
    """
    raw = (raw or "").strip()
    if not raw:
        return "", ""
    if raw[0].isdigit():
        if len(raw) <= 10 or raw[10] != "T":
            return raw[:10], ""
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            # Unreadable time: an all-day invite is honest; a guessed hour is not.
            return raw[:10], ""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(IST)
        return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M:%S")
    try:
        parts = raw.split(" at ")
        date_str = datetime.strptime(parts[0].strip(), "%b %d, %Y").strftime("%Y-%m-%d")
        time_str = ""
        if len(parts) > 1:
            try:
                time_str = datetime.strptime(parts[1].strip(), "%I:%M %p").strftime("%H:%M:%S")
            except ValueError:
                pass
        return date_str, time_str
    except ValueError:
        return "", ""


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

# Seconds to wait before the 2nd and 3rd attempt. Every invite opens a fresh
# Gmail connection and logs in again, and Gmail drops those ("Connection
# unexpectedly closed") once they come fast enough -- a 156-invite catch-up on
# 2026-10-18 sent 83 and lost 73 that way.
_RETRY_DELAYS = (10, 30)


def _is_transient(exc: Exception) -> bool:
    """True for failures worth retrying: dropped/refused connections, timeouts
    and Gmail's 4xx "try again later". Auth failures and permanent 5xx
    rejections are not -- a retry just repeats them."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return False
    if isinstance(exc, (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError)):
        return True
    if isinstance(exc, smtplib.SMTPResponseException):
        return 400 <= exc.smtp_code < 500
    return isinstance(exc, OSError)      # ConnectionError, TimeoutError, ...


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
        # call_time is already IST (see parse_kylas_datetime)
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
    # An update to an existing event only takes if its SEQUENCE is higher than
    # the one already on the calendar. The invites used to carry none (0), so a
    # corrected time could not replace a wrong one. Minutes since the epoch
    # always grows between sends and stays well inside a 32-bit integer.
    now = _now_utc()
    parts += [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{now.strftime('%Y%m%dT%H%M%SZ')}",
        f"SEQUENCE:{int(now.timestamp()) // 60}",
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

    raw = msg.as_string()
    attempts = len(_RETRY_DELAYS) + 1
    for attempt in range(attempts):
        try:
            with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
                s.ehlo()
                s.starttls()
                s.login(smtp_user, smtp_pass)
                s.sendmail(smtp_user, recipients, raw)
            print(f"[CalendarInvite] Sent → {contact_name!r} on {date_label}, "
                  f"recipients: {mask_emails(recipients)}")  # date_label already includes time if present
            return True
        except Exception as exc:
            if _is_transient(exc) and attempt < attempts - 1:
                delay = _RETRY_DELAYS[attempt]
                print(f"[CalendarInvite] {exc} for {contact_name!r} — retrying "
                      f"in {delay}s (attempt {attempt + 2}/{attempts})")
                time.sleep(delay)
                continue
            print(f"[CalendarInvite] ERROR for {contact_name!r}: {exc}")
            return False
    return False
