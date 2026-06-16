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

Account Status logic:
─────────────────────────────────────────────────────────────────────────────
  Exhausted          — NOI count ≥ 2 OR all contacts are terminal
  Near Exhausted     — NOI count = 1 OR ≥ 70% contacts are terminal
  Hot Pipeline       — has SQL / Discovery Call / Offsite contacts
  MQL - Action Needed— has MQL / Activation contacts (no Hot yet)
  Active             — has CNC / Follow-up contacts being worked
  Fresh              — no contact has ever been called (cfLastCalledAt empty)

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
    "Discovery Call No-Show",
    "Reschedule Pending",
}

# Subset of _ACTIVE_STAGES — contacts specifically in "Could Not Connect"
_CNC_STAGES = {
    "CNC (Could Not Connect) - 1",
    "CNC (Could Not Connect) - 2",
    "Followup - CNC",
}

# Connected — needs push from MQL/Activation → Discovery Call
_MQL_ACTION_STAGES = {
    "MQL (Marketing Qualified Lead)",
    "Activation",
}

# Hot pipeline — live SQL / Discovery Call / advanced stages
_HOT_STAGES = {
    "SQL (Sales Qualified Lead)",
    "Discovery Call Booked",
    "Offsite Delayed",
    "Discovery Call Done - Awaiting Client Inputs",
    "Closing Loops - Low Value",
    "Connect Later",
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
            "cnc": 0,
            "mql": 0, "hot": 0,
            "terminal": 0, "noi": 0,
            "called": 0, "called_apr19": 0, "last_called": "",
            "claimed_by": "",       # email of rep who called most recently since Apr 19
            "_claimed_date": "",    # internal: date of that call for comparison
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
        elif stage in _HOT_STAGES:
            e["hot"] += 1
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

        # Status — priority order matters
        if noi >= 2 or (t > 0 and term >= t):
            e["status"] = "Exhausted"
        elif noi == 1 or (t > 0 and term / t >= 0.7):
            e["status"] = "Near Exhausted"
        elif e["hot"] > 0:
            e["status"] = "Hot Pipeline"
        elif e["mql"] > 0:
            e["status"] = "MQL - Action Needed"
        elif e["active"] > 0 or e["called"] > 0:
            e["status"] = "Active"
        else:
            e["status"] = "Fresh"

        # connected = mql + hot  (summary "warm/hot" count for existing column)
        e["connected"] = e["mql"] + e["hot"]

        # An account is "properly exhausted" when it has pipeline value
        # OR tried 3+ CNC contacts OR has 3+ NOI rejections
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


def _write_table(tbl: AirtableClient, health: dict, fm: dict) -> tuple:
    """Write health stats to one Airtable table. Returns (updated, skipped)."""
    updated = skipped = 0
    try:
        tbl.build_cache("Kylas Company Id")
    except Exception as exc:
        print(f"[Account Health] WARNING: could not build cache — {exc}")
        return 0, 0

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
        ("accountStatus",    "status"),
    ]

    for co_id, e in health.items():
        if co_id not in tbl._cache:
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
            fields[fm["statusOfReachout"]] = e.get("status_of_reachout", "Stale")

        rid = tbl._cache[co_id]["id"]
        tbl._updates.append((co_id, rid, fields))
        updated += 1

    tbl.flush()
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
    "Exhausted":          "background:#d32f2f;color:#fff;",
    "Near Exhausted":     "background:#f57c00;color:#fff;",
    "Hot Pipeline":       "background:#1976d2;color:#fff;",
    "MQL - Action Needed":"background:#7b1fa2;color:#fff;",
    "Active":             "background:#388e3c;color:#fff;",
    "Fresh":              "background:#888;color:#fff;",
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
    # ── collect by status ──────────────────────────────────────────────────────
    needs_re   = sorted(
        [c for c, e in health.items() if e["needs_reassign"]],
        key=lambda c: health[c]["total"], reverse=True
    )[:20]
    exhausted  = sorted(
        [c for c, e in health.items() if e["status"] == "Exhausted"],
        key=lambda c: health[c]["noi"], reverse=True
    )[:15]
    near_ex    = sorted(
        [c for c, e in health.items() if e["status"] == "Near Exhausted"],
        key=lambda c: health[c]["noi"], reverse=True
    )[:15]
    mql_action = sorted(
        [c for c, e in health.items() if e["status"] == "MQL - Action Needed"],
        key=lambda c: health[c]["mql"], reverse=True
    )[:15]

    total_re  = sum(1 for e in health.values() if e["needs_reassign"])
    total_ex  = sum(1 for e in health.values() if e["status"] == "Exhausted")
    total_ne  = sum(1 for e in health.values() if e["status"] == "Near Exhausted")
    total_mql = sum(1 for e in health.values() if e["status"] == "MQL - Action Needed")
    total_hot = sum(1 for e in health.values() if e["status"] == "Hot Pipeline")
    total_act = sum(1 for e in health.values() if e["status"] == "Active")
    total_fr  = sum(1 for e in health.values() if e["status"] == "Fresh")

    # ── summary pills ──────────────────────────────────────────────────────────
    summary = (
        '<table style="border-collapse:collapse;margin:12px 0 20px;">'
        '<tr>'
        + "".join(
            f'<td style="padding:6px 10px;text-align:center;">'
            f'{_badge(st)}<br>'
            f'<span style="font-size:18px;font-weight:bold;">{cnt}</span>'
            f'</td>'
            for st, cnt in [
                ("Exhausted", total_ex),
                ("Near Exhausted", total_ne),
                ("Hot Pipeline", total_hot),
                ("MQL - Action Needed", total_mql),
                ("Active", total_act),
                ("Fresh", total_fr),
            ]
        )
        + '</tr></table>'
    )

    sections = summary

    # ── Needs Re-assign ────────────────────────────────────────────────────────
    if needs_re:
        note = f" (top {len(needs_re)} of {total_re})" if total_re > len(needs_re) else ""
        hdr  = (f'<th {_TH}>Company</th><th {_TH}>Owner</th>'
                f'<th {_TH}>Status</th>'
                f'<th {_TH}>Total</th><th {_TH}>YtBM</th>'
                f'<th {_TH}>Active</th><th {_TH}>Last Call</th>')
        rows = "".join(
            f'<tr>'
            f'<td {_TD}>{_tr(_name_of(c, tbl_cache), 38)}</td>'
            f'<td {_TD}>{_tr(_owner_of(c, tbl_cache), 20)}</td>'
            f'<td {_TD}>{_badge(health[c]["status"])}</td>'
            f'<td {_TDR}>{health[c]["total"]}</td>'
            f'<td {_TDR}>{health[c]["ytbm"]}</td>'
            f'<td {_TDR}>{health[c]["active"]}</td>'
            f'<td {_TD}>{health[c]["last_called"] or "—"}</td>'
            f'</tr>'
            for c in needs_re
        )
        sections += (
            f'<p {_SEC}>🔄 Needs Re-assign ({total_re} accounts{note})</p>'
            f'<p style="font-size:12px;color:#666;margin:0 0 6px;">'
            f'Has YtBM POCs but no call since April 19 — safe to reassign</p>'
            + _mk_table(hdr, rows)
        )

    # ── Exhausted ─────────────────────────────────────────────────────────────
    if exhausted:
        note = f" (top {len(exhausted)} of {total_ex})" if total_ex > len(exhausted) else ""
        hdr  = (f'<th {_TH}>Company</th><th {_TH}>Owner</th>'
                f'<th {_TH}>Total</th><th {_TH}>NOI</th>'
                f'<th {_TH}>Terminal</th><th {_TH}>Last Call</th>')
        rows = "".join(
            f'<tr>'
            f'<td {_TD}>{_tr(_name_of(c, tbl_cache), 38)}</td>'
            f'<td {_TD}>{_tr(_owner_of(c, tbl_cache), 20)}</td>'
            f'<td {_TDR}>{health[c]["total"]}</td>'
            f'<td {_TDR} style="color:#d32f2f;font-weight:bold;">{health[c]["noi"]}</td>'
            f'<td {_TDR}>{health[c]["terminal"]}</td>'
            f'<td {_TD}>{health[c]["last_called"] or "—"}</td>'
            f'</tr>'
            for c in exhausted
        )
        sections += (
            f'<p {_SEC}>🔴 Exhausted ({total_ex} accounts{note})</p>'
            f'<p style="font-size:12px;color:#666;margin:0 0 6px;">'
            f'≥2 Not Interested or all POCs terminal — needs fresh POC injection</p>'
            + _mk_table(hdr, rows)
        )

    # ── Near Exhausted ────────────────────────────────────────────────────────
    if near_ex:
        note = f" (top {len(near_ex)} of {total_ne})" if total_ne > len(near_ex) else ""
        hdr  = (f'<th {_TH}>Company</th><th {_TH}>Owner</th>'
                f'<th {_TH}>Total</th><th {_TH}>NOI</th>'
                f'<th {_TH}>Terminal</th><th {_TH}>YtBM</th><th {_TH}>Last Call</th>')
        rows = "".join(
            f'<tr>'
            f'<td {_TD}>{_tr(_name_of(c, tbl_cache), 38)}</td>'
            f'<td {_TD}>{_tr(_owner_of(c, tbl_cache), 20)}</td>'
            f'<td {_TDR}>{health[c]["total"]}</td>'
            f'<td {_TDR} style="color:#f57c00;font-weight:bold;">{health[c]["noi"]}</td>'
            f'<td {_TDR}>{health[c]["terminal"]}</td>'
            f'<td {_TDR}>{health[c]["ytbm"]}</td>'
            f'<td {_TD}>{health[c]["last_called"] or "—"}</td>'
            f'</tr>'
            for c in near_ex
        )
        sections += (
            f'<p {_SEC}>🟠 Near Exhausted ({total_ne} accounts{note})</p>'
            f'<p style="font-size:12px;color:#666;margin:0 0 6px;">'
            f'1 NOI or ≥70% terminal — monitor closely</p>'
            + _mk_table(hdr, rows)
        )

    # ── MQL – Action Needed ───────────────────────────────────────────────────
    if mql_action:
        note = f" (top {len(mql_action)} of {total_mql})" if total_mql > len(mql_action) else ""
        hdr  = (f'<th {_TH}>Company</th><th {_TH}>Owner</th>'
                f'<th {_TH}>MQL</th><th {_TH}>Hot</th>'
                f'<th {_TH}>Active</th><th {_TH}>Last Call</th>')
        rows = "".join(
            f'<tr>'
            f'<td {_TD}>{_tr(_name_of(c, tbl_cache), 38)}</td>'
            f'<td {_TD}>{_tr(_owner_of(c, tbl_cache), 20)}</td>'
            f'<td {_TDR} style="color:#7b1fa2;font-weight:bold;">{health[c]["mql"]}</td>'
            f'<td {_TDR}>{health[c]["hot"]}</td>'
            f'<td {_TDR}>{health[c]["active"]}</td>'
            f'<td {_TD}>{health[c]["last_called"] or "—"}</td>'
            f'</tr>'
            for c in mql_action
        )
        sections += (
            f'<p {_SEC}>🟣 MQL – Push to Discovery Call ({total_mql} accounts{note})</p>'
            f'<p style="font-size:12px;color:#666;margin:0 0 6px;">'
            f'Has MQL / Activation POCs — needs follow-up to book Discovery Call</p>'
            + _mk_table(hdr, rows)
        )

    if not (needs_re or exhausted or near_ex or mql_action):
        sections += '<p style="color:#555;">All accounts look healthy — no action needed.</p>'

    today = date.today()
    week_label = f"Week of {today.strftime('%b %d, %Y')}"

    return (
        f'<!DOCTYPE html><html><body {_BODY}>'
        '<p>Hi team,</p>'
        f'<p style="font-weight:bold;font-size:15px;margin:0 0 2px;">'
        f'Account Health — Weekly Digest &nbsp;&middot;&nbsp; {week_label}</p>'
        f'<p style="font-size:12px;color:#666;margin:0 0 4px;">'
        f'Re-assign = <b>Airtable filter: [YtBM POCs] &gt; 0 AND [Called Since Apr 19] = 0</b></p>'
        f'<p style="font-size:12px;color:#999;margin:0 0 16px;">'
        f'Exhausted = NOI ≥ 2 &nbsp;|&nbsp; '
        f'Near Exhausted = 1 NOI or ≥70% terminal &nbsp;|&nbsp; '
        f'MQL = connected, needs push to DCB</p>'
        + sections
        + '<p style="color:#999;font-size:11px;margin-top:24px;">— Kylas Sync</p>'
        '</body></html>'
    )


def _build_poc_email(first_name: str, accounts: list,
                     health: dict, tbl_cache: dict, week_label: str) -> str:
    """HTML email for one POC listing their unexhausted accounts."""
    _priority = {"Fresh": 0, "Active": 1, "Near Exhausted": 2,
                 "Exhausted": 3, "Hot Pipeline": 4, "MQL - Action Needed": 5}
    accounts = sorted(accounts, key=lambda c: (
        _priority.get(health[c]["status"], 9), -health[c]["ytbm"]
    ))

    hdr = (f'<th {_TH}>Company</th>'
           f'<th {_TH}>Status</th>'
           f'<th {_TH}>Total</th>'
           f'<th {_TH}>YtBM</th>'
           f'<th {_TH}>CNC</th>'
           f'<th {_TH}>NOI</th>'
           f'<th {_TH}>MQL/SQL</th>'
           f'<th {_TH}>Last Call</th>')

    def _td_n(val, color=""):
        style = f'text-align:right;padding:7px 12px;border:1px solid #ccc;font-size:12px;{color}'
        return f'<td style="{style}">{val}</td>'

    rows = "".join(
        '<tr>'
        f'<td {_TD}>{_tr(_name_of(c, tbl_cache), 38)}</td>'
        f'<td {_TD}>{_badge(health[c]["status"])}</td>'
        + _td_n(health[c]["total"])
        + _td_n(health[c]["ytbm"], "color:#888;")
        + _td_n(health[c]["cnc"])
        + _td_n(health[c]["noi"],  "color:#d32f2f;" if health[c]["noi"] else "")
        + _td_n(health[c]["mql"] + health[c]["hot"],
                "color:#7b1fa2;" if (health[c]["mql"] + health[c]["hot"]) else "")
        + f'<td {_TD}>{health[c]["last_called"] or "—"}</td>'
        '</tr>'
        for c in accounts
    )

    n = len(accounts)
    return (
        f'<!DOCTYPE html><html><body {_BODY}>'
        f'<p>Hi {first_name},</p>'
        f'<p style="font-weight:bold;font-size:15px;margin:0 0 4px;">'
        f'Exhaust Your Accounts &nbsp;&middot;&nbsp; {week_label}</p>'
        f'<p style="font-size:12px;color:#555;margin:0 0 2px;">'
        f'You have <b>{n} account{"s" if n != 1 else ""}</b> that '
        f'haven\'t been exhausted yet — please work on them this week.</p>'
        f'<p style="font-size:12px;color:#888;margin:0 0 16px;">'
        f'An account is exhausted when it has: MQL / SQL / DCB contacts, '
        f'<b>OR</b> 3+ CNC attempts, <b>OR</b> 3+ NOI rejections.</p>'
        + _mk_table(hdr, rows)
        + '<p style="color:#999;font-size:11px;margin-top:24px;">— Kylas Sync</p>'
        '</body></html>'
    )


def _send_poc_emails(health: dict, tbl_cache: dict, cfg: dict,
                     smtp_user: str, smtp_pass: str) -> None:
    """Group unexhausted accounts by owner and send each POC their personal email."""
    if not smtp_user or not smtp_pass:
        print("[Account Health] SMTP not configured — skipping POC emails")
        return

    today       = date.today()
    week_label  = f"Week of {today.strftime('%b %d, %Y')}"
    user_emails = cfg.get("kylas_user_emails", {})
    cc_list     = cfg.get("cc", [])

    by_owner: dict = {}
    for co_id, e in health.items():
        if not e.get("needs_exhaust"):
            continue
        owner = _owner_of(co_id, tbl_cache)
        if owner and owner != "—":
            by_owner.setdefault(owner, []).append(co_id)

    if not by_owner:
        print("[Account Health] No unexhausted accounts found — skipping POC emails")
        return

    for owner, accounts in sorted(by_owner.items()):
        email = user_emails.get(owner)
        if not email:
            for k, v in user_emails.items():
                if owner.lower() in k.lower() or k.lower() in owner.lower():
                    email = v
                    break

        if not email:
            print(f"[Account Health] No email for owner '{owner}' — skipping")
            continue

        first   = owner.split()[0]
        body    = _build_poc_email(first, accounts, health, tbl_cache, week_label)
        subject = f"Please Exhaust These Accounts — {week_label}"
        eff_cc  = [a for a in cc_list if a.lower() != email.lower()]

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
            print(f"[Account Health] POC email → {owner} <{email}> ({len(accounts)} accounts)")
        except Exception as exc:
            print(f"[Account Health] WARNING: POC email failed for {owner} — {exc}")


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

    print("[Account Health] Fetching all contacts from Kylas...")
    contacts = kylas._search_all(
        "contact",
        fields=["id", "company", "ownedBy", "updatedAt", "customFieldValues"],
    )
    print(f"[Account Health] {len(contacts)} contacts fetched")

    health = compute_health(contacts, user_email_map=user_email_map)

    counts = {s: sum(1 for e in health.values() if e["status"] == s)
              for s in ("Fresh", "Active", "MQL - Action Needed",
                        "Hot Pipeline", "Near Exhausted", "Exhausted")}
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

    tbl_crm_cache = {}
    try:
        tbl_crm = AirtableClient("Companies", base_id=crm_base)
        upd, skp = _write_table(tbl_crm, health, fm_crm)
        tbl_crm_cache = tbl_crm._cache
        print(f"[Account Health] Companies CRM → {upd} updated, {skp} no Kylas ID")
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
    needs_exhaust_ct = sum(1 for e in health.values() if e.get("needs_exhaust"))
    print(f"[Account Health] {needs_exhaust_ct} accounts need exhaust → sending per-POC emails...")
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
