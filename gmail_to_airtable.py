#!/usr/bin/env python3
"""
Gmail -> Airtable, in one standalone file. Nothing else from this repo needed.

    pip install google-api-python-client google-auth google-auth-oauthlib requests
    python gmail_to_airtable.py

What it does, in order — stopping at the first failure:

    1. AUTH    opens a browser once, saves token.json, never asks again
    2. CHECK   proves the token holds gmail.readonly and that Airtable answers
    3. SCHEMA  creates/verifies the Airtable table + fields (only ever adds)
    4. SCRAPE  searches the keywords, dedups by thread, shows what it found
    5. WRITE   upserts on Thread ID, uploads attachments as real files

One row per email *thread*, not per message — see UNIQUENESS below.

Before the first run you need `client_secret.json` next to this file:
    console.cloud.google.com -> enable "Gmail API"
    -> OAuth consent screen -> add your address under Test users
    -> Credentials -> Create credentials -> OAuth client ID -> Desktop app
    -> Download JSON, save as client_secret.json

And an Airtable personal access token with data.records:read/write and
schema.bases:read/write on the base:  export AIRTABLE_PAT=pat...


UNIQUENESS
----------
The Gmail thread id is the key. It's stable — replies land in the same thread
rather than creating a new one — so:
  * within a run, a thread matching both "proposal" and "cost sheet" is
    collected ONCE and lists both keywords
  * across runs, the Airtable write is an upsert on Thread ID, so re-running
    refreshes the row (new Last Email Date, higher Message Count) instead of
    adding a duplicate
Both are printed, so you can see it rather than trust it.
"""
import argparse
import base64
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses

import requests

# ═══════════════════════════════════════════════════════════════════════════
#  CONFIG — edit these
# ═══════════════════════════════════════════════════════════════════════════

KEYWORDS = ["proposal", "cost sheet"]

# Plain names are matched as exact phrases anywhere in the mailbox:
#     "cost sheet" in:anywhere
# Full Gmail syntax also works and is passed through untouched, e.g.
#     "from:sales@dmc.com has:attachment"

AIRTABLE_BASE_ID = os.environ.get("GMAIL_AIRTABLE_BASE_ID", "appNjXRYNAQ2Nuiah")
AIRTABLE_TABLE = os.environ.get("GMAIL_TABLE_NAME", "tblyXn5UDBAxTbWYZ")
AIRTABLE_PAT = os.environ.get("AIRTABLE_PAT", "")

DEFAULT_LIMIT = 5           # threads per run; --limit overrides
DEFAULT_LOOKBACK_DAYS = 180  # --since overrides
DEFAULT_SCOPE = "in:anywhere"  # includes Spam/Trash; use "" for normal mail only

ATTACHMENT_FIELD = "Files"
MAX_ATTACHMENT_MB = 5        # Airtable's own per-file upload limit
MAX_ATTACHMENTS_PER_THREAD = 10

CLIENT_SECRET_FILE = "client_secret.json"
TOKEN_FILE = "token.json"

# ═══════════════════════════════════════════════════════════════════════════

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
IST = timezone(timedelta(hours=5, minutes=30))
KEY_FIELD = "Thread ID"
META = "https://api.airtable.com/v0/meta/bases"
TRANSIENT = (429, 500, 502, 503)

_service = None
_manual_auth = False   # set from --manual; someone else's browser will grant consent


def rule(title):
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def die(message, hint=""):
    print(f"\n✗ {message}")
    if hint:
        print(f"  → {hint}")
    sys.exit(1)


# ─── 1. AUTH ────────────────────────────────────────────────────────────────

def gmail_service():
    """Authenticate once via browser, then reuse token.json forever."""
    global _service
    if _service:
        return _service

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:                       # noqa: BLE001
            print(f"  stored token could not refresh ({exc}); re-authorising")
            creds = None

    if not creds or not creds.valid:
        if not os.path.exists(CLIENT_SECRET_FILE):
            die(f"{CLIENT_SECRET_FILE} not found.",
                "Google Cloud Console -> Credentials -> OAuth client ID -> "
                "Desktop app -> download the JSON and save it here.")
        from google_auth_oauthlib.flow import InstalledAppFlow
        flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)

        if _manual_auth:
            # run_local_server() needs the browser that clicks "Allow" to be
            # able to reach back to a server on THIS machine's localhost. That
            # only works if you are the one clicking Allow. If someone else
            # (a different mailbox owner, on a different machine) has to grant
            # consent, their browser's "localhost" is THEIR machine, not this
            # one — the callback would just fail to connect. So instead: print
            # a URL, they open it and click Allow on their own machine, their
            # browser then tries (and fails) to load localhost — that failure
            # is expected — and they copy the full URL from their address bar
            # back to whoever is running this script.
            # Port 8080, not a low port: Chrome refuses to navigate to ports on
            # its blocked list (1, 7, 21, 22, 25 ...) with ERR_UNSAFE_PORT, and
            # would never reach the address bar we need them to copy from.
            # Google allows any loopback port for "Desktop app" clients, so no
            # redirect URI needs registering.
            flow.redirect_uri = "http://localhost:8080/"
            auth_url, _ = flow.authorization_url(
                access_type="offline", prompt="consent",
                include_granted_scopes="true")

            # These URLs run to ~900 characters. Copying one out of a wrapped
            # terminal reliably corrupts it — selection picks up the wrap
            # points — and Google answers a mangled query string with a bare
            # "400. Malformed request" that looks like an auth failure but
            # isn't. Writing to a file sidesteps the terminal entirely.
            link_file = "AUTH_LINK.txt"
            with open(link_file, "w", encoding="utf-8") as fh:
                fh.write(auth_url + "\n")

            print(f"\n  Wrote the authorisation link to:  {link_file}")
            print("  Open that file, copy the ONE line inside it, and send it "
                  "to the mailbox owner.\n")
            print("  Tell them, in this order:")
            print("    1. Open the link, sign in, click ALLOW.")
            print("    2. The next page fails: 'localhost refused to connect'.")
            print("       That failure IS the success — do not go back.")
            print("    3. Copy the url from the address bar of THAT error page.")
            print("       It starts with  http://localhost:8080/?...code=4/...\n")
            print("  The link is single-use. If they reload it, go back, or")
            print("  send you an accounts.google.com url, it is spent — Google")
            print("  answers '400. Malformed request'. Just re-run this to get")
            print("  a fresh one; nothing is broken.\n")

            response = input("  Paste that redirect URL here: ").strip()
            if "accounts.google.com" in response and "code=" not in response:
                die("That's the Google consent page, not the redirect.",
                    "They need to click ALLOW first, let the next page fail to "
                    "load, and copy the localhost:8080 url from there. Re-run "
                    "with --manual for a fresh link — the one they used is "
                    "now spent.")
            if "code=" not in response:
                die("That URL has no ?code= in it, so it isn't the redirect.",
                    "The url you want starts with http://localhost:8080/ and "
                    "contains code=4/...")
            flow.fetch_token(authorization_response=response)
            creds = flow.credentials
            if os.path.exists(link_file):
                os.remove(link_file)
        else:
            print("  opening a browser so you can grant read-only access...")
            creds = flow.run_local_server(port=0, access_type="offline",
                                          prompt="consent")

        with open(TOKEN_FILE, "w", encoding="utf-8") as fh:
            fh.write(creds.to_json())
        print(f"  saved {TOKEN_FILE} — you won't be asked again")

    from googleapiclient.discovery import build
    _service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    _service._creds = creds
    return _service


def api(request):
    """Execute a Gmail request, backing off on rate limits and 5xx."""
    from googleapiclient.errors import HttpError
    for attempt in range(5):
        try:
            return request.execute()
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            if status in (403, 429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(2 ** attempt)
                continue
            raise
    raise RuntimeError("Gmail request failed after 5 attempts")


# ─── 2. CHECK ───────────────────────────────────────────────────────────────

def step_check():
    rule("STEP 1/4  Auth and scope")

    if not AIRTABLE_PAT:
        die("AIRTABLE_PAT is not set.",
            "export AIRTABLE_PAT=patXXXXXXXX   (needs data.records:read/write "
            "and schema.bases:read/write on the base)")

    svc = gmail_service()
    mailbox = api(svc.users().getProfile(userId="me")).get("emailAddress", "")
    print(f"  ✓ Gmail auth — authenticated as {mailbox}")

    # Ask Google what the token actually holds. Requesting a scope and holding
    # one are different things: a consent screen with a box unticked
    # authenticates fine, then 403s on the first real call.
    r = requests.get("https://oauth2.googleapis.com/tokeninfo",
                     params={"access_token": svc._creds.token}, timeout=30)
    if r.status_code < 400:
        granted = (r.json().get("scope") or "").split()
        if SCOPES[0] in granted or "https://mail.google.com/" in granted:
            print("  ✓ Granted scopes — gmail.readonly held")
        else:
            die("gmail.readonly was NOT granted. Token holds: "
                + (", ".join(s.rsplit('/', 1)[-1] for s in granted) or "nothing"),
                f"delete {TOKEN_FILE}, revoke at "
                "https://myaccount.google.com/permissions, and re-run")
    else:
        print("  ! could not read tokeninfo — relying on the live calls below")

    r = requests.get(f"{META}/{AIRTABLE_BASE_ID}/tables",
                     headers={"Authorization": f"Bearer {AIRTABLE_PAT}"},
                     timeout=30)
    if r.status_code == 401:
        die("Airtable rejected the token (401).", "the PAT is invalid or revoked")
    if r.status_code == 403:
        die(f"The PAT cannot see base {AIRTABLE_BASE_ID} (403).",
            "add this base to the token's scope in your Airtable account")
    if r.status_code >= 400:
        die(f"Airtable {r.status_code}: {r.text[:200]}")
    tables = r.json().get("tables", [])
    print(f"  ✓ Airtable — base readable, {len(tables)} table(s)")
    return mailbox, tables


# ─── 3. SCHEMA ──────────────────────────────────────────────────────────────

T, ML, N = "singleLineText", "multilineText", "number"
DATETIME = {"type": "dateTime", "options": {
    "dateFormat": {"name": "iso"}, "timeFormat": {"name": "24hour"},
    "timeZone": "Asia/Kolkata"}}

FIELDS = [
    {"name": KEY_FIELD, "type": T},          # primary field = the upsert key
    {"name": "First Message ID", "type": T},
    {"name": "Category", "type": T},
    {"name": "All Categories", "type": T},
    {"name": "Subject", "type": T},
    {"name": "Sender Email", "type": "email"},
    {"name": "Sender Name", "type": T},
    {"name": "To Emails", "type": ML},
    {"name": "CC Emails", "type": ML},
    {"name": "First Email Date", **DATETIME},
    {"name": "Last Email Date", **DATETIME},
    {"name": "Attachments", "type": ML},
    {"name": ATTACHMENT_FIELD, "type": "multipleAttachments"},
    {"name": "Attachment Count", "type": N, "options": {"precision": 0}},
    {"name": "Message Count", "type": N, "options": {"precision": 0}},
    {"name": "Snippet", "type": ML},
    {"name": "Gmail Link", "type": "url"},
    {"name": "Mailbox", "type": T},
    {"name": "Synced At", **DATETIME},
]


def step_schema(tables):
    """Add whatever is missing. Never modifies or deletes anything existing."""
    rule("STEP 2/4  Airtable schema")
    headers = {"Authorization": f"Bearer {AIRTABLE_PAT}",
               "Content-Type": "application/json"}

    table = next((t for t in tables
                  if AIRTABLE_TABLE in (t["id"], t["name"])), None)

    if not table:
        if AIRTABLE_TABLE.startswith("tbl"):
            die(f"Table {AIRTABLE_TABLE} not found in base {AIRTABLE_BASE_ID}.",
                "check AIRTABLE_TABLE at the top of this file")
        r = requests.post(f"{META}/{AIRTABLE_BASE_ID}/tables", headers=headers,
                          json={"name": AIRTABLE_TABLE, "fields": FIELDS},
                          timeout=30)
        if r.status_code not in (200, 201):
            die(f"Could not create the table: {r.status_code} {r.text[:200]}")
        print(f"  + created table {AIRTABLE_TABLE}")
        return

    print(f"  table '{table['name']}' ({table['id']})")
    existing = {f["name"] for f in table.get("fields", [])}
    added = 0
    for field in FIELDS:
        if field["name"] in existing:
            continue
        time.sleep(0.3)
        r = requests.post(f"{META}/{AIRTABLE_BASE_ID}/tables/{table['id']}/fields",
                          headers=headers, json=field, timeout=30)
        if r.status_code in (200, 201):
            print(f"    + {field['name']}")
            added += 1
        elif r.status_code == 422:
            pass                                   # already there under another id
        else:
            print(f"    ! {field['name']}: {r.status_code} {r.text[:120]}")
    print(f"  ✓ schema ready ({added} field(s) added, "
          f"{len(existing)} already present)")


# ─── 4. SCRAPE ──────────────────────────────────────────────────────────────

_PREFIX = re.compile(r"^\s*(?:(?:re|fwd?|aw)\s*:\s*)+", re.IGNORECASE)


def build_query(term):
    """A bare name becomes an exact phrase; real Gmail syntax passes through."""
    term = term.strip()
    ops = ("from:", "to:", "cc:", "subject:", "label:", "has:", "in:",
           "filename:", "after:", "before:", "newer_than:", "is:")
    if any(o in term.lower() for o in ops) or " or " in term.lower() \
            or term.startswith(("(", '"', "-")):
        return term
    return f'"{term}" {DEFAULT_SCOPE}'.strip()


def since_clause(since):
    since = (since or "").strip().lower()
    if since in ("all", "any"):
        return ""
    if not since:
        return f"newer_than:{DEFAULT_LOOKBACK_DAYS}d"
    if since[-1] in "dmy" and since[:-1].isdigit():
        return f"newer_than:{since}"
    if since.isdigit():
        return f"newer_than:{since}d"
    day = datetime.strptime(since, "%Y-%m-%d").date()
    return f"after:{day:%Y/%m/%d}"


def head(message, name):
    wanted = name.lower()
    for h in message.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == wanted:
            return h.get("value", "") or ""
    return ""


def emails(value):
    out = []
    for _, addr in getaddresses([value or ""]):
        addr = addr.strip().lower()
        if "@" in addr and addr not in out:
            out.append(addr)
    return out


def attachments_in(payload):
    """Every downloadable attachment part, flattened out of the MIME tree."""
    found = []

    def walk(part):
        name = (part.get("filename") or "").strip()
        body = part.get("body", {}) or {}
        if name and body.get("attachmentId"):
            found.append({"filename": name,
                          "attachment_id": body["attachmentId"],
                          "mime_type": part.get("mimeType") or "application/octet-stream",
                          "size": int(body.get("size", 0) or 0)})
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload or {})
    return found


def thread_row(thread, categories, mailbox):
    """Flatten one Gmail thread into one Airtable row + its attachment refs.

    Subject and sender come from the FIRST message (who started it); CC is the
    union across every message, because someone looped in on reply 4 is still a
    participant; dates come from internalDate, which is Gmail's own receive
    timestamp and immune to a malformed Date: header.
    """
    messages = sorted(thread.get("messages", []),
                      key=lambda m: int(m.get("internalDate", 0)))
    if not messages:
        return None, []
    first, last = messages[0], messages[-1]

    cc, to, names, refs = [], [], [], []
    for msg in messages:
        for a in emails(head(msg, "Cc")):
            if a not in cc:
                cc.append(a)
        for a in emails(head(msg, "To")):
            if a not in to:
                to.append(a)
        for att in attachments_in(msg.get("payload", {})):
            if att["filename"] in names:
                continue
            names.append(att["filename"])
            refs.append({**att, "message_id": msg.get("id", "")})

    def ist(ms):
        return datetime.fromtimestamp(int(ms) / 1000, tz=IST).isoformat(
            timespec="seconds")

    sender = emails(head(first, "From"))
    parsed = getaddresses([head(first, "From")])
    tid = thread.get("id", "")

    return {
        KEY_FIELD: tid,
        "First Message ID": head(first, "Message-ID"),
        "Category": categories[0],
        "All Categories": ", ".join(categories),
        "Subject": _PREFIX.sub("", head(first, "Subject")).strip() or "(no subject)",
        "Sender Email": sender[0] if sender else "",
        "Sender Name": (parsed[0][0].strip('" ') if parsed else "") or
                       (sender[0].split("@")[0] if sender else ""),
        "To Emails": ", ".join(to),
        "CC Emails": ", ".join(cc),
        "First Email Date": ist(first["internalDate"]),
        "Last Email Date": ist(last["internalDate"]),
        "Attachments": ", ".join(names),
        "Attachment Count": len(names),
        "Message Count": len(messages),
        "Snippet": (first.get("snippet") or "").strip(),
        "Gmail Link": f"https://mail.google.com/mail/u/0/#all/{tid}",
        "Mailbox": mailbox,
        "Synced At": datetime.now(IST).isoformat(timespec="seconds"),
    }, refs


def step_scrape(keywords, since, limit, mailbox):
    rule("STEP 3/4  Searching Gmail")
    svc = gmail_service()
    date_clause = since_clause(since)

    matched, order, raw_hits = {}, [], 0
    for keyword in keywords:
        query = f"{build_query(keyword)} {date_clause}".strip()
        ids, token = [], None
        while len(ids) < 200:
            resp = api(svc.users().threads().list(
                userId="me", q=query, maxResults=100, pageToken=token))
            ids.extend(t["id"] for t in resp.get("threads", []))
            token = resp.get("nextPageToken")
            if not token:
                break
        raw_hits += len(ids)
        print(f"  {keyword:<18} {query!r} -> {len(ids)} thread(s)")
        for tid in ids:
            if tid not in matched:
                matched[tid] = []
                order.append(tid)
            label = keyword.title()
            if label not in matched[tid]:
                matched[tid].append(label)

    print(f"\n  Uniqueness")
    print(f"    search hits (before dedup) : {raw_hits}")
    print(f"    unique threads             : {len(order)}")
    if raw_hits > len(order):
        print(f"    -> {raw_hits - len(order)} hit(s) were the same thread "
              "matching more than one keyword")

    if limit and len(order) > limit:
        print(f"    taking the first {limit} (use --limit to change)")
        order = order[:limit]

    rows = []
    for n, tid in enumerate(order, 1):
        thread = api(svc.users().threads().get(userId="me", id=tid, format="full"))
        row, refs = thread_row(thread, matched[tid], mailbox)
        if row:
            rows.append((row, refs))
        if n % 10 == 0 or n == len(order):
            print(f"    fetched {n}/{len(order)}")
    return rows


def preview(rows):
    if not rows:
        print("\n  (nothing matched — try --since all, or other keywords)")
        return
    print(f"\n  {'CATEGORY':<14} {'FIRST':<11} {'LAST':<11} {'FROM':<26} "
          f"{'ATT':>3}  SUBJECT")
    print(f"  {'-' * 14} {'-' * 11} {'-' * 11} {'-' * 26} {'-' * 3}  {'-' * 30}")
    for row, _ in rows:
        print(f"  {row['Category'][:14]:<14} {row['First Email Date'][:10]:<11} "
              f"{row['Last Email Date'][:10]:<11} {row['Sender Email'][:26]:<26} "
              f"{row['Attachment Count']:>3}  {row['Subject'][:30]}")
    print(f"\n  {len(rows)} thread(s), "
          f"{sum(r['Attachment Count'] for r, _ in rows)} attachment(s)")


# ─── 5. WRITE ───────────────────────────────────────────────────────────────

def airtable(method, url, payload=None):
    headers = {"Authorization": f"Bearer {AIRTABLE_PAT}",
               "Content-Type": "application/json"}
    for attempt in range(5):
        r = requests.request(method, url, headers=headers, json=payload,
                             timeout=120)
        if r.status_code in TRANSIENT and attempt < 4:
            time.sleep(2 ** attempt)
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"Airtable {r.status_code}: {r.text[:400]}")
        return r.json()
    raise RuntimeError("Airtable request failed after 5 attempts")


def step_write(rows, upload_files):
    rule("STEP 4/4  Writing to Airtable")
    url = (f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/"
           f"{requests.utils.quote(AIRTABLE_TABLE)}")

    created = updated = 0
    by_key = {}
    for i in range(0, len(rows), 10):
        chunk = rows[i:i + 10]
        resp = airtable("PATCH", url, {
            "performUpsert": {"fieldsToMergeOn": [KEY_FIELD]},
            "records": [{"fields": {k: v for k, v in row.items()
                                    if v is not None and v != ""}}
                        for row, _ in chunk],
            "typecast": True,
        })
        created += len(resp.get("createdRecords", []))
        updated += len(resp.get("updatedRecords", []))
        for record in resp.get("records", []):
            key = record.get("fields", {}).get(KEY_FIELD)
            if key:
                by_key[key] = record

    print(f"  created {created} new row(s), updated {updated} existing row(s)")
    if updated:
        print("  (updates = threads already scraped before, matched on Thread ID"
              " — no duplicates were added)")

    if not upload_files:
        print("  attachments skipped (--no-attachments)")
        return

    limit_bytes = int(MAX_ATTACHMENT_MB * 1024 * 1024)
    svc = gmail_service()
    uploaded, skipped = 0, 0

    for row, refs in rows:
        record = by_key.get(row[KEY_FIELD])
        if not record or not refs:
            continue
        already = {c.get("filename") for c in
                   (record.get("fields", {}).get(ATTACHMENT_FIELD) or [])
                   if isinstance(c, dict)}
        for ref in refs[:MAX_ATTACHMENTS_PER_THREAD]:
            if ref["filename"] in already:
                continue
            if ref["size"] > limit_bytes:
                print(f"    ! {ref['filename']} is {ref['size'] / 1e6:.1f} MB — "
                      f"over Airtable's {MAX_ATTACHMENT_MB} MB upload limit, "
                      "name recorded only")
                skipped += 1
                continue
            try:
                blob = api(svc.users().messages().attachments().get(
                    userId="me", messageId=ref["message_id"], id=ref["attachment_id"]))
                content = base64.urlsafe_b64decode(blob.get("data", ""))
                airtable("POST",
                         f"https://content.airtable.com/v0/{AIRTABLE_BASE_ID}/"
                         f"{record['id']}/{requests.utils.quote(ATTACHMENT_FIELD)}"
                         "/uploadAttachment",
                         {"contentType": ref["mime_type"],
                          "file": base64.b64encode(content).decode("ascii"),
                          "filename": ref["filename"]})
                uploaded += 1
                print(f"    + {ref['filename']} ({len(content) / 1024:.0f} KB)")
            except Exception as exc:                   # noqa: BLE001
                print(f"    ! {ref['filename']}: {str(exc)[:160]}")
                skipped += 1

    print(f"  {uploaded} file(s) uploaded to '{ATTACHMENT_FIELD}'"
          + (f", {skipped} skipped" if skipped else ""))
    print(f"\nDone. https://airtable.com/{AIRTABLE_BASE_ID}")


# ─── main ───────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Scrape Gmail threads into Airtable.")
    ap.add_argument("--keywords", default=",".join(KEYWORDS),
                    help=f"Comma-separated. Default: {', '.join(KEYWORDS)}")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help=f"Max threads. Default: {DEFAULT_LIMIT}. 0 = no limit.")
    ap.add_argument("--since", default="",
                    help="30d / 6m / 1y / YYYY-MM-DD / all. "
                         f"Default: last {DEFAULT_LOOKBACK_DAYS} days.")
    ap.add_argument("--dry-run", action="store_true", help="Never write.")
    ap.add_argument("-y", "--yes", action="store_true", help="Don't ask.")
    ap.add_argument("--no-attachments", action="store_true",
                    help="Record filenames but don't upload the files.")
    ap.add_argument("--check-only", action="store_true", help="Stop after step 1.")
    ap.add_argument("--manual", action="store_true",
                    help="Headless OAuth: print a URL for someone ELSE to open "
                         "and approve on their own machine (e.g. authorising a "
                         "mailbox that isn't yours), then paste back the "
                         "redirect URL they send you. Use this whenever the "
                         "person granting consent isn't the person running "
                         "this script.")
    args = ap.parse_args()

    global _manual_auth
    _manual_auth = args.manual

    try:
        from dotenv import load_dotenv       # optional convenience
        load_dotenv()
        globals()["AIRTABLE_PAT"] = os.environ.get("AIRTABLE_PAT", AIRTABLE_PAT)
    except ImportError:
        pass

    mailbox, tables = step_check()
    if args.check_only:
        print("\n✓ Everything checks out. Drop --check-only for a real run.")
        return 0

    step_schema(tables)

    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    rows = step_scrape(keywords, args.since, args.limit, mailbox)
    preview(rows)

    if not rows:
        return 0
    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0
    if not args.yes:
        if input(f"\nWrite {len(rows)} thread(s) to Airtable? [y/N] "
                 ).strip().lower() not in ("y", "yes"):
            print("Cancelled — nothing written.")
            return 0

    step_write(rows, upload_files=not args.no_attachments)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
