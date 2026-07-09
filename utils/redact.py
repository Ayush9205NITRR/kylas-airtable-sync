"""Redaction helpers for log output (Actions logs may be public)."""


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
