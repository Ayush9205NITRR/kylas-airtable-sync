"""
Central configuration for the Gmail -> Airtable thread scraper.

Everything comes from environment variables (see .env.example). Two auth modes
are supported, tried in this order:

  1. OAuth refresh token  (GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET /
     GMAIL_REFRESH_TOKEN) — works for any Gmail account, personal or Workspace.
     Mint the refresh token once with `python -m gmail_scraper.auth_setup`.
  2. Service account with domain-wide delegation (GOOGLE_SERVICE_ACCOUNT_JSON +
     GMAIL_USER) — Workspace only, needs a Super Admin to authorise the
     client id for the gmail.readonly scope.

Mode 1 needs no admin console access, so it's the default recommendation.
"""
import json
import os
from datetime import datetime, timedelta, timezone

# ── Timezone ───────────────────────────────────────────────────────────────────
IST = timezone(timedelta(hours=5, minutes=30))

# ── Gmail ──────────────────────────────────────────────────────────────────────
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# The mailbox being scraped. With OAuth this is informational (the refresh token
# already identifies the account); with a service account it is the address to
# impersonate, so it is required there.
GMAIL_USER = os.environ.get("GMAIL_USER", "")

GMAIL_CLIENT_ID = os.environ.get("GMAIL_CLIENT_ID", "")
GMAIL_CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET", "")
GMAIL_REFRESH_TOKEN = os.environ.get("GMAIL_REFRESH_TOKEN", "")
GMAIL_TOKEN_URI = "https://oauth2.googleapis.com/token"

# Shared with the cold-call pipeline — same key file works if domain-wide
# delegation has been granted for the Gmail scope.
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")

# How far back to search when --since isn't passed. Gmail's `newer_than:` is
# day-granular, so this is in days.
DEFAULT_LOOKBACK_DAYS = int(os.environ.get("GMAIL_LOOKBACK_DAYS", "180"))

# Safety valve so a broad keyword can't pull the whole mailbox in one run.
MAX_THREADS_PER_CATEGORY = int(os.environ.get("GMAIL_MAX_THREADS", "500"))

# ── Categories ─────────────────────────────────────────────────────────────────
# Keyword -> category definitions live in config/email_categories.json so new
# categories can be added without touching code.
CATEGORIES_FILE = os.environ.get(
    "GMAIL_CATEGORIES_FILE",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "config", "email_categories.json"),
)

# ── Airtable ───────────────────────────────────────────────────────────────────
AIRTABLE_TOKEN = os.environ.get("AIRTABLE_PAT") or os.environ.get("AIRTABLE_TOKEN", "")
# Own base by preference so scraped mail never lands in the Kylas CRM base.
AIRTABLE_BASE_ID = (
    os.environ.get("GMAIL_AIRTABLE_BASE_ID")
    or os.environ.get("AIRTABLE_BASE_ID", "")
)
TABLE_NAME = os.environ.get("GMAIL_TABLE_NAME", "Email Threads")

# Dedup / upsert key. One Airtable row per Gmail thread.
KEY_FIELD = "Thread ID"


def now_ist_iso() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


def load_categories() -> list:
    """Read config/email_categories.json -> [{'name', 'query'}, ...].

    A category may specify either an explicit ``query`` (raw Gmail search
    syntax, used as-is) or a list of ``keywords`` which get OR'd together and
    combined with the file's ``default_scope`` (``in:anywhere`` by default).
    Order matters: when a thread matches several categories the first one wins.
    """
    with open(CATEGORIES_FILE, encoding="utf-8") as fh:
        data = json.load(fh)

    scope = data.get("default_scope", "in:anywhere").strip()
    out = []
    for entry in data.get("categories", []):
        name = entry.get("name", "").strip()
        if not name:
            continue
        query = entry.get("query", "").strip()
        if not query:
            keywords = [k.strip() for k in entry.get("keywords", []) if k.strip()]
            if not keywords:
                continue
            terms = " OR ".join(f'"{k}"' for k in keywords)
            query = f"({terms})" if len(keywords) > 1 else f'"{keywords[0]}"'
            if scope:
                query = f"{query} {scope}"
        out.append({"name": name, "query": query})
    return out


def since_clause(since: str) -> str:
    """Translate a --since value into a Gmail search clause.

    Accepts ``30d`` / ``6m`` / ``1y`` style shorthands, an absolute
    ``YYYY-MM-DD``, or ``all`` for no date restriction at all.
    """
    since = (since or "").strip().lower()
    if since in ("all", "any", "0"):
        return ""
    if not since:
        return f"newer_than:{DEFAULT_LOOKBACK_DAYS}d"
    if since[-1] in "dmy" and since[:-1].isdigit():
        return f"newer_than:{since}"
    if since.isdigit():
        return f"newer_than:{since}d"
    try:
        day = datetime.strptime(since, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(
            f"--since {since!r} not understood — use 30d / 6m / 1y / YYYY-MM-DD / all"
        ) from exc
    return f"after:{day:%Y/%m/%d}"
