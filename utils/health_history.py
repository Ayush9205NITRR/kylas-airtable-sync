"""
Account Health history — the snapshot that makes health a trackable parameter.

Account Health is DERIVED, not stored: it is recomputed from contacts on every
run. On its own that means you can see what an account is today but never what
it was, so "did this account move this month?" is unanswerable. This module
keeps a snapshot between runs so each run can diff against the last one.

The snapshot lives in git (state/account_health.json), not Airtable:
  * git is free, unlimited and immutable, so history cannot be silently lost —
    Airtable's client SKIPS creates when the base hits its record cap, and the
    existing log tables prune themselves at 90 days.
  * `git log -p state/account_health.json` is a complete audit trail, and any
    past state is recoverable with `git show <sha>:state/account_health.json`.
Airtable then mirrors the per-account counters so reallocation can be done
there — but Airtable is the view, never the record.

Per-account entry:
    status        current Account Health
    baseline      status at the START of the current month
    prev          status immediately before the most recent change
    changed       ISO date of the most recent change ("" if never)
    count         number of changes WITHIN the current month
    month         "YYYY-MM" the baseline/count belong to
    status_since  ISO date the CURRENT status was first reached. Unlike
                  baseline/count this does NOT reset at a month boundary, so it
                  is what answers "unchanged for the last three months".
    months        {"YYYY-MM": status} — the status each month CLOSED at. Every
                  run overwrites the current month's slot, so whatever the last
                  run of a month wrote is that month's closing value. Trimmed to
                  the most recent MONTHS_KEPT months; git holds the rest.
    v             FORMULA_VERSION the status was computed under

Month scoping is deliberate: reallocation asks "did this account move during
September?", so baseline and count reset at each month boundary. Nothing is
lost — every prior month stays in git.

baseline and status_since answer different questions and both are needed. An
account that went Active → MQL → Active inside one month has count=2 and a
status_since from mid-month, but its baseline and current status both read
Active — so baseline alone would call it unchanged. status_since is what makes
"genuinely stable for N months" answerable.

FORMULA_VERSION exists because a change to the health FORMULA moves thousands
of accounts at once, and that is not business movement. When the version
changes, accounts are re-baselined instead of counted as changed, so a
definitional change can never be mistaken for real activity later.
"""
import json
import os

# Bump whenever the Account Health formula changes, and note why:
#   1 — status keyed off cfLastCalledAt alone ("called").
#   2 — status keyed off max(createdAt, updatedAt, cfLastCalledAt) ("activity"),
#       which retired the Fresh bucket.
FORMULA_VERSION = 2

# How many months of per-account closing statuses to carry in the snapshot.
# 24 keeps year-on-year comparison possible while bounding the file: every
# account carries at most this many entries. Older months stay in git history.
MONTHS_KEPT = 24

DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "state", "account_health.json",
)


def load(path: str = None) -> dict:
    """Read the previous snapshot. Missing/corrupt file → {} (first run)."""
    path = path or DEFAULT_PATH
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError):
        return {}
    return data.get("accounts", {}) if isinstance(data, dict) else {}


def save(snapshot: dict, path: str = None, today: str = "") -> None:
    """Write the snapshot, sorted so git diffs stay readable and minimal."""
    path = path or DEFAULT_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "_comment": "Account Health snapshot. Written by modules/06_account_health.py; "
                    "see utils/health_history.py. git history is the archive.",
        "formula_version": FORMULA_VERSION,
        "as_of": today,
        "accounts": {k: snapshot[k] for k in sorted(snapshot)},
    }
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, sort_keys=False)
        fh.write("\n")
    os.replace(tmp, path)   # atomic: a killed run cannot leave a half-written snapshot


def months_unchanged(entry: dict, today: str) -> int:
    """
    Whole calendar months the account has held its current status.

    Counts month boundaries crossed since status_since, so a status reached on
    5 Sep reads as 3 on 1 Dec. 0 means "changed this month". Filtering
    >= 3 in Airtable is the "stable for a quarter" reallocation question.
    """
    since = str(entry.get("status_since") or "")
    if len(since) < 7 or len(today) < 7:
        return 0
    try:
        sy, sm = int(since[:4]), int(since[5:7])
        ty, tm = int(today[:4]), int(today[5:7])
    except ValueError:
        return 0
    return max(0, (ty - sy) * 12 + (tm - sm))


def _trim_months(months: dict) -> dict:
    """Keep only the most recent MONTHS_KEPT entries, oldest dropped first."""
    if len(months) <= MONTHS_KEPT:
        return months
    keep = sorted(months)[-MONTHS_KEPT:]
    return {m: months[m] for m in keep}


def apply(prev: dict, health: dict, today: str) -> tuple:
    """
    Fold today's health into the previous snapshot.

    prev:   snapshot from the last run ({} on the very first run)
    health: {company_id: {"status": ...}} as computed this run
    today:  ISO date, e.g. "2026-09-01"

    Returns (new_snapshot, stats). Accounts present in prev but absent from
    health (deleted in Kylas) are carried through untouched — history intact.
    """
    month = today[:7]
    out = dict(prev)
    stats = {"new": 0, "changed": 0, "unchanged": 0,
             "rebaselined": 0, "month_rollover": 0, "carried": 0}

    for cid, e in health.items():
        status = e.get("status", "")
        old = prev.get(cid)

        if old is None:
            out[cid] = {"status": status, "baseline": status, "prev": "",
                        "changed": "", "count": 0, "month": month,
                        "status_since": today, "months": {month: status},
                        "v": FORMULA_VERSION}
            stats["new"] += 1
            continue

        entry = dict(old)
        # Entries written before status_since/months existed: adopt them rather
        # than resetting, so the baseline snapshot already on disk stays valid.
        entry.setdefault("status_since", entry.get("changed") or today)
        entry["months"] = dict(entry.get("months") or {})

        # A formula change moves accounts en masse and is not business movement:
        # re-baseline rather than record a change, so the count stays meaningful.
        if entry.get("v") != FORMULA_VERSION:
            entry.update({"status": status, "baseline": status, "prev": "",
                          "changed": "", "count": 0, "month": month,
                          "status_since": today, "v": FORMULA_VERSION})
            entry["months"][month] = status
            entry["months"] = _trim_months(entry["months"])
            out[cid] = entry
            stats["rebaselined"] += 1
            continue

        # New month: whatever the account carried in becomes this month's baseline.
        if entry.get("month") != month:
            entry["baseline"] = entry.get("status", status)
            entry["count"] = 0
            entry["month"] = month
            stats["month_rollover"] += 1

        if status != entry.get("status"):
            entry["prev"] = entry.get("status", "")
            entry["status"] = status
            entry["changed"] = today
            # Deliberately NOT reset by the month rollover above: this is the
            # clock that answers "stable for the last three months".
            entry["status_since"] = today
            entry["count"] = int(entry.get("count", 0)) + 1
            stats["changed"] += 1
        else:
            stats["unchanged"] += 1

        # Stamp the current month every run — the last write of a month is that
        # month's closing status, so no explicit month-end step is needed.
        entry["months"][month] = status
        entry["months"] = _trim_months(entry["months"])
        out[cid] = entry

    stats["carried"] = len(out) - len(health)
    return out, stats
