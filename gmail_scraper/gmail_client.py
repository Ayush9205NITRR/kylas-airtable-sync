"""
Gmail API (v1) access for the thread scraper.

Search returns *threads*, not individual messages, because the fields we want
(first date, last date, everyone CC'd anywhere in the conversation) are
properties of the whole conversation.

Auth: an OAuth refresh token if one is configured, otherwise a service account
impersonating GMAIL_USER. See config.py for the env vars.
"""
import json
import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gmail_scraper import config

_SERVICE = None
_TRANSIENT_STATUS = (403, 429, 500, 502, 503, 504)


def _credentials():
    if config.GMAIL_REFRESH_TOKEN:
        if not (config.GMAIL_CLIENT_ID and config.GMAIL_CLIENT_SECRET):
            raise RuntimeError(
                "GMAIL_REFRESH_TOKEN is set but GMAIL_CLIENT_ID / "
                "GMAIL_CLIENT_SECRET are missing"
            )
        from google.oauth2.credentials import Credentials
        return Credentials(
            token=None,
            refresh_token=config.GMAIL_REFRESH_TOKEN,
            client_id=config.GMAIL_CLIENT_ID,
            client_secret=config.GMAIL_CLIENT_SECRET,
            token_uri=config.GMAIL_TOKEN_URI,
            scopes=config.SCOPES,
        )

    raw = (config.GOOGLE_SERVICE_ACCOUNT_JSON or "").strip()
    if not raw:
        raise RuntimeError(
            "No Gmail credentials. Set GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / "
            "GMAIL_REFRESH_TOKEN (run `python -m gmail_scraper.auth_setup`), or "
            "GOOGLE_SERVICE_ACCOUNT_JSON + GMAIL_USER with domain-wide delegation."
        )
    if not config.GMAIL_USER:
        raise RuntimeError(
            "GMAIL_USER must be set when authenticating with a service account "
            "(it is the mailbox to impersonate)"
        )
    from google.oauth2 import service_account
    if raw.startswith("{"):
        creds = service_account.Credentials.from_service_account_info(
            json.loads(raw), scopes=config.SCOPES)
    else:
        creds = service_account.Credentials.from_service_account_file(
            raw, scopes=config.SCOPES)
    return creds.with_subject(config.GMAIL_USER)


def _service():
    global _SERVICE
    if _SERVICE is None:
        from googleapiclient.discovery import build
        _SERVICE = build("gmail", "v1", credentials=_credentials(),
                         cache_discovery=False)
    return _SERVICE


def _execute(request):
    """Run a Gmail API request with backoff on rate limits / 5xx."""
    from googleapiclient.errors import HttpError
    for attempt in range(5):
        try:
            return request.execute()
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            if status in _TRANSIENT_STATUS and attempt < 4:
                wait = 2 ** attempt
                print(f"[gmail] transient {status}, retry in {wait}s "
                      f"({attempt + 1}/5)")
                time.sleep(wait)
                continue
            raise
    raise RuntimeError("Gmail request failed after 5 attempts")


def granted_scopes() -> list:
    """Scopes Google actually granted this token — not the ones we asked for.

    Requesting a scope and holding it are different things: an OAuth consent
    where the user unticked a box, or a service account whose domain-wide
    delegation lists different scopes, both yield a token that authenticates
    fine and then 403s on the first real call. This asks Google directly.
    """
    creds = _credentials()
    from google.auth.transport.requests import Request
    creds.refresh(Request())
    r = requests.get("https://oauth2.googleapis.com/tokeninfo",
                     params={"access_token": creds.token}, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"tokeninfo {r.status_code}: {r.text[:200]}")
    return (r.json().get("scope") or "").split()


def whoami() -> str:
    """The address the credentials actually resolve to — worth printing once."""
    profile = _execute(_service().users().getProfile(userId="me"))
    return profile.get("emailAddress", "")


def search_thread_ids(query: str, max_threads: int = None) -> list:
    """Thread ids matching a Gmail search query, newest first."""
    max_threads = max_threads or config.MAX_THREADS_PER_CATEGORY
    svc = _service()
    ids, token = [], None
    while len(ids) < max_threads:
        page_size = min(100, max_threads - len(ids))
        resp = _execute(svc.users().threads().list(
            userId="me", q=query, maxResults=page_size, pageToken=token))
        ids.extend(t["id"] for t in resp.get("threads", []))
        token = resp.get("nextPageToken")
        if not token:
            break
    return ids[:max_threads]


def download_attachment(message_id: str, attachment_id: str) -> bytes:
    """Raw bytes of one attachment (Gmail returns base64url-encoded data)."""
    import base64
    resp = _execute(_service().users().messages().attachments().get(
        userId="me", messageId=message_id, id=attachment_id))
    return base64.urlsafe_b64decode(resp.get("data", ""))


def get_thread(thread_id: str) -> dict:
    """Full thread payload.

    ``format=full`` (not ``metadata``) is required: attachment filenames live in
    the MIME part tree, which the metadata format omits.
    """
    return _execute(_service().users().threads().get(
        userId="me", id=thread_id, format="full"))


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    print(f"Authenticated as: {whoami()}")
    for cat in config.load_categories():
        found = search_thread_ids(cat["query"], max_threads=10)
        print(f"  {cat['name']:<16} {cat['query']!r} -> {len(found)} thread(s) "
              f"(capped at 10 for this probe)")
