"""
BD Monthly Matrix — per-rep, per-month funnel counts, written to Airtable.

One Airtable row per (BD rep × month) so the numbers can be filtered by month
in Airtable directly. Creates the table on first run.

Columns — canonical funnel definitions agreed 2026-08-24
──────────────────────────────────────────────────────
  Call Attempted — any stage except "Yet to Be Mined"/blank
  Call Connected — Attempted MINUS CNC-1/2/3 and Followup-CNC (i.e. the call
                   was picked up / a person was reached, whatever happened next)
  Meeting Booked — Discovery Call Booked, Reschedule Pending, Offsite Delayed,
                   Discovery Call No-Show (a meeting is/was on the calendar,
                   whether or not it has happened yet)
  Meeting Done   — Discovery Call Done - Awaiting Client Inputs,
                   Closing Loops - Low Value, Offsite Done (Late Reachout),
                   SQL (Sales Qualified Lead) (the meeting actually took place)
  SQL            — SQL (Sales Qualified Lead) only
  MQL            — MQL (Marketing Qualified Lead) only

Booked vs Done is intentionally non-overlapping: a stage counts in exactly one
of the two (e.g. Offsite Delayed = booked-not-done; Reschedule Pending and
Discovery Call No-Show are booked but did NOT happen, so neither is in Done).
Connected here is an EXCLUDE-list (not bd_metrics.CONNECTED_STAGES, which is a
curated include-list that silently drops any stage nobody remembered to add,
e.g. Reschedule Pending/Discovery Call No-Show/Closing Loops previously fell
through it despite obviously implying a connection). This file intentionally
does not share Connected/Meeting-Booked/Meeting-Done with utils/bd_metrics —
only Attempted does — so a change here does not ripple into the daily BD
emails / BD Trends without a separate, explicit decision to do that too.

Month attribution
─────────────────
month = the month of the contact's "Last Called At" date. Contacts with an
EMPTY last-called date are skipped entirely (they have no month to sit in),
which is what makes the Month column safe to filter on.

READ THIS BEFORE TRUSTING TRENDS
────────────────────────────────
A Kylas contact carries ONE current stage and ONE last-called date, so this is
a SNAPSHOT attributed to the month of the most recent call — not a historical
activity log. Consequences:
  * A contact called in June and again in July counts ONLY in July.
  * A contact's CURRENT stage is counted, even if it reached that stage in a
    later month than the call. E.g. called in June, became SQL in August →
    counted as an SQL in June.
So a month's row changes retroactively as its contacts are re-called or moved
on. It answers "of the contacts last touched in month M, where do they stand
now?" — not "what happened during month M". True per-month history would need
stage-change timestamps, which Kylas does not expose on the contact record.

Run:  python scripts/bd_monthly_matrix.py            (write to Airtable)
      python scripts/bd_monthly_matrix.py --dry-run  (print table only)
"""
import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

TABLE_NAME = "BD Monthly Matrix"
META = "https://api.airtable.com/v0/meta/bases"

# Stage sets specific to this report. Kylas stores a plain hyphen "-"; the
# spec was written with an en-dash "–", so all comparisons run through
# _norm() which folds dash variants and case. Without that these silently
# match nothing and every count reads 0.
MEETING_BOOKED_STAGES = {
    "Discovery Call Booked",
    "Reschedule Pending",
    "Offsite Delayed",
    "Discovery Call No-Show",
}
MEETING_DONE_STAGES = {
    "Discovery Call Done - Awaiting Client Inputs",
    "Closing Loops - Low Value",
    "Offsite Done (Late Reachout)",
    "SQL (Sales Qualified Lead)",
}
MQL_STAGES = {"MQL (Marketing Qualified Lead)"}

# "Connected" = Attempted minus these (couldn't-connect outcomes only).
CNC_EXCLUDE_STAGES = {
    "CNC (Could Not Connect) - 1",
    "CNC (Could Not Connect) - 2",
    "CNC (Could Not Connect) - 3",
    "Followup - CNC",
}

COLUMNS = ["Call Attempted", "Call Connected", "Meeting Booked",
           "Meeting Done", "SQL", "MQL"]


def _norm(s: str) -> str:
    """Fold dash variants + case so en-dash/em-dash spellings still match."""
    return (str(s or "").strip().lower()
            .replace("–", "-").replace("—", "-").replace("−", "-"))


_MEETING_BOOKED_N = {_norm(s) for s in MEETING_BOOKED_STAGES}
_MEETING_DONE_N   = {_norm(s) for s in MEETING_DONE_STAGES}
_MQL_N            = {_norm(s) for s in MQL_STAGES}
_CNC_EXCLUDE_N    = {_norm(s) for s in CNC_EXCLUDE_STAGES}


def _parse_lc(raw: str) -> str:
    """cfLastCalledAt → 'YYYY-MM-DD'. '' when absent/unparseable.

    Mirrors modules/06_account_health.py: Kylas returns either an ISO string
    or a display form like 'Jun 22, 2026 at 05:22 PM'.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    if raw[0].isdigit():
        return raw[:10]
    try:
        return datetime.strptime(raw.split(" at ")[0], "%b %d, %Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _owner_name(ct: dict, user_map: dict = None) -> str:
    """Resolve a contact's BD owner name.

    Mirrors modules/02_contact_sync.py: the contact SEARCH API usually returns
    a bare `ownerId` rather than a populated `ownedBy` dict (same pattern as
    pipeline stages coming back as bare option ids). Without the ownerId
    fallback every contact resolves to "Unassigned" and the whole per-rep
    split collapses into one row.
    """
    ob = ct.get("ownedBy")
    if isinstance(ob, dict):
        name = (ob.get("name")
                or f"{ob.get('firstName', '')} {ob.get('lastName', '')}".strip())
        if name:
            return name
    oid = ct.get("ownerId") or (ob if isinstance(ob, (int, float)) else None)
    if oid and user_map:
        name = user_map.get(str(int(oid))) or user_map.get(int(oid))
        if name:
            return name
    return "Unassigned"


def _build_user_map(kylas) -> dict:
    """{str(uid): name} from team.json plus the live Kylas user list."""
    umap = {}
    tp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "config", "team.json")
    try:
        import json
        with open(tp) as fh:
            for uid, name in (json.load(fh).get("kylas_users") or {}).items():
                umap[str(uid)] = name
    except Exception as exc:
        print(f"[matrix] WARN: team.json users unusable ({exc})")
    try:
        for uid, name in (kylas.get_users() or {}).items():
            umap.setdefault(str(uid), name)
    except Exception as exc:
        print(f"[matrix] WARN: live user list unavailable ({exc})")
    print(f"[matrix] {len(umap)} Kylas users mapped for owner resolution")
    return umap


def build_matrix(kylas) -> tuple:
    """Return ({(rep, month): {col: count}}, stats)."""
    from utils.bd_metrics import refresh_stage_map, contact_stage, ATTEMPTED_EXCLUDE
    refresh_stage_map(kylas)   # bare option ids must resolve to real labels

    exclude_n = {_norm(s) for s in ATTEMPTED_EXCLUDE}

    user_map = _build_user_map(kylas)

    print("[matrix] Fetching contacts from Kylas...")
    # ownerId is REQUIRED: the search API returns it instead of a populated
    # ownedBy dict, and without it every row collapses to "Unassigned".
    # updatedAt is REQUIRED too: _search_all pages past the ~10k search cap
    # using it as a cursor, so omitting it silently truncates to 10,000.
    contacts = kylas._search_all(
        "contact",
        fields=["id", "ownedBy", "ownerId", "updatedAt", "customFieldValues"],
    )
    print(f"[matrix] {len(contacts)} contacts fetched")

    grid = defaultdict(lambda: dict.fromkeys(COLUMNS, 0))
    no_lc = 0
    for ct in contacts:
        cf = ct.get("customFieldValues") or {}
        lc = _parse_lc(cf.get("cfLastCalledAt", ""))
        if not lc:
            no_lc += 1          # no month to attribute → excluded by design
            continue
        month = lc[:7]                     # YYYY-MM
        stage = _norm(contact_stage(ct))
        cell  = grid[(_owner_name(ct, user_map), month)]

        attempted = bool(stage) and stage not in exclude_n
        if attempted:
            cell["Call Attempted"] += 1
        if attempted and stage not in _CNC_EXCLUDE_N:
            cell["Call Connected"] += 1
        if stage in _MEETING_BOOKED_N:
            cell["Meeting Booked"] += 1
        if stage in _MEETING_DONE_N:
            cell["Meeting Done"] += 1
        if stage == _norm("SQL (Sales Qualified Lead)"):
            cell["SQL"] += 1
        if stage in _MQL_N:
            cell["MQL"] += 1

    stats = {"contacts": len(contacts), "no_last_called": no_lc,
             "counted": len(contacts) - no_lc, "rows": len(grid)}
    print(f"[matrix] {stats['counted']} contacts have a Last Called date "
          f"({no_lc} skipped as empty) → {len(grid)} rep×month rows")
    return grid, stats


def print_table(grid: dict) -> None:
    months = sorted({m for _, m in grid}, reverse=True)
    reps   = sorted({r for r, _ in grid})
    w = max([len(r) for r in reps] + [10])
    print(f"\n{'MONTH':<9} {'BD REP':<{w}} " + " ".join(f"{c:>21}" for c in COLUMNS))
    for m in months:
        tot = dict.fromkeys(COLUMNS, 0)
        for r in reps:
            if (r, m) not in grid:
                continue
            c = grid[(r, m)]
            print(f"{m:<9} {r:<{w}} " + " ".join(f"{c[k]:>21}" for k in COLUMNS))
            for k in COLUMNS:
                tot[k] += c[k]
        print(f"{'':<9} {'TOTAL':<{w}} " + " ".join(f"{tot[k]:>21}" for k in COLUMNS))
        print()


def _patch_existing_columns(base_id: str, headers: dict, table: dict) -> None:
    """Bring an already-existing table's columns up to the current schema:
    rename the two pre-2026-08-24 columns to their agreed names (Airtable
    field renames keep the field's data/position; push_to_airtable() always
    overwrites values on its next run, so the renamed cells self-correct to
    the new definitions), and add the MQL column if it's missing."""
    table_id = table["id"]
    fields   = {f["name"]: f["id"] for f in table.get("fields", [])}

    renames = [("Discovery Call Booked", "Meeting Booked"),
               ("Meeting Happened", "Meeting Done")]
    for old, new in renames:
        if new in fields:
            continue
        if old not in fields:
            print(f"[matrix]   ! neither {old!r} nor {new!r} found — add {new!r} manually")
            continue
        resp = requests.patch(
            f"{META}/{base_id}/tables/{table_id}/fields/{fields[old]}",
            json={"name": new}, headers=headers, timeout=30,
        )
        if resp.status_code == 200:
            print(f"[matrix]   renamed {old!r} -> {new!r}")
            fields[new] = fields.pop(old)
        else:
            print(f"[matrix]   ! rename {old!r} -> {new!r} FAILED {resp.status_code}: {resp.text[:200]}")

    if "MQL" not in fields:
        resp = requests.post(
            f"{META}/{base_id}/tables/{table_id}/fields",
            json={"name": "MQL", "type": "number", "options": {"precision": 0}},
            headers=headers, timeout=30,
        )
        if resp.status_code in (200, 201):
            print("[matrix]   + added MQL column")
        else:
            print(f"[matrix]   ! add MQL FAILED {resp.status_code}: {resp.text[:200]}")


def ensure_table(base_id: str, headers: dict) -> bool:
    r = requests.get(f"{META}/{base_id}/tables", headers=headers, timeout=30)
    r.raise_for_status()
    existing = next((t for t in r.json().get("tables", []) if t["name"] == TABLE_NAME), None)
    if existing:
        print(f"[matrix] Airtable table {TABLE_NAME!r} already exists")
        _patch_existing_columns(base_id, headers, existing)
        return True
    defn = {
        "name": TABLE_NAME,
        "description": ("Per BD rep, per month funnel counts (Attempted / Connected / "
                        "Meeting Booked / Meeting Done / SQL / MQL). Month = month of "
                        "the contact's Last Called date; contacts with no last-called "
                        "date are excluded. Snapshot of CURRENT stage, not history "
                        "— see scripts/bd_monthly_matrix.py."),
        "fields": [
            {"name": "Key",    "type": "singleLineText"},   # "<rep> | YYYY-MM"
            {"name": "BD Rep", "type": "singleLineText"},
            {"name": "Month",  "type": "singleLineText"},   # YYYY-MM, sorts + filters
        ] + [{"name": c, "type": "number", "options": {"precision": 0}}
             for c in COLUMNS]
          + [{"name": "Updated At", "type": "singleLineText"}],
    }
    resp = requests.post(f"{META}/{base_id}/tables", json=defn, headers=headers, timeout=30)
    if resp.status_code in (200, 201):
        print(f"[matrix] Created Airtable table {TABLE_NAME!r}")
        return True
    print(f"[matrix] ERROR creating {TABLE_NAME!r}: {resp.status_code} {resp.text[:300]}")
    return False


def push_to_airtable(grid: dict) -> None:
    from utils.airtable_client import AirtableClient
    base_id = os.environ["AIRTABLE_BASE_ID"]
    headers = {"Authorization": f"Bearer {os.environ['AIRTABLE_PAT']}",
               "Content-Type": "application/json"}
    if not ensure_table(base_id, headers):
        return

    at = AirtableClient(TABLE_NAME)
    n  = at.build_cache("Key")
    print(f"[matrix] {n} existing row(s) in {TABLE_NAME!r}")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    tally = defaultdict(int)
    for (rep, month), counts in sorted(grid.items()):
        key = f"{rep} | {month}"
        action, _ = at.upsert(
            "Key", key,
            {"Key": key, "BD Rep": rep, "Month": month,
             **{c: counts[c] for c in COLUMNS}, "Updated At": stamp},
            stamp, updated_at_field="")   # always refresh: counts move over time
        tally[action] += 1
    at.flush()   # returns None; counts come from the upsert() actions above
    print(f"[matrix] Airtable: created={tally['created']} updated={tally['updated']} "
          f"skipped={tally['skipped']}")


def diagnose(kylas) -> None:
    """Explain the SQL count: where every SQL-ish contact actually sits.

    Read-only. Answers the three ways SQL can look 'too low':
      1. contacts whose stage IS SQL but have no Last Called date (excluded)
      2. contacts that WERE SQL and have since moved further down the pipeline
         (only the CURRENT stage is stored, so they no longer count as SQL)
      3. a stage id that used to be mislabelled SQL in the static map
    """
    from collections import Counter
    from utils.bd_metrics import refresh_stage_map, contact_stage, _PIPELINE_STAGE
    refresh_stage_map(kylas)

    contacts = kylas._search_all(
        "contact", fields=["id", "ownedBy", "ownerId", "updatedAt", "customFieldValues"])
    print(f"\n[diag] {len(contacts)} contacts fetched\n")

    by_stage, by_stage_lc, sql_month, raw_ids = Counter(), Counter(), Counter(), Counter()
    for ct in contacts:
        cf    = ct.get("customFieldValues") or {}
        stage = contact_stage(ct) or "(no stage)"
        lc    = _parse_lc(cf.get("cfLastCalledAt", ""))
        by_stage[stage] += 1
        if lc:
            by_stage_lc[stage] += 1
        if _norm(stage) == _norm("SQL (Sales Qualified Lead)"):
            sql_month[lc[:7] if lc else "(NO LAST CALLED DATE)"] += 1
            rv = cf.get("cfPipelineStageBd")
            raw_ids[rv.get("id") if isinstance(rv, dict) else rv] += 1

    print("[diag] Every stage — total vs. those WITH a Last Called date:")
    print(f"    {'stage':<46}{'total':>7}{'with LC':>9}{'no LC':>7}")
    for stage, n in by_stage.most_common():
        w = by_stage_lc[stage]
        print(f"    {stage:<46}{n:>7}{w:>9}{n - w:>7}")

    tot = by_stage[ "SQL (Sales Qualified Lead)" ]
    wlc = by_stage_lc["SQL (Sales Qualified Lead)"]
    print(f"\n[diag] Contacts CURRENTLY at 'SQL (Sales Qualified Lead)': {tot}")
    print(f"[diag]   with a Last Called date (counted in the matrix): {wlc}")
    print(f"[diag]   WITHOUT one (excluded by the empty-date rule)  : {tot - wlc}")
    print("[diag] SQL by month:")
    for m, n in sorted(sql_month.items(), reverse=True):
        print(f"      {m:<26}{n:>6}")
    print(f"[diag] raw option id(s) behind those SQL contacts: {dict(raw_ids)}")

    print("\n[diag] Stages a contact may have moved to AFTER being SQL — a contact")
    print("[diag] here was likely an SQL earlier, but only its CURRENT stage is stored,")
    print("[diag] so it no longer counts toward SQL:")
    post = ["Offsite Delayed", "Offsite Done (Late Reachout)", "Offsite Done",
            "Discovery Call Booked", "Discovery Call Done - Awaiting Client Inputs",
            "Closing Loops - Low Value", "Discovery Call No-Show", "Reschedule Pending"]
    carry = 0
    for s in post:
        if by_stage.get(s):
            carry += by_stage_lc[s]
            print(f"      {s:<46}{by_stage[s]:>7}{by_stage_lc[s]:>9}")
    print(f"[diag] → {carry} such contacts have a Last Called date. If 'SQL' is meant as")
    print(f"[diag]   'reached SQL at some point', the figure is nearer {wlc + carry}, not {wlc}.")

    print("\n[diag] Option ids the live picklist maps to an SQL-looking label:")
    for oid, label in sorted(_PIPELINE_STAGE.items()):
        if "sql" in str(label).lower() or "qualified" in str(label).lower():
            print(f"      {oid}  {label!r}")
    print("[diag] NOTE: id 2870484 was previously mislabelled 'SQL (Sales Qualified Lead)'")
    print("[diag] in the static map; the live picklist says 'Disqualified - Wrong POC'.")
    print("[diag] Any historical SQL figure built before that correction was inflated by it.")

    print("\n[diag] Meeting Booked / Meeting Done — exact stage behind each rep's count")
    print("[diag] (CURRENT stage, all months combined; no-Last-Called-date contacts excluded)")
    user_map = _build_user_map(kylas)
    booked_rs, done_rs = Counter(), Counter()
    for ct in contacts:
        cf = ct.get("customFieldValues") or {}
        if not _parse_lc(cf.get("cfLastCalledAt", "")):
            continue
        stage_raw = contact_stage(ct)
        stage_n   = _norm(stage_raw)
        rep       = _owner_name(ct, user_map)
        if stage_n in _MEETING_BOOKED_N:
            booked_rs[(rep, stage_raw)] += 1
        if stage_n in _MEETING_DONE_N:
            done_rs[(rep, stage_raw)] += 1

    rep_totals = Counter()
    for (r, _), n in booked_rs.items():
        rep_totals[r] += n
    for (r, _), n in done_rs.items():
        rep_totals[r] += n

    for rep, _ in rep_totals.most_common():
        b_total = sum(n for (r, s), n in booked_rs.items() if r == rep)
        d_total = sum(n for (r, s), n in done_rs.items() if r == rep)
        print(f"\n    {rep}  (Booked={b_total}  Done={d_total})")
        for (r, s), n in sorted(booked_rs.items(), key=lambda x: -x[1]):
            if r == rep:
                print(f"      BOOKED  {s:<46}{n:>5}")
        for (r, s), n in sorted(done_rs.items(), key=lambda x: -x[1]):
            if r == rep:
                print(f"      DONE    {s:<46}{n:>5}")


def main():
    ap = argparse.ArgumentParser(description="Build the BD monthly matrix in Airtable")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the table only; write nothing to Airtable")
    ap.add_argument("--diagnose", action="store_true",
                    help="Read-only: explain where the SQL counts come from, then exit")
    args = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv()
    from utils.kylas_client import KylasClient

    kylas = KylasClient()
    if args.diagnose:
        diagnose(kylas)
        return

    grid, _ = build_matrix(kylas)
    print_table(grid)
    if args.dry_run:
        print("[matrix] DRY RUN — nothing written to Airtable")
        return
    push_to_airtable(grid)


if __name__ == "__main__":
    main()
