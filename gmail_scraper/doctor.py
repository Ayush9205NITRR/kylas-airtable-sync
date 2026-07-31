"""
Pre-flight check: is everything wired up correctly?

Runs read-only checks against Gmail and Airtable and prints a pass/fail
summary. Writes nothing, uploads nothing, changes nothing — safe to run any
time, and the fastest way to find out which secret is wrong.

    python -m gmail_scraper.doctor

Exit code 0 = every check passed, 1 = something needs fixing.
"""
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gmail_scraper import config

META = "https://api.airtable.com/v0/meta/bases"

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
_results = []


def report(status: str, check: str, detail: str = ""):
    icon = {PASS: "✓", FAIL: "✗", WARN: "!"}[status]
    print(f"  {icon} {check}" + (f" — {detail}" if detail else ""))
    _results.append((status, check))


def mask(value: str) -> str:
    if not value:
        return "(not set)"
    return f"{value[:4]}…{value[-4:]} ({len(value)} chars)" if len(value) > 12 else "(set)"


# ── 1. Secrets present ─────────────────────────────────────────────────────────

def check_env():
    print("\n[1/4] Credentials present")
    ok = True

    if config.AIRTABLE_TOKEN:
        report(PASS, "AIRTABLE_PAT", mask(config.AIRTABLE_TOKEN))
    else:
        report(FAIL, "AIRTABLE_PAT", "not set")
        ok = False

    oauth = [config.GMAIL_CLIENT_ID, config.GMAIL_CLIENT_SECRET,
             config.GMAIL_REFRESH_TOKEN]
    if all(oauth):
        report(PASS, "Gmail OAuth", "client id + secret + refresh token set")
    elif any(oauth):
        missing = [n for n, v in (("GMAIL_CLIENT_ID", oauth[0]),
                                  ("GMAIL_CLIENT_SECRET", oauth[1]),
                                  ("GMAIL_REFRESH_TOKEN", oauth[2])) if not v]
        report(FAIL, "Gmail OAuth", f"incomplete — missing {', '.join(missing)}")
        ok = False
    elif config.GOOGLE_SERVICE_ACCOUNT_JSON and config.GMAIL_USER:
        report(PASS, "Gmail service account",
               f"impersonating {config.GMAIL_USER} (needs domain-wide delegation)")
    else:
        report(FAIL, "Gmail credentials",
               "set GMAIL_CLIENT_ID/SECRET/REFRESH_TOKEN — run "
               "`python -m gmail_scraper.auth_setup` to mint them")
        ok = False

    print(f"      base={config.AIRTABLE_BASE_ID}  table={config.TABLE_NAME}  "
          f"terms={config.SEARCH_TERMS_TABLE}")
    return ok


# ── 2. Airtable ────────────────────────────────────────────────────────────────

def check_airtable():
    print("\n[2/4] Airtable")
    if not config.AIRTABLE_TOKEN:
        report(FAIL, "Airtable", "no token — skipped")
        return False

    headers = {"Authorization": f"Bearer {config.AIRTABLE_TOKEN}"}
    try:
        r = requests.get(f"{META}/{config.AIRTABLE_BASE_ID}/tables",
                         headers=headers, timeout=30)
    except requests.RequestException as exc:
        report(FAIL, "Airtable reachable", str(exc))
        return False

    if r.status_code == 401:
        report(FAIL, "Airtable token", "401 — the PAT is invalid or revoked")
        return False
    if r.status_code == 403:
        report(FAIL, "Airtable access",
               f"403 — the PAT cannot see base {config.AIRTABLE_BASE_ID}. Add the "
               "base to the token's scope, and include schema.bases:read.")
        return False
    if r.status_code >= 400:
        report(FAIL, "Airtable schema", f"{r.status_code}: {r.text[:200]}")
        return False

    tables = r.json().get("tables", [])
    report(PASS, "Airtable base readable",
           f"{len(tables)} table(s): " + ", ".join(t["name"] for t in tables[:6]))

    ok = True
    thread_table = next((t for t in tables
                         if config.TABLE_NAME in (t["id"], t["name"])), None)
    if thread_table:
        fields = {f["name"]: f for f in thread_table.get("fields", [])}
        report(PASS, "Thread table",
               f"'{thread_table['name']}' ({thread_table['id']}), "
               f"{len(fields)} field(s)")

        required = [config.KEY_FIELD, "Category", "Subject", "Sender Email",
                    "CC Emails", "First Email Date", "Last Email Date",
                    "Attachments"]
        missing = [f for f in required if f not in fields]
        if missing:
            report(FAIL, "Thread table fields",
                   f"missing {', '.join(missing)} — run "
                   "scripts/setup_gmail_airtable.py")
            ok = False
        else:
            report(PASS, "Thread table fields", "all scraped columns present")

        attach = fields.get(config.ATTACHMENT_FIELD)
        if not attach:
            report(FAIL, f"'{config.ATTACHMENT_FIELD}' field",
                   "missing — attachments cannot be uploaded. Run "
                   "scripts/setup_gmail_airtable.py")
            ok = False
        elif attach.get("type") != "multipleAttachments":
            report(FAIL, f"'{config.ATTACHMENT_FIELD}' field",
                   f"is type {attach.get('type')}, needs to be Attachment")
            ok = False
        else:
            report(PASS, f"'{config.ATTACHMENT_FIELD}' field",
                   "attachment field ready for downloads")
    else:
        report(FAIL, "Thread table",
               f"{config.TABLE_NAME!r} not found in the base — check "
               "GMAIL_TABLE_NAME, or run scripts/setup_gmail_airtable.py")
        ok = False

    if any(config.SEARCH_TERMS_TABLE in (t["id"], t["name"]) for t in tables):
        report(PASS, "Search Terms table", "present")
    else:
        report(WARN, "Search Terms table",
               f"{config.SEARCH_TERMS_TABLE!r} not found — the scraper will fall "
               f"back to {os.path.basename(config.CATEGORIES_FILE)}")
    return ok


# ── 3. Search terms ────────────────────────────────────────────────────────────

def check_terms():
    print("\n[3/4] Search terms")
    from gmail_scraper import search_terms
    try:
        terms = search_terms.load("auto")
    except Exception as exc:                          # noqa: BLE001
        report(FAIL, "Search terms", str(exc))
        return []
    if not terms:
        report(FAIL, "Search terms", "none active anywhere")
        return []
    report(PASS, "Search terms", f"{len(terms)} active")
    for t in terms[:10]:
        print(f"      {t['name']:<24} -> {t['query']}")
    if len(terms) > 10:
        print(f"      ... and {len(terms) - 10} more")
    return terms


# ── 4. Gmail ───────────────────────────────────────────────────────────────────

def check_gmail(terms):
    print("\n[4/4] Gmail — auth, scope, and the three calls the scraper makes")
    from gmail_scraper import gmail_client, parse

    # -- does the token authenticate at all --------------------------------
    try:
        mailbox = gmail_client.whoami()
    except Exception as exc:                          # noqa: BLE001
        detail = str(exc)
        if "invalid_grant" in detail:
            detail += ("  → the refresh token is stale or was revoked; re-run "
                       "`python -m gmail_scraper.auth_setup`")
        elif "insufficient" in detail.lower() or "403" in detail:
            detail += ("  → the credentials lack the gmail.readonly scope, or "
                       "the Gmail API is not enabled on the project")
        report(FAIL, "Gmail auth", detail[:400])
        return False
    report(PASS, "Gmail auth", f"authenticated as {mailbox}")

    # -- does it hold the scope, per Google, not per our request -----------
    needed = "https://www.googleapis.com/auth/gmail.readonly"
    try:
        scopes = gmail_client.granted_scopes()
    except Exception as exc:                          # noqa: BLE001
        report(WARN, "Granted scopes",
               f"could not read tokeninfo ({str(exc)[:120]}) — relying on the "
               "live calls below instead")
    else:
        broader = [s for s in scopes if s in (
            "https://mail.google.com/",
            "https://www.googleapis.com/auth/gmail.modify")]
        if needed in scopes:
            report(PASS, "Granted scopes", "gmail.readonly held")
        elif broader:
            report(PASS, "Granted scopes",
                   f"{broader[0]} held (covers read access)")
        else:
            report(FAIL, "Granted scopes",
                   "gmail.readonly NOT granted — token has: "
                   + (", ".join(s.rsplit("/", 1)[-1] for s in scopes) or "none")
                   + ". Revoke at https://myaccount.google.com/permissions and "
                     "re-run `python -m gmail_scraper.auth_setup`.")
            return False

    if config.GMAIL_USER and mailbox.lower() != config.GMAIL_USER.lower():
        report(WARN, "Mailbox",
               f"GMAIL_USER is {config.GMAIL_USER} but the token resolves to "
               f"{mailbox} — {mailbox} is what gets scraped")

    # -- exercise each call the scraper actually depends on -----------------
    ok = True
    date_clause = config.since_clause("")
    probe_thread_id = None

    for term in terms[:5]:
        query = f"{term['query']} {date_clause}".strip()
        try:
            found = gmail_client.search_thread_ids(query, max_threads=5)
        except Exception as exc:                      # noqa: BLE001
            report(FAIL, f"threads.list '{term['name']}'", str(exc)[:200])
            ok = False
            continue
        if found:
            report(PASS, f"Search '{term['name']}'",
                   f"{len(found)}+ thread(s) match")
            probe_thread_id = probe_thread_id or found[0]
        else:
            report(WARN, f"Search '{term['name']}'",
                   f"0 threads in the last {config.DEFAULT_LOOKBACK_DAYS} days — "
                   "widen with --since all if that's unexpected")

    if not probe_thread_id:
        report(WARN, "threads.get / attachments.get",
               "no matching thread to probe with — searches returned nothing")
        return ok

    try:
        thread = gmail_client.get_thread(probe_thread_id)
    except Exception as exc:                          # noqa: BLE001
        report(FAIL, "threads.get (read a full message)", str(exc)[:200])
        return False
    report(PASS, "threads.get (read a full message)",
           f"{len(thread.get('messages', []))} message(s) in the probe thread")

    refs = parse.attachment_refs(thread)
    if not refs:
        report(WARN, "attachments.get (download a file)",
               "the probe thread has no attachments — untested. Re-run once a "
               "matching mail has one, or trust the scope check above.")
        return ok

    try:
        data = gmail_client.download_attachment(refs[0]["message_id"],
                                                refs[0]["attachment_id"])
    except Exception as exc:                          # noqa: BLE001
        report(FAIL, "attachments.get (download a file)", str(exc)[:200])
        return False
    report(PASS, "attachments.get (download a file)",
           f"{refs[0]['filename']} — {len(data) / 1024:.0f} KB downloaded "
           "(discarded, this is a probe)")
    return ok


def main() -> int:
    from dotenv import load_dotenv
    load_dotenv()

    print("=== Gmail scraper pre-flight check (read-only) ===")
    env_ok = check_env()
    air_ok = check_airtable()
    # Still worth listing terms when Airtable is down — it shows the JSON fallback.
    terms = check_terms()
    gmail_ok = check_gmail(terms) if env_ok and terms else False

    failures = [c for s, c in _results if s == FAIL]
    warnings = [c for s, c in _results if s == WARN]

    print("\n=== Summary ===")
    print(f"  {len(_results) - len(failures) - len(warnings)} passed, "
          f"{len(warnings)} warning(s), {len(failures)} failed")
    if failures:
        print("  Fix: " + "; ".join(failures))
        print("\nNot ready yet.")
        return 1
    if not (env_ok and air_ok and gmail_ok):
        print("\nNot ready yet.")
        return 1
    print("\nAll good — try a real run with:")
    print("  python -m gmail_scraper.pipeline --limit 5 --dry-run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
