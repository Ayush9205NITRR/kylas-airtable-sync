"""
Contact pipeline-stage history — detecting that a stage actually MOVED.

Kylas stores only a contact's CURRENT stage, with no change timestamps, so
"how many stages moved this week" is unanswerable from a single read. This
keeps a snapshot between runs and diffs against it: whatever differs from last
time is a change, recorded with its from-stage, to-stage and the date it was
observed.

The snapshot lives in git (state/contact_stage.json), not Airtable, for the
same reasons as the account-health one: Airtable silently skips creates at the
record cap, and its log tables self-prune. `git log -p` on that file is a
complete audit trail.

WHAT THIS IS NOT
────────────────────────────────────────────────────────────────────────────
It is not a call count, and it must not be labelled as one:

  * Resolution is one RUN, not one call. Two moves between runs collapse into
    a single change showing only the net start -> end.
  * A call that does not move the stage is invisible. Ringing a contact who
    stays at "CNC - 1" is real work that produces no change here.
  * A change can happen without a call — a bulk edit or an import moves the
    stage just as well.

So this measures PIPELINE PROGRESSION, which is the thing the stage list can
actually evidence. True call volume comes from Kylas /call-logs, which carries
the caller, the timestamp and the duration.

A contact seen for the first time is recorded as a baseline, never as a change:
otherwise the first run would report ~37k "changes" that are simply the initial
read.

LAST CALL DATE
────────────────────────────────────────────────────────────────────────────
Each entry also carries `last_call_date`: the date of that contact's most
recent DETECTED stage change, and nothing else. It is deliberately NOT set on
first sighting (creation is not a call) and NOT touched by an unchanged stage
(no move, no call). This is what makes it safe to use as "when was this
contact actually called" — unlike cfLastCalledAt (a manually-entered field,
often blank) or max(createdAt, updatedAt, cfLastCalledAt) (bumped by our OWN
automation editing an unrelated field, e.g. an owner reassignment).

Read it through effective_call_date(), not the raw dict: most contacts have no
detected change yet (tracking only just started), so every caller needs the
documented fallback rather than treating a blank as "never called".
"""
import json
import os
from datetime import datetime

SCHEMA_VERSION = 2

# schema_version history:
#   1 - stage, owner, email, since, changes
#   2 - + last_call_date: set ONLY when a real stage change is detected, never
#       on a contact's first sighting. See effective_call_date() below.

DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "state", "contact_stage.json",
)


def load(path: str = None) -> dict:
    """Read the previous snapshot. Missing/corrupt file → {} (first run)."""
    path = path or DEFAULT_PATH
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError):
        return {}
    return data.get("contacts", {}) if isinstance(data, dict) else {}


def save(snapshot: dict, path: str = None, today: str = "") -> None:
    """Write the snapshot, key-sorted so git diffs stay small and readable."""
    path = path or DEFAULT_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "_comment": "Contact pipeline-stage snapshot. Written by "
                    "scripts/bd_stage_changes.py; see utils/stage_history.py. "
                    "git history is the archive.",
        "schema_version": SCHEMA_VERSION,
        "as_of": today,
        "contacts": {k: snapshot[k] for k in sorted(snapshot)},
    }
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=0, sort_keys=False)
        fh.write("\n")
    os.replace(tmp, path)     # atomic: a killed run cannot half-write it


def diff(prev: dict, current: dict, today: str) -> tuple:
    """
    prev:    snapshot from the last run ({} on the very first run)
    current: {contact_id: {"stage", "owner", "email", "company"}} read now
    today:   ISO date the observation is attributed to

    Returns (new_snapshot, changes, stats). `changes` is a list of
    {contact_id, owner, email, company, from, to, date} — one per contact whose
    stage differs from last time. Contacts absent from `current` (deleted, or
    outside this run's filter) are carried through untouched.
    """
    out = dict(prev)
    changes = []
    stats = {"new": 0, "changed": 0, "unchanged": 0, "carried": 0}

    for cid, cur in current.items():
        stage = cur.get("stage", "")
        old = prev.get(cid)

        if old is None:
            # Creation is a baseline capture, not a call: last_call_date stays
            # blank until this contact's stage actually moves from here.
            out[cid] = {"stage": stage, "owner": cur.get("owner", ""),
                        "email": cur.get("email", ""), "since": today,
                        "changes": 0, "last_call_date": ""}
            stats["new"] += 1      # first sighting is a baseline, not a move
            continue

        entry = dict(old)
        entry.setdefault("last_call_date", "")   # adopt pre-v2 entries in place
        if stage != entry.get("stage"):
            changes.append({
                "contact_id": cid,
                "owner":   cur.get("owner", ""),
                "email":   cur.get("email", ""),
                "company": cur.get("company", ""),
                "from":    entry.get("stage", ""),
                "to":      stage,
                "date":    today,
            })
            entry["stage"] = stage
            entry["since"] = today
            entry["changes"] = int(entry.get("changes", 0)) + 1
            entry["last_call_date"] = today   # the ONLY thing that sets this
            stats["changed"] += 1
        else:
            stats["unchanged"] += 1

        # Ownership can move without the stage moving; keep it current either
        # way. Only overwrite with a non-blank value: different callers (this
        # module's diff() is invoked from both 02_contact_sync.py and
        # bd_stage_changes.py) may not all resolve an email, and a blank from
        # one caller must not erase a real value a previous caller recorded.
        if cur.get("owner"):
            entry["owner"] = cur["owner"]
        if cur.get("email"):
            entry["email"] = cur["email"]
        out[cid] = entry

    stats["carried"] = len(out) - len(current)
    return out, changes, stats


def effective_call_date(snapshot: dict, contact_id, fallback: str = "") -> str:
    """
    The date to treat as "this contact was actually called", for anything that
    used to read cfLastCalledAt (or the account-health activity composite)
    directly.

    Prefers the snapshot's detected last_call_date — set only by a REAL stage
    change, so it cannot be bumped by our own automation editing an unrelated
    field, the way the activity composite could.

    Falls back to `fallback` (caller-supplied: cfLastCalledAt, or the account
    health composite) when the snapshot has no detected change for this
    contact yet — which today is most contacts, since tracking only just
    started. Without this fallback, Account Health and BD daily counts would
    read as empty for weeks while real changes slowly accumulate. The stage-
    change date takes over for a contact automatically the first time it
    actually moves.
    """
    entry = snapshot.get(str(contact_id))
    if entry and entry.get("last_call_date"):
        return entry["last_call_date"]
    return fallback or ""
