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

A contact seen for the first time is normally recorded as a baseline, not a
change: otherwise the first run would report ~37k "changes" that are simply the
initial read.

The one exception is a contact that first appears ALREADY IN PROGRESS — bulk
imported straight in at, say, Activation rather than at the bottom of the
funnel. Work demonstrably happened on it, just before we were watching, so it
is logged as a change from nothing -> its stage. Suppressed entirely on a
bootstrap run (empty snapshot), where every contact is a first sighting and the
rule would otherwise fire ~20k times for no reason.

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

# The day "last call date" stopped being inferred and started being evidenced.
#
# BEFORE this date, effective_call_date() still honours the old fallback
# (cfLastCalledAt, or the account-health createdAt/updatedAt composite), so
# every figure already measured stays exactly as it was — closed days are a
# record, not a projection.
#
# FROM this date onward the fallback is ignored entirely: a contact counts as
# called on a day only if its pipeline stage actually MOVED, as detected by
# diff() against the previous run. The old composite could be bumped by our own
# automation editing an unrelated field (an owner reassignment, say), which is
# exactly what made it untrustworthy as a call signal.
#
# Consequence, and it is deliberate: forward-looking counts start near zero and
# fill in as real moves accumulate, because a contact that never moves never
# gets a date. That is the point — an unmoved contact was not evidence of a
# call before either, it just looked like one.
CALL_DATE_CUTOVER = "2026-09-05"


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


def diff(prev: dict, current: dict, today: str, is_call=None) -> tuple:
    """
    prev:    snapshot from the last run ({} on the very first run)
    current: {contact_id: {"stage", "owner", "email", "company"}} read now
    today:   ISO date the observation is attributed to
    is_call: optional callable(to_stage) -> bool. Decides whether landing on
             a given stage counts as a CALL (sets last_call_date), separately
             from whether the stage merely DIFFERS (which always updates
             `stage`/`since`/`changes` — the contact's position did move,
             that part is never in question).

             Without this, any difference sets last_call_date, which breaks
             on a picklist RENAME: Kylas relabelling id 2862826 from "Yet to
             Be Mined" to "LinkedIn Outreach Initiated" made every contact
             sitting at the bottom of the funnel look, once, like it had just
             been called — 12,638 of them, in the run that first hit it. Pass
             `is_call=lambda s: order.rank_of(s) != order.unmined_rank` (see
             utils/account_pipeline.py) so landing back on the bottom stage —
             whether by a rename or a real regression — is recorded as a
             stage change but never mistaken for a call.

    Returns (new_snapshot, changes, stats). `changes` is a list of
    {contact_id, owner, email, company, from, to, date} — one per contact whose
    stage differs from last time, REGARDLESS of is_call (the change is real
    either way; only whether it set last_call_date depends on is_call).
    Contacts absent from `current` (deleted, or outside this run's filter) are
    carried through untouched.
    """
    out = dict(prev)
    changes = []
    # A completely empty `prev` means we are bootstrapping (first ever run, or
    # state/contact_stage.json lost), NOT that 33k contacts all appeared at
    # once. Every contact is a first sighting on that run, so the
    # "started in progress" rule below would fire for every contact already
    # above the bottom stage — roughly 20k rows, all stamped with today's date,
    # none of them real activity. Same shape as the picklist-rename incident.
    # On a bootstrap run every first sighting is a baseline, no exceptions.
    bootstrapping = not prev
    stats = {"new": 0, "new_in_progress": 0, "changed": 0,
             "unchanged": 0, "carried": 0}

    for cid, cur in current.items():
        stage = cur.get("stage", "")
        old = prev.get(cid)

        if old is None:
            # A contact seen for the first time AT THE BOTTOM stage is just a
            # baseline capture — it was created, not worked, so no change and
            # no last_call_date. That is the normal case for a fresh import.
            #
            # But a contact that first appears ALREADY IN PROGRESS (imported
            # straight in at, say, Activation) did have work done on it; the
            # work simply happened before we were watching. Recording that as a
            # baseline would silently swallow it, and the contact would then
            # need to move AGAIN before it ever counted. So it is logged as a
            # change from nothing -> its stage.
            #
            # `is_call` is what distinguishes the two: it already answers "is
            # this stage above the bottom of the funnel", which is exactly the
            # question here. From-stage is left blank on purpose — rank_of("")
            # returns 0 silently, so it renders as an empty Previous Stage and
            # never trips the unranked-stage warning.
            started_in_progress = (not bootstrapping
                                   and is_call is not None and is_call(stage))
            if started_in_progress:
                changes.append({
                    "contact_id": cid,
                    "owner":   cur.get("owner", ""),
                    "email":   cur.get("email", ""),
                    "company": cur.get("company", ""),
                    "company_id": cur.get("company_id", ""),
                    "from":    "",
                    "to":      stage,
                    "date":    today,
                })
            out[cid] = {"stage": stage, "owner": cur.get("owner", ""),
                        "email": cur.get("email", ""), "since": today,
                        "changes": 1 if started_in_progress else 0,
                        "last_call_date": today if started_in_progress else ""}
            stats["new_in_progress" if started_in_progress else "new"] += 1
            continue

        entry = dict(old)
        entry.setdefault("last_call_date", "")   # adopt pre-v2 entries in place
        if stage != entry.get("stage"):
            changes.append({
                "contact_id": cid,
                "owner":   cur.get("owner", ""),
                "email":   cur.get("email", ""),
                "company": cur.get("company", ""),
                    "company_id": cur.get("company_id", ""),
                "from":    entry.get("stage", ""),
                "to":      stage,
                "date":    today,
            })
            entry["stage"] = stage
            entry["since"] = today
            entry["changes"] = int(entry.get("changes", 0)) + 1
            if is_call is None or is_call(stage):
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


def effective_call_date(snapshot: dict, contact_id, fallback: str = "",
                        cutover: str = None) -> str:
    """
    The date to treat as "this contact was actually called", for anything that
    used to read cfLastCalledAt (or the account-health activity composite)
    directly.

    Prefers the snapshot's detected last_call_date — set only by a REAL stage
    change, so it cannot be bumped by our own automation editing an unrelated
    field, the way the activity composite could.

    The `fallback` (caller-supplied: cfLastCalledAt, or the account-health
    composite) is honoured only for dates BEFORE `cutover`. See CALL_DATE_CUTOVER:
    history stays exactly as it was measured, while everything from the cutover
    onward must be evidenced by an actual stage move. Pass cutover="" to
    disable the rule (used by tests that assert the old behaviour).
    """
    entry = snapshot.get(str(contact_id))
    if entry and entry.get("last_call_date"):
        return entry["last_call_date"]

    fallback = fallback or ""
    if cutover is None:
        cutover = CALL_DATE_CUTOVER
    # ISO dates compare lexicographically — the same trick _is_closed() uses.
    if cutover and fallback and fallback >= cutover:
        return ""
    return fallback
