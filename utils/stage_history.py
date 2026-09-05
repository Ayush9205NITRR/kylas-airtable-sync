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
"""
import json
import os

SCHEMA_VERSION = 1

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
            out[cid] = {"stage": stage, "owner": cur.get("owner", ""),
                        "email": cur.get("email", ""), "since": today,
                        "changes": 0}
            stats["new"] += 1      # first sighting is a baseline, not a move
            continue

        entry = dict(old)
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
            stats["changed"] += 1
        else:
            stats["unchanged"] += 1

        # Ownership can move without the stage moving; keep it current either way.
        entry["owner"] = cur.get("owner", entry.get("owner", ""))
        entry["email"] = cur.get("email", entry.get("email", ""))
        out[cid] = entry

    stats["carried"] = len(out) - len(current)
    return out, changes, stats
