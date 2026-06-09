"""
Account Health — per-company POC health stats + re-assignment alerts.

For each Kylas company, reads ALL associated contacts and computes:

  Total POCs         — all contacts linked to this company
  YtBM POCs          — Yet to Be Mined (stage blank / YtBM — never called)
  Active POCs        — CNC / Follow-up (being worked)
  Connected POCs     — MQL / SQL / DCB / Activation (warm leads)
  Terminal POCs      — Not Interested / Invalid Contact / NDM (no more calls)
  Last Called At     — latest cfLastCalledAt across all contacts (ISO date)
  Called Since Apr 19 — contacts with cfLastCalledAt >= 2026-04-19
  Account Status     — Fresh / Active / Near Exhausted / Exhausted
  Needs Re-assign    — True when YtBM POCs exist but nobody called since Apr 19

Writes to BOTH Airtable tables:
  Company List   (AIRTABLE_COMPANY_BASE_ID)
  Companies CRM  (AIRTABLE_BASE_ID)

Runs on full_day slot only; can also be run standalone.
Sends an email alert (to cc list) summarising accounts that need attention.
"""
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

_HOT_STAGES = {
    "MQL (Marketing Qualified Lead)",
    "SQL (Sales Qualified Lead)",
    "Activation",
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


def compute_health(contacts: list) -> dict:
    """
    contacts: raw Kylas contact dicts (full fields including customFieldValues).
    Returns {kylas_company_id (str): health_dict}.
    """
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
            "total": 0, "ytbm": 0, "active": 0, "connected": 0, "terminal": 0,
            "called": 0, "called_apr19": 0, "last_called": "",
        })
        e["total"] += 1

        if not stage or stage == "Yet to Be Mined":
            e["ytbm"] += 1
        elif stage in _TERMINAL_STAGES:
            e["terminal"] += 1
        elif stage in _ACTIVE_STAGES:
            e["active"] += 1
        elif stage in _HOT_STAGES:
            e["connected"] += 1
        # else: unknown / unmapped stage → counted in total but no bucket

        if lc:
            e["called"] += 1
            if lc >= REASSIGN_CUTOFF:
                e["called_apr19"] += 1
            if lc > e["last_called"]:
                e["last_called"] = lc

    for e in by_co.values():
        t = e["total"]
        if t == 0 or e["called"] == 0:
            e["status"] = "Fresh"
        elif e["terminal"] >= t:
            e["status"] = "Exhausted"
        elif e["terminal"] / t >= 0.7:
            e["status"] = "Near Exhausted"
        else:
            e["status"] = "Active"

        # Needs re-assign: has untouched POCs (YtBM) but nobody called since Apr 19
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

    for co_id, e in health.items():
        if co_id not in tbl._cache:
            skipped += 1
            continue

        fields = {}
        for key, at_field in [
            ("totalPocs",            fm.get("totalPocs")),
            ("ytbmPocs",             fm.get("ytbmPocs")),
            ("activePocs",           fm.get("activePocs")),
            ("connectedPocs",        fm.get("connectedPocs")),
            ("terminalPocs",         fm.get("terminalPocs")),
            ("calledSinceApr19",     fm.get("calledSinceApr19")),
            ("accountStatus",        fm.get("accountStatus")),
        ]:
            if at_field:
                stat_key = {
                    "totalPocs":         "total",
                    "ytbmPocs":          "ytbm",
                    "activePocs":        "active",
                    "connectedPocs":     "connected",
                    "terminalPocs":      "terminal",
                    "calledSinceApr19":  "called_apr19",
                    "accountStatus":     "status",
                }.get(key, key)
                fields[at_field] = e[stat_key]

        if fm.get("lastCalledAtContacts") and e["last_called"]:
            fields[fm["lastCalledAtContacts"]] = e["last_called"]
        if fm.get("needsReassign"):
            fields[fm["needsReassign"]] = e["needs_reassign"]

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
          'max-width:720px;margin:0 auto;padding:24px 20px;"')


def _tr(s, n=35):
    s = (s or "").strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def _owner_of(co_id: str, tbl_cache: dict) -> str:
    rec = tbl_cache.get(co_id)
    if not rec:
        return "—"
    f = rec.get("fields", {})
    return (f.get("Owner") or f.get("Owner - Kylas") or "—").strip()


def _name_of(co_id: str, tbl_cache: dict) -> str:
    rec = tbl_cache.get(co_id)
    if not rec:
        return co_id
    f = rec.get("fields", {})
    return (f.get("Company Name") or f.get("Company Name - Kylas") or co_id).strip()


def _build_email(health: dict, tbl_cache: dict, friendly: str) -> str:
    needs_re  = sorted(
        [co for co, e in health.items() if e["needs_reassign"]],
        key=lambda c: health[c]["total"], reverse=True
    )[:20]
    exhausted = sorted(
        [co for co, e in health.items() if e["status"] == "Exhausted"],
        key=lambda c: health[c]["total"], reverse=True
    )[:15]
    near_ex   = sorted(
        [co for co, e in health.items() if e["status"] == "Near Exhausted"],
        key=lambda c: health[c]["terminal"] / max(health[c]["total"], 1), reverse=True
    )[:15]

    total_re = sum(1 for e in health.values() if e["needs_reassign"])
    total_ex = sum(1 for e in health.values() if e["status"] == "Exhausted")
    total_ne = sum(1 for e in health.values() if e["status"] == "Near Exhausted")

    def section_table(ids, cols_fn):
        rows = "".join(cols_fn(co) for co in ids)
        return rows

    # Re-assign table
    re_rows = section_table(needs_re, lambda co: (
        f'<tr>'
        f'<td {_TD}>{_tr(_name_of(co, tbl_cache), 38)}</td>'
        f'<td {_TD}>{_tr(_owner_of(co, tbl_cache), 20)}</td>'
        f'<td {_TDR}>{health[co]["total"]}</td>'
        f'<td {_TDR}>{health[co]["ytbm"]}</td>'
        f'<td {_TDR}>{health[co]["active"]}</td>'
        f'<td {_TD}>{health[co]["last_called"] or "—"}</td>'
        f'</tr>'
    ))

    # Exhausted table
    ex_rows = section_table(exhausted, lambda co: (
        f'<tr>'
        f'<td {_TD}>{_tr(_name_of(co, tbl_cache), 38)}</td>'
        f'<td {_TD}>{_tr(_owner_of(co, tbl_cache), 20)}</td>'
        f'<td {_TDR}>{health[co]["total"]}</td>'
        f'<td {_TDR}>{health[co]["terminal"]}</td>'
        f'<td {_TD}>{health[co]["last_called"] or "—"}</td>'
        f'</tr>'
    ))

    # Near-exhausted table
    ne_rows = section_table(near_ex, lambda co: (
        f'<tr>'
        f'<td {_TD}>{_tr(_name_of(co, tbl_cache), 38)}</td>'
        f'<td {_TD}>{_tr(_owner_of(co, tbl_cache), 20)}</td>'
        f'<td {_TDR}>{health[co]["total"]}</td>'
        f'<td {_TDR}>{health[co]["terminal"]}</td>'
        f'<td {_TDR}>{health[co]["ytbm"]}</td>'
        f'<td {_TD}>{health[co]["last_called"] or "—"}</td>'
        f'</tr>'
    ))

    def mk_table(header_html, rows):
        return (f'<table {_TABLE}><thead><tr>{header_html}</tr></thead>'
                f'<tbody>{rows}</tbody></table>')

    re_hdr = (f'<th {_TH}>Company</th><th {_TH}>Owner</th>'
              f'<th {_TH}>Total</th><th {_TH}>YtBM</th>'
              f'<th {_TH}>Active</th><th {_TH}>Last Call</th>')
    ex_hdr = (f'<th {_TH}>Company</th><th {_TH}>Owner</th>'
              f'<th {_TH}>Total</th><th {_TH}>Terminal</th>'
              f'<th {_TH}>Last Call</th>')
    ne_hdr = (f'<th {_TH}>Company</th><th {_TH}>Owner</th>'
              f'<th {_TH}>Total</th><th {_TH}>Terminal</th>'
              f'<th {_TH}>YtBM</th><th {_TH}>Last Call</th>')

    sections = ""

    if needs_re:
        note = f" (showing top {len(needs_re)} of {total_re})" if total_re > len(needs_re) else ""
        sections += (
            f'<p {_SEC}>Needs Re-assign ({total_re} accounts{note})</p>'
            f'<p style="font-size:12px;color:#666;margin:0 0 6px;">'
            f'Has YtBM POCs but no call since April 19</p>'
            + mk_table(re_hdr, re_rows)
        )

    if exhausted:
        note = f" (showing top {len(exhausted)} of {total_ex})" if total_ex > len(exhausted) else ""
        sections += (
            f'<p {_SEC}>Exhausted ({total_ex} accounts{note})</p>'
            f'<p style="font-size:12px;color:#666;margin:0 0 6px;">'
            f'All POCs in terminal stages — needs fresh POC injection</p>'
            + mk_table(ex_hdr, ex_rows)
        )

    if near_ex:
        note = f" (showing top {len(near_ex)} of {total_ne})" if total_ne > len(near_ex) else ""
        sections += (
            f'<p {_SEC}>Near Exhausted ({total_ne} accounts{note})</p>'
            f'<p style="font-size:12px;color:#666;margin:0 0 6px;">'
            f'70%+ POCs in terminal stages</p>'
            + mk_table(ne_hdr, ne_rows)
        )

    if not sections:
        sections = '<p style="color:#555;">All accounts look healthy — no action needed.</p>'

    return (
        f'<!DOCTYPE html><html><body {_BODY}>'
        '<p>Hi team,</p>'
        f'<p style="font-weight:bold;font-size:14px;margin:0 0 4px;">'
        f'Account Health Snapshot &nbsp;&middot;&nbsp; {friendly}</p>'
        f'<p style="font-size:13px;color:#666;margin:0 0 16px;">'
        f'Accounts that need attention based on POC pipeline status.</p>'
        + sections
        + '<p style="color:#999;font-size:12px;margin-top:24px;">— Kylas Sync</p>'
        '</body></html>'
    )


def run(kylas=None) -> dict:
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

    # Full contact fetch (no since filter) for accurate account-wide stats
    print("[Account Health] Fetching all contacts from Kylas...")
    contacts = kylas._search_all(
        "contact",
        fields=["id", "company", "ownedBy", "updatedAt", "customFieldValues"],
    )
    print(f"[Account Health] {len(contacts)} contacts fetched")

    health = compute_health(contacts)
    exhausted = sum(1 for e in health.values() if e["status"] == "Exhausted")
    near_ex   = sum(1 for e in health.values() if e["status"] == "Near Exhausted")
    needs_re  = sum(1 for e in health.values() if e["needs_reassign"])
    fresh     = sum(1 for e in health.values() if e["status"] == "Fresh")
    active    = sum(1 for e in health.values() if e["status"] == "Active")
    print(f"[Account Health] {len(health)} companies  |  "
          f"Fresh={fresh}  Active={active}  "
          f"Near Exhausted={near_ex}  Exhausted={exhausted}  "
          f"Needs Re-assign={needs_re}")

    company_base = os.environ.get("AIRTABLE_COMPANY_BASE_ID") or os.environ["AIRTABLE_BASE_ID"]
    crm_base     = os.environ["AIRTABLE_BASE_ID"]

    # Write to Company List (company database)
    tbl_list_cache = {}
    try:
        tbl_list = AirtableClient("Company List", base_id=company_base)
        upd, skp = _write_table(tbl_list, health, fm_list)
        tbl_list_cache = tbl_list._cache
        print(f"[Account Health] Company List → {upd} updated, {skp} no Kylas ID")
    except Exception as exc:
        print(f"[Account Health] WARNING: Company List write failed — {exc}")

    # Write to Companies CRM
    tbl_crm_cache = {}
    try:
        tbl_crm = AirtableClient("Companies", base_id=crm_base)
        upd, skp = _write_table(tbl_crm, health, fm_crm)
        tbl_crm_cache = tbl_crm._cache
        print(f"[Account Health] Companies CRM → {upd} updated, {skp} no Kylas ID")
    except Exception as exc:
        print(f"[Account Health] WARNING: Companies CRM write failed — {exc}")

    # Send alert email
    if not smtp_user or not smtp_pass:
        print("[Account Health] SMTP not configured — skipping email")
        return health
    if not recipients:
        print("[Account Health] No recipients in team.json cc — skipping email")
        return health

    friendly   = f"{date.today().strftime('%B')} {date.today().day}"
    tbl_cache  = tbl_crm_cache or tbl_list_cache
    body       = _build_email(health, tbl_cache, friendly)
    subject    = f"Account Health | {friendly}"

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
        print(f"[Account Health] Alert sent → {', '.join(recipients)}")
    except Exception as exc:
        print(f"[Account Health] WARNING: email send failed — {exc}")

    return health


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    run()
