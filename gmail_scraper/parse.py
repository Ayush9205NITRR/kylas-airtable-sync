"""
Turn a raw Gmail thread payload into the flat row we store in Airtable.

Pure functions, no network — this is the part the tests exercise.
"""
import re
from datetime import datetime
from email.utils import getaddresses

from gmail_scraper import config

# "Re:", "Fwd:", "RE :", "FW:" and any pile-up of them at the start of a subject.
_PREFIX_RE = re.compile(r"^\s*(?:(?:re|fwd?|aw|antwort)\s*:\s*)+", re.IGNORECASE)


def header(message: dict, name: str) -> str:
    """Case-insensitive header lookup on a Gmail message resource."""
    wanted = name.lower()
    for h in message.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == wanted:
            return h.get("value", "") or ""
    return ""


def addresses(value: str) -> list:
    """Extract lowercase email addresses from a header value, order preserved."""
    out = []
    for _, addr in getaddresses([value or ""]):
        addr = addr.strip().lower()
        if "@" in addr and addr not in out:
            out.append(addr)
    return out


def display_name(value: str) -> str:
    """The human name from a From header, falling back to the local part."""
    parsed = getaddresses([value or ""])
    if not parsed:
        return ""
    name, addr = parsed[0]
    return name.strip().strip('"') or addr.split("@")[0]


def clean_subject(subject: str) -> str:
    """Strip Re:/Fwd: noise so the thread reads under its original subject."""
    return _PREFIX_RE.sub("", subject or "").strip()


def attachment_names(payload: dict) -> list:
    """Walk the MIME tree and collect real attachment filenames.

    Parts with a filename but no attachmentId are inline/nested bodies, so we
    key off attachmentId (or a non-zero size) to avoid listing signature images
    as attachments where we can.
    """
    found = []

    def walk(part):
        filename = (part.get("filename") or "").strip()
        body = part.get("body", {}) or {}
        if filename and (body.get("attachmentId") or body.get("size")):
            if filename not in found:
                found.append(filename)
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload or {})
    return found


def _ist(ms: int) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000, tz=config.IST)


def thread_to_record(thread: dict, category: str, all_categories=None,
                     mailbox: str = "") -> dict:
    """Flatten a Gmail thread into Airtable field values.

    * Sender / Subject come from the **first** message (who started it).
    * CC is the union across every message — someone looped in on reply 4 still
      counts as a participant of the conversation.
    * Dates come from internalDate, which is Gmail's own receive timestamp and
      immune to malformed Date: headers.
    """
    messages = sorted(thread.get("messages", []),
                      key=lambda m: int(m.get("internalDate", 0)))
    if not messages:
        return {}

    first, last = messages[0], messages[-1]

    cc, to, files = [], [], []
    for msg in messages:
        for addr in addresses(header(msg, "Cc")):
            if addr not in cc:
                cc.append(addr)
        for addr in addresses(header(msg, "To")):
            if addr not in to:
                to.append(addr)
        for name in attachment_names(msg.get("payload", {})):
            if name not in files:
                files.append(name)

    from_header = header(first, "From")
    sender = addresses(from_header)
    thread_id = thread.get("id", "")

    return {
        config.KEY_FIELD: thread_id,
        "Category": category,
        "All Categories": ", ".join(all_categories or [category]),
        "Subject": clean_subject(header(first, "Subject")) or "(no subject)",
        "Sender Email": sender[0] if sender else "",
        "Sender Name": display_name(from_header),
        "To Emails": ", ".join(to),
        "CC Emails": ", ".join(cc),
        "First Email Date": _ist(first["internalDate"]).isoformat(timespec="seconds"),
        "Last Email Date": _ist(last["internalDate"]).isoformat(timespec="seconds"),
        "Attachments": ", ".join(files),
        "Attachment Count": len(files),
        "Message Count": len(messages),
        "Snippet": (first.get("snippet") or "").strip(),
        "Gmail Link": f"https://mail.google.com/mail/u/0/#all/{thread_id}",
        "Mailbox": mailbox,
        "Synced At": config.now_ist_iso(),
    }
