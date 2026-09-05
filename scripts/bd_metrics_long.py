#!/usr/bin/env python3
"""
BD Metrics Daily — the same numbers, reshaped so metric NAMES can go on an axis.

Airtable (and every other charting tool) plots the VALUES of one column on the
X-axis. The funnel tables keep each metric in its own column, so those can only
ever be six separate Y-series — there is no way to get "Call Attempted, Call
Connected, Meeting Booked, …" along the X-axis from that shape.

This writes the same data in LONG form: one row per associate per day per
metric, with the metric name in a `Metric` column and the number in `Value`.
Chart it as X = Metric, Y = Sum of Value, and filter by BD Associate / Week /
Month. One chart definition then serves the team view and every individual.

Two metric families live here, tagged by `Metric Group` so a chart can show
either without mixing grains:

  Contact  — Call Attempted, Call Connected, Meeting Booked, Meeting Done,
             SQL, MQL. Counted in CONTACTS. Definitions are imported from
             bd_monthly_matrix.py rather than restated, so the daily view and
             the monthly matrix can never drift apart.
  Company  — Companies Worked, Companies Reached, Requirements Stated, Handoff
             Calls Held, SQLs Accepted, SQLs Rejected. Counted in COMPANIES,
             each at its best stage that day. Imported from bd_company_funnel.py.

Never put both groups in one chart: one counts people, the other accounts.

Day attribution is cfLastCalledAt, matching both source scripts. Closed days are
frozen on first write — see bd_company_funnel._is_closed for why re-deriving
history silently shrinks it.

    python scripts/bd_metrics_long.py              # build + push
    python scripts/bd_metrics_long.py --dry-run    # print a summary only
"""
import argparse
import importlib.util
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.kylas_client import KylasClient                      # noqa: E402
from utils.account_pipeline import load_order, _company_id       # noqa: E402
from utils import stage_history                                  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name: str):
    """Import a sibling script by path — they start with digits/underscores and
    are not importable as normal modules."""
    spec = importlib.util.spec_from_file_location(
        name.replace(".py", ""), os.path.join(_HERE, name))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


matrix = _load("bd_monthly_matrix.py")
funnel = _load("bd_company_funnel.py")

META       = "https://api.airtable.com/v0/meta/bases"
TABLE_NAME = "BD Metrics Daily"

CONTACT_METRICS = ["Call Attempted", "Call Connected", "Meeting Booked",
                   "Meeting Done", "SQL", "MQL"]
COMPANY_METRICS = funnel.COLUMNS

HISTORY_DAYS = funnel.DAILY_HISTORY_DAYS


# bd_monthly_matrix imports this inside its build function, so it is not a
# module attribute there — take it from the shared source both of them use.
from utils.bd_metrics import ATTEMPTED_EXCLUDE                   # noqa: E402

_ATTEMPTED_EXCLUDE_N = None    # built lazily: _norm lives on the matrix module


def _contact_counts(stage_n: str, order) -> dict:
    """The six contact-level metrics for ONE contact's normalised stage."""
    global _ATTEMPTED_EXCLUDE_N
    if _ATTEMPTED_EXCLUDE_N is None:
        _ATTEMPTED_EXCLUDE_N = {matrix._norm(s) for s in ATTEMPTED_EXCLUDE}
    attempted = bool(stage_n) and stage_n not in _ATTEMPTED_EXCLUDE_N
    return {
        "Call Attempted":  int(attempted),
        "Call Connected":  int(attempted and stage_n not in matrix._CNC_EXCLUDE_N),
        "Meeting Booked":  int(stage_n in matrix._MEETING_BOOKED_N),
        "Meeting Done":    int(stage_n in matrix._MEETING_DONE_N),
        "SQL":             int(stage_n == matrix._norm(funnel.SQL_STAGE)),
        "MQL":             int(stage_n in matrix._MQL_N),
    }


def build_long(kylas, all_owners: bool = False) -> tuple:
    """Return ({(rep, email, day, group, metric): value}, stats)."""
    from utils.bd_metrics import refresh_stage_map, contact_stage
    refresh_stage_map(kylas)

    order    = load_order()
    user_map = funnel._build_user_map(kylas)
    roster   = set() if all_owners else funnel.bd_roster()

    print("[long] Fetching contacts from Kylas...")
    contacts = kylas._search_all(
        "contact",
        fields=["id", "company", "ownedBy", "ownerId", "updatedAt",
                "customFieldValues"],
    )
    print(f"[long] {len(contacts)} contacts fetched")

    # Day attribution prefers the DETECTED stage-change date, falling back to
    # cfLastCalledAt while a contact has no detected change yet — matches
    # bd_company_funnel.py so the two never disagree about which day owns
    # a given contact. See utils/stage_history.effective_call_date().
    stage_snap = stage_history.load()

    # Contact metrics accumulate directly; company metrics need the per-day
    # best-rank pass first, since an account counts once at its best stage.
    contact_cells = defaultdict(lambda: defaultdict(int))   # (rep,email,day) -> metric -> n
    company_best  = defaultdict(dict)                       # (rep,email,day) -> cid -> rank
    skipped = 0

    for ct in contacts:
        cf = ct.get("customFieldValues") or {}
        day = stage_history.effective_call_date(
            stage_snap, ct.get("id"),
            fallback=funnel._parse_lc(cf.get("cfLastCalledAt", "")))
        if not day:
            skipped += 1
            continue
        stage = contact_stage(ct)
        if not stage:
            skipped += 1
            continue
        name, email = funnel._owner(ct, user_map)
        if roster and email.strip().lower() not in roster:
            skipped += 1
            continue

        key = (name, email, day)
        for metric, n in _contact_counts(matrix._norm(stage), order).items():
            if n:
                contact_cells[key][metric] += n

        cid = _company_id(ct)
        if cid:
            rank = order.rank_of(stage)
            if rank:
                cur = company_best[key].get(cid)
                if cur is None or rank < cur:
                    company_best[key][cid] = rank

    long_rows = {}
    for key, metrics in contact_cells.items():
        for metric in CONTACT_METRICS:
            long_rows[(*key, "Contact", metric)] = metrics.get(metric, 0)
    for key, companies in company_best.items():
        counts = funnel._counts(list(companies.values()), order)
        for metric in COMPANY_METRICS:
            long_rows[(*key, "Company", metric)] = counts[metric]

    stats = {"contacts": len(contacts), "skipped": skipped, "rows": len(long_rows)}
    print(f"[long] {skipped} contact(s) skipped (no call date, blank stage, "
          f"or off-roster) → {len(long_rows)} long row(s)")
    return long_rows, stats


def ensure_table(base_id: str, headers: dict) -> bool:
    r = requests.get(f"{META}/{base_id}/tables", headers=headers, timeout=30)
    r.raise_for_status()
    if any(t["name"] == TABLE_NAME for t in r.json().get("tables", [])):
        print(f"[long] Airtable table {TABLE_NAME!r} already exists")
        return True
    defn = {
        "name": TABLE_NAME,
        "description": ("Long/tidy form of the BD metrics: one row per "
                        "associate per day per metric. Chart X = Metric, "
                        "Y = Sum of Value. Filter Metric Group to 'Contact' or "
                        "'Company' — never chart both together, one counts "
                        "people and the other accounts. "
                        "Built by scripts/bd_metrics_long.py."),
        "fields": [
            {"name": "Key",          "type": "singleLineText"},
            {"name": "Date",         "type": "singleLineText"},
            {"name": "Week",         "type": "singleLineText"},
            {"name": "Month",        "type": "singleLineText"},
            {"name": "BD Email",     "type": "singleLineText"},
            {"name": "BD Associate", "type": "singleLineText"},
            {"name": "Metric Group", "type": "singleLineText"},
            {"name": "Metric",       "type": "singleLineText"},
            {"name": "Value",        "type": "number", "options": {"precision": 0}},
            {"name": "Updated At",   "type": "singleLineText"},
        ],
    }
    resp = requests.post(f"{META}/{base_id}/tables", json=defn, headers=headers, timeout=30)
    if resp.status_code in (200, 201):
        print(f"[long] Created Airtable table {TABLE_NAME!r}")
        return True
    print(f"[long] ERROR creating {TABLE_NAME!r}: {resp.status_code} {resp.text[:300]}")
    return False


def push(long_rows: dict) -> None:
    from utils.airtable_client import AirtableClient
    base_id = os.environ["AIRTABLE_BASE_ID"]
    headers = {"Authorization": f"Bearer {os.environ['AIRTABLE_PAT']}",
               "Content-Type": "application/json"}
    if not ensure_table(base_id, headers):
        return

    today  = datetime.now(timezone.utc).date().isoformat()
    cutoff = (datetime.now(timezone.utc).date()
              - timedelta(days=HISTORY_DAYS)).isoformat()
    rows = {k: v for k, v in long_rows.items() if k[2] >= cutoff}

    at = AirtableClient(TABLE_NAME)
    n  = at.build_cache("Key")
    print(f"[long] {n} existing row(s) in {TABLE_NAME!r}")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    tally, frozen = defaultdict(int), 0
    for (rep, email, day, group, metric), value in sorted(rows.items()):
        key = f"{rep} | {day} | {group} | {metric}"
        # Closed days are a record, not a projection — see
        # bd_company_funnel._is_closed.
        if funnel._is_closed(day, today) and key in at._cache:
            frozen += 1
            continue
        action, _ = at.upsert(
            "Key", key,
            {"Key": key, "Date": day, "Week": funnel._iso_week(day),
             "Month": day[:7], "BD Email": email, "BD Associate": rep,
             "Metric Group": group, "Metric": metric, "Value": value,
             "Updated At": stamp},
            stamp, updated_at_field="")
        tally[action] += 1
    at.flush()
    print(f"[long] {TABLE_NAME}: created={tally['created']} "
          f"updated={tally['updated']} skipped={tally['skipped']} frozen={frozen}")

    funnel.prune_expired(TABLE_NAME, cutoff)


_TD  = 'style="padding:6px 10px;border:1px solid #ddd;font-size:12px;"'
_TDR = 'style="padding:6px 10px;border:1px solid #ddd;font-size:12px;text-align:right;"'
_TH  = ('style="padding:6px 10px;border:1px solid #ddd;font-size:11px;'
       'text-align:right;background:#f2f2f2;"')
_THL = ('style="padding:6px 10px;border:1px solid #ddd;font-size:11px;'
       'text-align:left;background:#f2f2f2;"')

# (header label, metric group, metric name, which window)
# window: "today" = today's Date; "week" = sum over this ISO Week;
#         "month" = sum over this Month (also the sort key).
_DIGEST_COLUMNS = [
    ("Call Attempted",     "Contact", "Call Attempted",     "today"),
    ("Call Connected",     "Contact", "Call Connected",     "today"),
    ("Meeting Booked",     "Contact", "Meeting Booked",     "today"),
    ("SQL (This Month)",   "Contact", "SQL",                "month"),
    ("Companies Worked",   "Company", "Companies Worked",   "today"),
    ("Companies Reached",  "Company", "Companies Reached",  "today"),
    ("Requirements Stated", "Company", "Requirements Stated", "today"),
    ("Handoff Calls (This Week)", "Company", "Handoff Calls Held", "week"),
]


def team_digest_rows(long_rows: dict, today: str) -> list:
    """
    One row per BD associate, built entirely from THIS run's long_rows — no
    extra Airtable read needed, since build_long() already buckets every
    contact by its effective_call_date regardless of which day that falls on.

    "today"/"week"/"month" windows per _DIGEST_COLUMNS. Sorted by the SQL
    (This Month) column descending — the explicit ask: highest SQL count for
    the month at the top.
    """
    week, month = funnel._iso_week(today), today[:7]
    by_rep = defaultdict(lambda: defaultdict(int))   # rep -> header -> value
    emails = {}

    for (rep, email, day, group, metric), value in long_rows.items():
        emails.setdefault(rep, email)
        for header, want_group, want_metric, window in _DIGEST_COLUMNS:
            if group != want_group or metric != want_metric:
                continue
            if ((window == "today" and day == today) or
                (window == "week" and funnel._iso_week(day) == week) or
                (window == "month" and day[:7] == month)):
                by_rep[rep][header] += value

    # Iterate every rep EVER seen in long_rows, not just those with a row
    # inside one of the digest windows — a rep idle all day/week/month must
    # still appear, showing zeros, rather than silently vanish from the table.
    rows = [{"rep": rep, "email": emails.get(rep, ""), **by_rep.get(rep, {})}
            for rep in emails]
    rows.sort(key=lambda r: -r.get("SQL (This Month)", 0))
    return rows


def build_digest_html(rows: list, today: str) -> str:
    head = "".join(f'<th {_THL if i == 0 else _TH}>{h}</th>'
                   for i, (h, *_r) in enumerate([("BD Associate",)] + _DIGEST_COLUMNS))
    body = ""
    for r in rows:
        cells = f'<td {_TD}>{r["rep"]}</td>'
        for header, *_rest in _DIGEST_COLUMNS:
            cells += f'<td {_TDR}>{r.get(header, 0)}</td>'
        body += f"<tr>{cells}</tr>"
    table = (f'<table style="border-collapse:collapse;width:100%;margin:12px 0;">'
            f'<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>')
    return (
        '<!DOCTYPE html><html><body style="font-family:Arial,sans-serif;'
        'color:#333;max-width:900px;margin:0 auto;padding:20px;">'
        f'<p style="font-weight:bold;font-size:15px;margin:0 0 4px;">'
        f'BD Daily Digest — {today}</p>'
        '<p style="font-size:12px;color:#777;margin:0 0 8px;">'
        'Sorted by SQL (This Month), highest first. "Today" columns are '
        'today\'s activity; "This Week"/"This Month" columns are running '
        'totals for the current period.</p>'
        + table +
        '<p style="color:#999;font-size:11px;margin:20px 0 0;">— Kylas Sync</p>'
        '</body></html>'
    )


def send_team_digest(long_rows: dict, today: str) -> None:
    """One email, to the whole team, replacing the old per-person
    1:30pm/6:30pm sends. See modules/04_email_alert.py for the retired
    per-person builder — kept in the repo, no longer called from run_sync.py."""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    if not smtp_user or not smtp_pass:
        print("[long] SMTP_USER / SMTP_PASS not set — skipping team digest")
        return

    import json
    tp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "config", "team.json")
    try:
        with open(tp) as fh:
            cfg = json.load(fh)
    except Exception as exc:
        print(f"[long] WARNING: team.json unreadable, digest not sent — {exc}")
        return

    roster_emails = {str(m.get("email", "")).strip().lower()
                     for m in (cfg.get("bd_team") or []) if m.get("email")}
    cc_list = cfg.get("cc", [])
    rows = team_digest_rows(long_rows, today)
    to_list = sorted({r["email"] for r in rows if r["email"].lower() in roster_emails}
                     | roster_emails)
    if not to_list:
        print("[long] WARNING: no recipients resolved, digest not sent")
        return

    msg = MIMEMultipart("alternative")
    msg["From"] = smtp_user
    msg["To"] = ", ".join(to_list)
    if cc_list:
        msg["CC"] = ", ".join(cc_list)
    msg["Subject"] = f"BD Daily Digest — {today}"
    msg.attach(MIMEText(build_digest_html(rows, today), "html", "utf-8"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as s:
            s.ehlo(); s.starttls()
            s.login(smtp_user, smtp_pass)
            s.sendmail(smtp_user, to_list + list(cc_list), msg.as_string())
        from utils.redact import mask_emails
        print(f"[long] Team digest sent → {len(to_list)} recipient(s) "
              f"{mask_emails(to_list)} (cc: {mask_emails(cc_list)})")
    except Exception as exc:
        print(f"[long] WARNING: team digest send failed — {exc}")


def summarise(long_rows: dict) -> None:
    """Totals per metric for the most recent month present — a sanity read."""
    if not long_rows:
        return
    month = max(k[2][:7] for k in long_rows)
    tot = defaultdict(int)
    for (_r, _e, day, group, metric), v in long_rows.items():
        if day[:7] == month:
            tot[(group, metric)] += v
    print(f"\n{month} totals")
    print("-" * 46)
    for group in ("Contact", "Company"):
        for metric in (CONTACT_METRICS if group == "Contact" else COMPANY_METRICS):
            print(f"  {group:<8} {metric:<22} {tot[(group, metric)]:>7}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="print a summary, write nothing to Airtable")
    ap.add_argument("--all-owners", action="store_true",
                    help="include owners outside the BD roster (diagnostic)")
    ap.add_argument("--no-email", action="store_true",
                    help="update Airtable only, skip the team digest email")
    args = ap.parse_args()

    today = datetime.now(timezone.utc).date().isoformat()
    long_rows, _ = build_long(KylasClient(), all_owners=args.all_owners)
    summarise(long_rows)
    if args.dry_run:
        print(f"[long] dry run — nothing written ({len(long_rows)} rows)")
        return 0
    push(long_rows)
    if not args.no_email:
        send_team_digest(long_rows, today)
    return 0


if __name__ == "__main__":
    sys.exit(main())
