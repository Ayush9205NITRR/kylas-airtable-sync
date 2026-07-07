"""
Account Health — per-company POC health stats + weekly re-assignment alerts.

Airtable column mapping (what gets written where):
─────────────────────────────────────────────────────────────────────────────
  Column                 Source                 Use
  ──────────────────────────────────────────────────────────────────────────
  Total POCs             count of contacts      All contacts linked to company
  YtBM POCs              stage blank/YtBM       Never been called — untouched
  Active POCs            CNC / Follow-up        Being worked, not yet connected
  MQL POCs               MQL / Activation       Connected — needs push to DCB
  Hot POCs               SQL / DCB / Offsite    Live pipeline (hot!)
  Connected POCs         MQL+Activation+SQL+DCB Warm+hot combined (summary)
  Terminal POCs          NOI+Invalid+NDM+etc.   Dead ends — no more calls
  NOI Count              "Not Interested" only  Key exhaustion signal (≥2 = gone)
  Called Since Apr 19    cfLastCalledAt ≥ Apr19 How many contacts tapped since cutoff
  Last Called At         max(cfLastCalledAt)     Last time any POC in this company was called
  Account Status         computed (see below)   Health label for filtering
  Needs Re-assign        YtBM>0 AND called=0    Has untouched POCs, nobody called since Apr 19

Account Status logic (best POC stage wins across all POCs):
─────────────────────────────────────────────────────────────────────────────
  Offsite Delayed    — any POC at Offsite Delayed stage (highest priority)
  Offsite Done       — any POC at Offsite Done stage
  SQL                — any POC at SQL stage
  Discovery Call Stage— any POC at DCB / Reschedule / Closing Loops stages
  MQL - Action Needed— any POC at MQL / Activation (no higher stage)
  Active             — any POC at CNC / Follow-up stages, or has been called
  Exhausted          — no positive pipeline POC AND (NOI ≥ 2 OR all terminal)
  Fresh              — no contact has ever been called (lowest priority)

Re-assignment logic (Needs Re-assign = true when):
  • Account has YtBM POCs (untouched contacts exist)  AND
  • Called Since Apr 19 = 0  (nobody has called this account since Apr 19)
  Use the Airtable filter: [YtBM POCs] > 0 AND [Called Since Apr 19] = 0
  → these are safe to reassign (haven't been tapped by current owner)

Email schedule:
  • Daily sync (run_sync.py full_day): updates Airtable silently (send_email=False)
  • Weekly workflow (account_health_weekly.yml): sends the digest email
"""
import argparse
import json
import os
import smtplib
import sys
import time
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.kylas_client import KylasClient
from utils.airtable_client import AirtableClient
from utils.bd_metrics import contact_stage

TEAM_PATH       = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "team.json")
FM_PATH         = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "field_map.json")
REASSIGN_CUTOFF = "2026-04-19"

_TERMINAL_STAGES = {
    "Not Interested",
    "Invalid Contact",
    "Not a Decision Maker (NDM)",
    "Disqualified - Wrong POC",
    "POC - Organisation - Changed",
}

_ACTIVE_STAGES = {
    "CNC (Could Not Connect) - 1",
    "CNC (Could Not Connect) - 2",
    "Followup - CNC",
    "Follow-up (1)",
    "Follow-up (2)",
    "Follow-up (3)",
}

_CNC_STAGES = {
    "CNC (Could Not Connect) - 1",
    "CNC (Could Not Connect) - 2",
    "Followup - CNC",
}

_MQL_ACTION_STAGES = {
    "MQL (Marketing Qualified Lead)",
    "Activation",
}

_DCB_STAGES = {
    "Discovery Call Booked",
    "Reschedule Pending",
    "Closing Loops - Low Value",
    "Discovery Call No-Show",
    "Discovery Call Done - Awaiting Client Inputs",
}

_SQL_STAGES = {
    "SQL (Sales Qualified Lead)",
}

_OFFSITE_STAGES = {
    "Offsite Delayed",
}

_OFFSITE_DONE_STAGES = {
    "Offsite Done",
}


def _parse_lc(raw: str) -> str:
    """Parse cfLastCalledAt value → ISO date "YYYY-MM-DD". Returns "" on failure."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if raw[0].isdigit():
        return raw[:10]
    try:
        return datetime.strptime(raw.split(" at ")[0], "%b %d, %Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _contact_owner_email(ct: dict, user_email_map: dict) -> str:
    """Extract owner email from a raw Kylas contact dict."""
    ob = ct.get("ownedBy") or {}
    if not isinstance(ob, dict):
        return ""
    email = ob.get("email", "")
    if email:
        return email
    name = (ob.get("name") or
            f"{ob.get('firstName', '')} {ob.get('lastName', '')}".strip())
    return user_email_map.get(name, "") if name and user_email_map else ""


def compute_health(contacts: list, user_email_map: dict = None) -> dict:
    """
    contacts: raw Kylas contact dicts.
    user_email_map: {owner_name: email} — used to populate claimed_by.
    Returns {kylas_company_id (str): health_dict}.
    """
    if user_email_map is None:
        user_email_map = {}
    by_co = {}
    for ct in contacts:
        co    = ct.get("company")
        co_id = str(co) if isinstance(co, (int, float)) else (
            str(co.get("id", "")) if isinstance(co, dict) else ""
        )
        if not co_id:
            continue

        cf    = ct.get("customFieldValues") or {}
        stage = contact_stage(ct)
        lc    = _parse_lc(cf.get("cfLastCalledAt", ""))

        e = by_co.setdefault(co_id, {
            "total": 0, "ytbm": 0, "active": 0,
            "cnc": 0, "mql": 0,
            "sql": 0, "dcb": 0, "offsite": 0, "offsite_done": 0,
            "terminal": 0, "noi": 0,
            "called": 0, "called_apr19": 0, "last_called": "",
            "claimed_by": "",
            "_claimed_date": "",
        })
        e["total"] += 1

        if not stage or stage == "Yet to Be Mined":
            e["ytbm"] += 1
        elif stage in _TERMINAL_STAGES:
            e["terminal"] += 1
            if stage == "Not Interested":
                e["noi"] += 1
        elif stage in _ACTIVE_STAGES:
            e["active"] += 1
            if stage in _CNC_STAGES:
                e["cnc"] += 1
        elif stage in _MQL_ACTION_STAGES:
            e["mql"] += 1
        elif stage in _SQL_STAGES:
            e["sql"] += 1
        elif stage in _DCB_STAGES:
            e["dcb"] += 1
        elif stage in _OFFSITE_STAGES:
            e["offsite"] += 1
        elif stage in _OFFSITE_DONE_STAGES:
            e["offsite_done"] += 1
        # else: unmapped stage → counted in total only

        if lc:
            e["called"] += 1
            if lc >= REASSIGN_CUTOFF:
                e["called_apr19"] += 1
                if lc > e["_claimed_date"]:
                    e["_claimed_date"] = lc
                    e["claimed_by"] = _contact_owner_email(ct, user_email_map)
            if lc > e["last_called"]:
                e["last_called"] = lc

    for e in by_co.values():
        t    = e["total"]
        term = e["terminal"]
        noi  = e["noi"]

        # Status — best POC stage wins; Exhausted only when no positive pipeline POC remains.
        # Priority: Offsite Delayed > Offsite Done > SQL > Discovery Call Stage > MQL > Active > Exhausted > Fresh
        if e["offsite"] > 0:
            e["status"] = "Offsite Delayed"
        elif e["offsite_done"] > 0:
            e["status"] = "Offsite Done"
        elif e["sql"] > 0:
            e["status"] = "SQL"
        elif e["dcb"] > 0:
            e["status"] = "Discovery Call Stage"
        elif e["mql"] > 0:
            e["status"] = "MQL - Action Needed"
        elif e["active"] > 0:
            e["status"] = "Active"
        elif noi >= 2 or (t > 0 and term >= t):
            e["status"] = "Exhausted"
        elif e["called"] > 0:
            e["status"] = "Active"
        else:
            e["status"] = "Fresh"

        # hot = sql + dcb + offsite stages combined (keeps existing Airtable "Hot POCs" column working)
        e["hot"] = e["sql"] + e["dcb"] + e["offsite"] + e["offsite_done"]
        e["connected"] = e["mql"] + e["hot"]

        e["is_exhausted"] = bool(
            e["mql"] > 0 or e["hot"] > 0 or
            e["cnc"] >= 3 or e["noi"] >= 3
        )
        e["needs_exhaust"] = not e["is_exhausted"]

        # Status of Reachout — single column summarising tapped/stale + pipeline status
        # "Stale"                = nobody called this account since Apr 19
        # "Tapped – <status>"   = at least one contact called since Apr 19;
        #                         <status> is the existing account status (Exhausted,
        #                         Near Exhausted, Hot Pipeline, MQL - Action Needed,
        #                         Active, Fresh)
        if e["called_apr19"] > 0:
            e["status_of_reachout"] = f"Tapped – {e['status']}"
        else:
            e["status_of_reachout"] = "Stale"

        # Re-assign: has untouched POCs but no call since Apr 19
        e["needs_reassign"] = bool(e["ytbm"] > 0 and e["called_apr19"] == 0)

    return by_co


def _norm_name(val) -> str:
    """Lowercase, trimmed company name for matching. Handles list values."""
    if isinstance(val, list):
        val = val[0] if val else ""
    return str(val or "").strip().lower()


def _write_table(tbl: AirtableClient, health: dict, fm: dict,
                 id_to_name: dict = None) -> tuple:
    """
    Write health stats to one Airtable table. Returns (updated, skipped).

    Matches each company by its Kylas id (fm["id"] — the field name differs
    per base). When the id isn't found and id_to_name is supplied, falls back
    to matching by company name (fm["name"]) and backfills the id field so the
    next run matches directly.
    """
    updated = skipped = 0
    id_field   = fm["id"]
    name_field = fm.get("name")

    # One fetch → build id-cache + name-index together (with simple retry).
    records = None
    for attempt in range(4):
        try:
            records = tbl.table.all()
            break
        except Exception as exc:
            if attempt < 3:
                time.sleep(2 ** attempt)
            else:
                print(f"[Account Health] WARNING: could not read {id_field!r} — {exc}")
                return 0, 0

    tbl._cache = {}
    name_index, name_dupes = {}, set()
    for r in records:
        kid = str(r["fields"].get(id_field, "")).strip()
        if kid:
            tbl._cache[kid] = r
        if name_field and id_to_name:
            nm = _norm_name(r["fields"].get(name_field, ""))
            if nm:
                if nm in name_index:
                    name_dupes.add(nm)
                else:
                    name_index[nm] = r
    for d in name_dupes:          # drop ambiguous names — don't guess
        name_index.pop(d, None)

    _FIELD_MAP = [
        ("totalPocs",        "total"),
        ("ytbmPocs",         "ytbm"),
        ("activePocs",       "active"),
        ("mqlPocs",          "mql"),
        ("hotPocs",          "hot"),
        ("connectedPocs",    "connected"),
        ("terminalPocs",     "terminal"),
        ("noiCount",         "noi"),
        ("calledSinceApr19", "called_apr19"),
    ]

    matched_by_name = 0
    for co_id, e in health.items():
        rec = tbl._cache.get(co_id)
        by_name = False
        if rec is None and id_to_name:
            nm = _norm_name(id_to_name.get(co_id, ""))
            if nm:
                rec = name_index.get(nm)
                by_name = rec is not None
        if rec is None:
            skipped += 1
            continue

        fields = {}
        for fm_key, stat_key in _FIELD_MAP:
            at_field = fm.get(fm_key)
            if at_field:
                fields[at_field] = e[stat_key]

        if fm.get("lastCalledAtContacts") and e["last_called"]:
            fields[fm["lastCalledAtContacts"]] = e["last_called"]
        if fm.get("needsReassign"):
            fields[fm["needsReassign"]] = e["needs_reassign"]
        if fm.get("claimedBy"):
            fields[fm["claimedBy"]] = e.get("claimed_by", "")
        if fm.get("statusOfReachout"):
            sor = e.get("status_of_reachout", "Stale")
            lc  = e.get("last_called", "")
            fields[fm["statusOfReachout"]] = f"{sor} | Last Call: {lc}" if lc else f"{sor} | Last Call: —"
        if by_name:                       # backfill id so next run matches directly
            fields[id_field] = co_id
            matched_by_name += 1

        tbl._updates.append((co_id, rec["id"], fields))
        updated += 1

    tbl.flush()
    if matched_by_name:
        print(f"[Account Health]   ({matched_by_name} matched by name, id backfilled)")
    return updated, skipped


# ── HTML email helpers ────────────────────────────────────────────────────────

_TH    = ('style="background:#f2f2f2;text-align:left;padding:7px 12px;'
          'border:1px solid #ccc;font-size:12px;"')
_TD    = 'style="padding:7px 12px;border:1px solid #ccc;font-size:12px;"'
_TDR   = 'style="padding:7px 12px;border:1px solid #ccc;font-size:12px;text-align:right;"'
_TABLE = 'style="border-collapse:collapse;width:100%;margin:6px 0 18px;"'
_SEC   = 'style="font-size:14px;font-weight:bold;margin:22px 0 4px;color:#333;"'
_BODY  = ('style="font-family:Arial,sans-serif;color:#333;'
          'max-width:760px;margin:0 auto;padding:24px 20px;"')
_BADGE = {
    "Exhausted":                        "background:#d32f2f;color:#fff;",
    "Offsite Delayed":                  "background:#e65100;color:#fff;",
    "Offsite Done":                     "background:#2e7d32;color:#fff;",
    "SQL":                              "background:#1565c0;color:#fff;",
    "Discovery Call Stage":             "background:#00838f;color:#fff;",
    "MQL - Action Needed":              "background:#7b1fa2;color:#fff;",
    "Active":                           "background:#388e3c;color:#fff;",
    "Fresh":                            "background:#888;color:#fff;",
    "Stale":                            "background:#9e9e9e;color:#fff;",
    "Tapped – Exhausted":               "background:#d32f2f;color:#fff;",
    "Tapped – Offsite Delayed":         "background:#e65100;color:#fff;",
    "Tapped – Offsite Done":            "background:#2e7d32;color:#fff;",
    "Tapped – SQL":                     "background:#1565c0;color:#fff;",
    "Tapped – Discovery Call Stage":    "background:#00838f;color:#fff;",
    "Tapped – MQL - Action Needed":     "background:#7b1fa2;color:#fff;",
    "Tapped – Active":                  "background:#388e3c;color:#fff;",
    "Tapped – Fresh":                   "background:#aaa;color:#fff;",
}


def _badge(status: str) -> str:
    style = _BADGE.get(status, "background:#888;color:#fff;")
    return (f'<span style="font-size:10px;padding:2px 6px;border-radius:3px;'
            f'{style}">{status}</span>')


def _tr(s, n=35):
    s = (s or "").strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def _scalar(val, default: str) -> str:
    if isinstance(val, list):
        val = val[0] if val else None
    return str(val).strip() if val else default


def _owner_of(co_id: str, tbl_cache: dict) -> str:
    rec = tbl_cache.get(co_id)
    if not rec:
        return "—"
    f = rec.get("fields", {})
    return _scalar(f.get("Owner") or f.get("Owner - Kylas"), "—")


def _name_of(co_id: str, tbl_cache: dict) -> str:
    rec = tbl_cache.get(co_id)
    if not rec:
        return co_id
    f = rec.get("fields", {})
    return _scalar(f.get("Company Name") or f.get("Company Name - Kylas"), co_id)


def _mk_table(header_html: str, rows: str) -> str:
    return (f'<table {_TABLE}><thead><tr>{header_html}</tr></thead>'
            f'<tbody>{rows}</tbody></table>')


def _build_email(health: dict, tbl_cache: dict, friendly: str) -> str:
    """Management weekly digest — sections keyed on Status of Reachout."""
    sor_groups: dict = {}
    for co_id, e in health.items():
        sor = e.get("status_of_reachout", "Stale")
        sor_groups.setdefault(sor, []).append(co_id)

    total_stale   = len(sor_groups.get("Stale", []))
    total_t_act   = len(sor_groups.get("Tapped – Active", []))
    total_t_mql   = len(sor_groups.get("Tapped – MQL - Action Needed", []))
    total_t_dcb   = len(sor_groups.get("Tapped – Discovery Call Stage", []))
    total_t_sql   = len(sor_groups.get("Tapped – SQL", []))
    total_t_offd  = len(sor_groups.get("Tapped – Offsite Done", []))
    total_t_off   = len(sor_groups.get("Tapped – Offsite Delayed", []))
    total_t_ex    = len(sor_groups.get("Tapped – Exhausted", []))

    summary = (
        '<table style="border-collapse:collapse;margin:12px 0 20px;"><tr>'
        + "".join(
            f'<td style="padding:6px 10px;text-align:center;">'
            f'{_badge(st)}<br>'
            f'<span style="font-size:18px;font-weight:bold;">{cnt}</span>'
            f'</td>'
            for st, cnt in [
                ("Stale",                        total_stale),
                ("Tapped – Active",              total_t_act),
                ("Tapped – MQL - Action Needed", total_t_mql),
                ("Tapped – Discovery Call Stage",total_t_dcb),
                ("Tapped – SQL",                 total_t_sql),
                ("Tapped – Offsite Done",        total_t_offd),
                ("Tapped – Offsite Delayed",     total_t_off),
                ("Tapped – Exhausted",           total_t_ex),
            ]
        )
        + '</tr></table>'
    )
    sections = summary

    # ── Stale ─────────────────────────────────────────────────────────────────
    stale_cos = sorted(sor_groups.get("Stale", []),
                       key=lambda c: health[c]["total"], reverse=True)[:25]
    if stale_cos:
        note = f" (top {len(stale_cos)} of {total_stale})" if total_stale > len(stale_cos) else ""
        hdr = (f'<th {_TH}>Company</th><th {_TH}>Owner</th>'
               f'<th {_TH}>Total</th><th {_TH}>YtBM</th>'
               f'<th {_TH}>Active</th><th {_TH}>Last Call</th>')
        rows = "".join(
            f'<tr>'
            f'<td {_TD}>{_tr(_name_of(c, tbl_cache), 38)}</td>'
            f'<td {_TD}>{_tr(_owner_of(c, tbl_cache), 20)}</td>'
            f'<td {_TDR}>{health[c]["total"]}</td>'
            f'<td {_TDR}>{health[c]["ytbm"]}</td>'
            f'<td {_TDR}>{health[c]["active"]}</td>'
            f'<td {_TD}>{health[c]["last_called"] or "—"}</td>'
            f'</tr>'
            for c in stale_cos
        )
        sections += (
            f'<p {_SEC}>⚪ Stale ({total_stale} accounts{note})</p>'
            f'<p style="font-size:12px;color:#666;margin:0 0 6px;">'
            f'No call since April 19 — reassign or reactivate</p>'
            + _mk_table(hdr, rows)
        )

    # ── Tapped – Exhausted ────────────────────────────────────────────────────
    t_ex_cos = sorted(sor_groups.get("Tapped – Exhausted", []),
                      key=lambda c: health[c]["noi"], reverse=True)[:15]
    if t_ex_cos:
        note = f" (top {len(t_ex_cos)} of {total_t_ex})" if total_t_ex > len(t_ex_cos) else ""
        hdr = (f'<th {_TH}>Company</th><th {_TH}>Claimed By</th>'
               f'<th {_TH}>Total</th><th {_TH}>NOI</th>'
               f'<th {_TH}>Terminal</th><th {_TH}>Last Call</th>')
        rows = "".join(
            f'<tr>'
            f'<td {_TD}>{_tr(_name_of(c, tbl_cache), 38)}</td>'
            f'<td {_TD}>{_tr(health[c].get("claimed_by", "—"), 28)}</td>'
            f'<td {_TDR}>{health[c]["total"]}</td>'
            f'<td {_TDR} style="color:#d32f2f;font-weight:bold;">{health[c]["noi"]}</td>'
            f'<td {_TDR}>{health[c]["terminal"]}</td>'
            f'<td {_TD}>{health[c]["last_called"] or "—"}</td>'
            f'</tr>'
            for c in t_ex_cos
        )
        sections += (
            f'<p {_SEC}>🔴 Tapped – Exhausted ({total_t_ex} accounts{note})</p>'
            f'<p style="font-size:12px;color:#666;margin:0 0 6px;">'
            f'Called since Apr 19 but fully exhausted — needs fresh POC injection</p>'
            + _mk_table(hdr, rows)
        )

    # ── Tapped – Offsite Delayed ──────────────────────────────────────────────
    t_off_cos = sorted(sor_groups.get("Tapped – Offsite Delayed", []),
                       key=lambda c: health[c]["offsite"], reverse=True)[:15]
    if t_off_cos:
        note = f" (top {len(t_off_cos)} of {total_t_off})" if total_t_off > len(t_off_cos) else ""
        hdr = (f'<th {_TH}>Company</th><th {_TH}>Claimed By</th>'
               f'<th {_TH}>Offsite</th><th {_TH}>SQL</th><th {_TH}>Last Call</th>')
        rows = "".join(
            f'<tr>'
            f'<td {_TD}>{_tr(_name_of(c, tbl_cache), 38)}</td>'
            f'<td {_TD}>{_tr(health[c].get("claimed_by", "—"), 28)}</td>'
            f'<td {_TDR} style="color:#e65100;font-weight:bold;">{health[c]["offsite"]}</td>'
            f'<td {_TDR}>{health[c]["sql"]}</td>'
            f'<td {_TD}>{health[c]["last_called"] or "—"}</td>'
            f'</tr>'
            for c in t_off_cos
        )
        sections += (
            f'<p {_SEC}>🟠 Tapped – Offsite Delayed ({total_t_off} accounts{note})</p>'
            f'<p style="font-size:12px;color:#666;margin:0 0 6px;">'
            f'Offsite stage — follow up and reschedule</p>'
            + _mk_table(hdr, rows)
        )

    # ── Tapped – SQL ──────────────────────────────────────────────────────────
    t_sql_cos = sorted(sor_groups.get("Tapped – SQL", []),
                       key=lambda c: health[c]["sql"], reverse=True)[:15]
    if t_sql_cos:
        note = f" (top {len(t_sql_cos)} of {total_t_sql})" if total_t_sql > len(t_sql_cos) else ""
        hdr = (f'<th {_TH}>Company</th><th {_TH}>Claimed By</th>'
               f'<th {_TH}>SQL</th><th {_TH}>DCB</th><th {_TH}>Last Call</th>')
        rows = "".join(
            f'<tr>'
            f'<td {_TD}>{_tr(_name_of(c, tbl_cache), 38)}</td>'
            f'<td {_TD}>{_tr(health[c].get("claimed_by", "—"), 28)}</td>'
            f'<td {_TDR} style="color:#1565c0;font-weight:bold;">{health[c]["sql"]}</td>'
            f'<td {_TDR}>{health[c]["dcb"]}</td>'
            f'<td {_TD}>{health[c]["last_called"] or "—"}</td>'
            f'</tr>'
            for c in t_sql_cos
        )
        sections += (
            f'<p {_SEC}>🔵 Tapped – SQL ({total_t_sql} accounts{note})</p>'
            f'<p style="font-size:12px;color:#666;margin:0 0 6px;">'
            f'Sales Qualified — push to Discovery Call / Offsite</p>'
            + _mk_table(hdr, rows)
        )

    # ── Tapped – Discovery Call Stage ─────────────────────────────────────────
    t_dcb_cos = sorted(sor_groups.get("Tapped – Discovery Call Stage", []),
                       key=lambda c: health[c]["dcb"], reverse=True)[:15]
    if t_dcb_cos:
        note = f" (top {len(t_dcb_cos)} of {total_t_dcb})" if total_t_dcb > len(t_dcb_cos) else ""
        hdr = (f'<th {_TH}>Company</th><th {_TH}>Claimed By</th>'
               f'<th {_TH}>DCB</th><th {_TH}>MQL</th><th {_TH}>Last Call</th>')
        rows = "".join(
            f'<tr>'
            f'<td {_TD}>{_tr(_name_of(c, tbl_cache), 38)}</td>'
            f'<td {_TD}>{_tr(health[c].get("claimed_by", "—"), 28)}</td>'
            f'<td {_TDR} style="color:#00838f;font-weight:bold;">{health[c]["dcb"]}</td>'
            f'<td {_TDR}>{health[c]["mql"]}</td>'
            f'<td {_TD}>{health[c]["last_called"] or "—"}</td>'
            f'</tr>'
            for c in t_dcb_cos
        )
        sections += (
            f'<p {_SEC}>🩵 Tapped – Discovery Call Stage ({total_t_dcb} accounts{note})</p>'
            f'<p style="font-size:12px;color:#666;margin:0 0 6px;">'
            f'Discovery Call scheduled / done — push to SQL or Offsite</p>'
            + _mk_table(hdr, rows)
        )

    # ── Tapped – MQL Action Needed ────────────────────────────────────────────
    t_mql_cos = sorted(sor_groups.get("Tapped – MQL - Action Needed", []),
                       key=lambda c: health[c]["mql"], reverse=True)[:15]
    if t_mql_cos:
        note = f" (top {len(t_mql_cos)} of {total_t_mql})" if total_t_mql > len(t_mql_cos) else ""
        hdr = (f'<th {_TH}>Company</th><th {_TH}>Claimed By</th>'
               f'<th {_TH}>MQL</th><th {_TH}>Active</th><th {_TH}>Last Call</th>')
        rows = "".join(
            f'<tr>'
            f'<td {_TD}>{_tr(_name_of(c, tbl_cache), 38)}</td>'
            f'<td {_TD}>{_tr(health[c].get("claimed_by", "—"), 28)}</td>'
            f'<td {_TDR} style="color:#7b1fa2;font-weight:bold;">{health[c]["mql"]}</td>'
            f'<td {_TDR}>{health[c]["active"]}</td>'
            f'<td {_TD}>{health[c]["last_called"] or "—"}</td>'
            f'</tr>'
            for c in t_mql_cos
        )
        sections += (
            f'<p {_SEC}>🟣 Tapped – MQL Action Needed ({total_t_mql} accounts{note})</p>'
            f'<p style="font-size:12px;color:#666;margin:0 0 6px;">'
            f'Has MQL/Activation POCs — needs push to Discovery Call</p>'
            + _mk_table(hdr, rows)
        )

    if not (stale_cos or t_ex_cos or t_off_cos or t_sql_cos or t_dcb_cos or t_mql_cos):
        sections += '<p style="color:#555;">All accounts look healthy — no action needed.</p>'

    today      = date.today()
    week_label = f"Week of {today.strftime('%b %d, %Y')}"

    return (
        f'<!DOCTYPE html><html><body {_BODY}>'
        '<p>Hi team,</p>'
        f'<p style="font-weight:bold;font-size:15px;margin:0 0 2px;">'
        f'Account Health — Weekly Digest &nbsp;&middot;&nbsp; {week_label}</p>'
        f'<p style="font-size:12px;color:#666;margin:0 0 4px;">'
        f'Stale = no call since Apr 19 &nbsp;|&nbsp; '
        f'Tapped = called since Apr 19 &nbsp;|&nbsp; '
        f'Exhausted = NOI≥2 or all terminal</p>'
        + sections
        + '<p style="color:#999;font-size:11px;margin-top:24px;">— Kylas Sync</p>'
        '</body></html>'
    )


def _build_poc_email(first_name: str,
                     stale_accounts: list, tapped_accounts: list,
                     health: dict, tbl_cache: dict, week_label: str) -> str:
    """HTML email for one rep: Section 1 = Stale, Section 2 = Tapped-Unexhausted."""
    hdr = (f'<th {_TH}>Company</th>'
           f'<th {_TH}>Status of Reachout</th>'
           f'<th {_TH}>Total</th>'
           f'<th {_TH}>YtBM</th>'
           f'<th {_TH}>CNC</th>'
           f'<th {_TH}>NOI</th>'
           f'<th {_TH}>MQL/SQL</th>'
           f'<th {_TH}>Last Call</th>')

    def _td_n(val, color=""):
        style = f'text-align:right;padding:7px 12px;border:1px solid #ccc;font-size:12px;{color}'
        return f'<td style="{style}">{val}</td>'

    def _rows_for(accounts):
        return "".join(
            '<tr>'
            f'<td {_TD}>{_tr(_name_of(c, tbl_cache), 38)}</td>'
            f'<td {_TD}>{_badge(health[c].get("status_of_reachout", "Stale"))}</td>'
            + _td_n(health[c]["total"])
            + _td_n(health[c]["ytbm"], "color:#888;")
            + _td_n(health[c]["cnc"])
            + _td_n(health[c]["noi"], "color:#d32f2f;" if health[c]["noi"] else "")
            + _td_n(health[c]["mql"] + health[c]["hot"],
                    "color:#7b1fa2;" if (health[c]["mql"] + health[c]["hot"]) else "")
            + f'<td {_TD}>{health[c]["last_called"] or "—"}</td>'
            '</tr>'
            for c in accounts
        )

    _pri = {"Fresh": 0, "Active": 1, "MQL - Action Needed": 2,
            "Discovery Call Stage": 3, "SQL": 4, "Offsite Done": 5,
            "Offsite Delayed": 6, "Exhausted": 7}

    sections = ""

    if stale_accounts:
        stale_sorted = sorted(stale_accounts,
                              key=lambda c: (_pri.get(health[c]["status"], 9),
                                             -health[c]["total"]))
        sections += (
            f'<p {_SEC}>⚪ Stale Accounts ({len(stale_sorted)}) — Reach out</p>'
            f'<p style="font-size:12px;color:#666;margin:0 0 6px;">'
            f'Nobody has called these accounts since April 19. '
            f'Please reach out this week or release them for reassignment.</p>'
            + _mk_table(hdr, _rows_for(stale_sorted))
        )

    if tapped_accounts:
        tapped_sorted = sorted(tapped_accounts,
                               key=lambda c: (_pri.get(health[c]["status"], 9),
                                              -health[c]["ytbm"]))
        sections += (
            f'<p {_SEC}>🔄 Keep Working ({len(tapped_sorted)}) — Push to exhaustion</p>'
            f'<p style="font-size:12px;color:#666;margin:0 0 6px;">'
            f'You\'ve called these since April 19 but they\'re not yet exhausted. '
            f'Exhausted = MQL/SQL/DCB contact, <b>OR</b> 3+ CNC, <b>OR</b> 3+ NOI.</p>'
            + _mk_table(hdr, _rows_for(tapped_sorted))
        )

    if not sections:
        sections = '<p style="color:#555;">No pending accounts this week — great work!</p>'

    n_s = len(stale_accounts)
    n_t = len(tapped_accounts)
    summary_line = " &nbsp;|&nbsp; ".join(filter(None, [
        f'<b>{n_s}</b> stale account{"s" if n_s != 1 else ""}' if n_s else "",
        f'<b>{n_t}</b> to exhaust' if n_t else "",
    ]))

    return (
        f'<!DOCTYPE html><html><body {_BODY}>'
        f'<p>Hi {first_name},</p>'
        f'<p style="font-weight:bold;font-size:15px;margin:0 0 4px;">'
        f'Your Accounts This Week &nbsp;&middot;&nbsp; {week_label}</p>'
        f'<p style="font-size:12px;color:#555;margin:0 0 16px;">{summary_line}</p>'
        + sections
        + '<p style="color:#999;font-size:11px;margin-top:24px;">— Kylas Sync</p>'
        '</body></html>'
    )


def _send_poc_emails(health: dict, tbl_cache: dict, cfg: dict,
                     smtp_user: str, smtp_pass: str) -> None:
    """
    One email per rep with two sections:
      • Stale accounts  — Status of Reachout = "Stale" → current Airtable owner
      • Tapped but unexhausted — called since Apr 19, not yet exhausted → claimed_by
    """
    if not smtp_user or not smtp_pass:
        print("[Account Health] SMTP not configured — skipping POC emails")
        return

    today       = date.today()
    week_label  = f"Week of {today.strftime('%b %d, %Y')}"
    user_emails = cfg.get("kylas_user_emails", {})
    cc_list     = cfg.get("cc", [])

    # Build per-email buckets
    stale_by_email:  dict = {}
    tapped_by_email: dict = {}

    # Reverse map: email → name (for resolving claimed_by email → owner name for display)
    email_to_name = {v.lower(): k for k, v in user_emails.items()}

    for co_id, e in health.items():
        sor = e.get("status_of_reachout", "")

        # Section 1: Stale — send to current Airtable owner
        if sor == "Stale":
            owner = _owner_of(co_id, tbl_cache)
            if owner and owner != "—":
                em = user_emails.get(owner, "")
                if not em:
                    em = next((v for k, v in user_emails.items()
                               if owner.lower() in k.lower() or k.lower() in owner.lower()), "")
                if em:
                    stale_by_email.setdefault(em.lower(), []).append(co_id)

        # Section 2: Tapped but not exhausted — send to whoever called last (claimed_by)
        if e.get("needs_exhaust") and sor.startswith("Tapped"):
            claimed = (e.get("claimed_by") or "").strip().lower()
            if claimed:
                tapped_by_email.setdefault(claimed, []).append(co_id)
            else:
                # Fall back to Airtable owner
                owner = _owner_of(co_id, tbl_cache)
                if owner and owner != "—":
                    em = user_emails.get(owner, "")
                    if em:
                        tapped_by_email.setdefault(em.lower(), []).append(co_id)

    all_emails = set(stale_by_email) | set(tapped_by_email)
    if not all_emails:
        print("[Account Health] No accounts to report — skipping POC emails")
        return

    for email in sorted(all_emails):
        stale_cos  = stale_by_email.get(email, [])
        tapped_cos = tapped_by_email.get(email, [])
        if not stale_cos and not tapped_cos:
            continue

        # Resolve display name from email
        name  = email_to_name.get(email, email)
        first = name.split()[0] if " " in name else name.split("@")[0].capitalize()

        body    = _build_poc_email(first, stale_cos, tapped_cos, health, tbl_cache, week_label)
        subject = f"Your Accounts This Week — {week_label}"
        eff_cc  = [a for a in cc_list if a.lower() != email]

        msg            = MIMEMultipart("alternative")
        msg["From"]    = smtp_user
        msg["To"]      = email
        msg["Subject"] = subject
        if eff_cc:
            msg["CC"] = ", ".join(eff_cc)
        msg.attach(MIMEText(body, "html", "utf-8"))

        try:
            with smtplib.SMTP("smtp.gmail.com", 587) as s:
                s.ehlo(); s.starttls()
                s.login(smtp_user, smtp_pass)
                s.sendmail(smtp_user, [email] + eff_cc, msg.as_string())
            print(f"[Account Health] POC email → {name} <{email}> "
                  f"({len(stale_cos)} stale + {len(tapped_cos)} tapped-unexhausted)")
        except Exception as exc:
            print(f"[Account Health] WARNING: POC email failed for {email} — {exc}")


def run(kylas=None, send_email: bool = True) -> dict:
    """
    send_email=False: only update Airtable (used by daily run_sync.py).
    send_email=True:  update Airtable + send weekly digest email.
    """
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")

    with open(FM_PATH) as f:
        fm_all  = json.load(f)
    fm_list = fm_all.get("company", {})
    fm_crm  = fm_all.get("company_crm", {})

    with open(TEAM_PATH) as f:
        cfg = json.load(f)
    recipients = cfg.get("cc", [])

    if kylas is None:
        kylas = KylasClient()

    # Build owner name → email map for "Claimed By" field
    user_email_map = cfg.get("kylas_user_emails", {})
    try:
        api_emails = kylas.get_user_emails()
        user_email_map.update(api_emails)
    except Exception as _e:
        print(f"[Account Health] WARNING: user email fetch failed ({_e}) — using team.json")

    from utils.bd_metrics import refresh_stage_map
    refresh_stage_map(kylas)   # bare-id stages must resolve or they bucket nowhere

    print("[Account Health] Fetching all contacts from Kylas...")
    contacts = kylas._search_all(
        "contact",
        fields=["id", "company", "ownedBy", "updatedAt", "customFieldValues"],
    )
    print(f"[Account Health] {len(contacts)} contacts fetched")

    health = compute_health(contacts, user_email_map=user_email_map)

    counts = {s: sum(1 for e in health.values() if e["status"] == s)
              for s in ("Fresh", "Active", "MQL - Action Needed",
                        "Discovery Call Stage", "SQL", "Offsite Done",
                        "Offsite Delayed", "Exhausted")}
    needs_re = sum(1 for e in health.values() if e["needs_reassign"])
    print(f"[Account Health] {len(health)} companies  |  " +
          "  ".join(f"{s.split()[0]}={v}" for s, v in counts.items()) +
          f"  Needs-Re-assign={needs_re}")

    company_base = os.environ.get("AIRTABLE_COMPANY_BASE_ID") or os.environ["AIRTABLE_BASE_ID"]
    crm_base     = os.environ["AIRTABLE_BASE_ID"]

    tbl_list_cache = {}
    try:
        tbl_list = AirtableClient("Company List", base_id=company_base)
        upd, skp = _write_table(tbl_list, health, fm_list)
        tbl_list_cache = tbl_list._cache
        print(f"[Account Health] Company List → {upd} updated, {skp} no Kylas ID")
    except Exception as exc:
        print(f"[Account Health] WARNING: Company List write failed — {exc}")

    # co_id → company name (from Company List, which has every company by id).
    # Lets the CRM write match by name when its records lack a Kylas id.
    id_to_name = {}
    name_key = fm_list.get("name")
    for cid, rec in tbl_list_cache.items():
        nm = rec.get("fields", {}).get(name_key, "")
        if nm:
            id_to_name[cid] = nm

    tbl_crm_cache = {}
    try:
        tbl_crm = AirtableClient("Companies", base_id=crm_base)
        upd, skp = _write_table(tbl_crm, health, fm_crm, id_to_name=id_to_name)
        tbl_crm_cache = tbl_crm._cache
        print(f"[Account Health] Companies CRM → {upd} updated, {skp} unmatched")
    except Exception as exc:
        print(f"[Account Health] WARNING: Companies CRM write failed — {exc}")

    if not send_email:
        print("[Account Health] Airtable updated (email skipped — use weekly workflow to send)")
        return health

    if not smtp_user or not smtp_pass:
        print("[Account Health] SMTP not configured — skipping email")
        return health
    if not recipients:
        print("[Account Health] No recipients in team.json cc — skipping email")
        return health

    today      = date.today()
    week_label = f"Week of {today.strftime('%b %d')}"
    tbl_cache  = tbl_crm_cache or tbl_list_cache
    body       = _build_email(health, tbl_cache, week_label)
    subject    = f"Account Health Weekly | {week_label}"

    msg            = MIMEMultipart("alternative")
    msg["From"]    = smtp_user
    msg["To"]      = ", ".join(recipients)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "html", "utf-8"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as s:
            s.ehlo(); s.starttls()
            s.login(smtp_user, smtp_pass)
            s.sendmail(smtp_user, recipients, msg.as_string())
        print(f"[Account Health] Weekly digest sent → {', '.join(recipients)}")
    except Exception as exc:
        print(f"[Account Health] WARNING: email send failed — {exc}")

    # Per-POC exhaust emails
    stale_ct   = sum(1 for e in health.values() if e.get("status_of_reachout") == "Stale")
    tapped_ct  = sum(1 for e in health.values()
                     if e.get("needs_exhaust") and
                     (e.get("status_of_reachout") or "").startswith("Tapped"))
    print(f"[Account Health] {stale_ct} stale + {tapped_ct} tapped-unexhausted → sending per-POC emails...")
    _send_poc_emails(health, tbl_cache, cfg, smtp_user, smtp_pass)

    return health


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-email", action="store_true",
                        help="Update Airtable only, skip email")
    args = parser.parse_args()
    from dotenv import load_dotenv
    load_dotenv()
    run(send_email=not args.no_email)
