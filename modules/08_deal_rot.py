"""
Deal Rotting Alert — flags deals that have stopped moving.

A deal is "rotting" when, for `idle_days` or more, it has had:
  - no pipeline stage change, AND
  - no new note / comment.

How activity is measured
────────────────────────
Kylas bumps a deal's `updatedAt` whenever its stage (or any field) changes,
so that is the primary activity clock. For deals that look idle by
`updatedAt`, we additionally pull the latest note from Kylas — if a comment
was added recently the deal is treated as alive. So:

    last_activity = max(Updated At, latest note createdAt)
    rotting       = open deal AND (today - last_activity) >= idle_days

Deals in a terminal stage (Won / Closed*) are never flagged.

Alert table:  Deal Name | Owner | Pipeline Stage | Idle (days) | Last Comment
Recipients:   team.json deal_rot.recipients  (+ each deal owner as CC)
Run:          modules/08_deal_rot.py   (daily — see deal_rot.yml)
"""
import argparse
import json
import os
import smtplib
import sys
from datetime import datetime, timezone, date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEAM_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "team.json")

# ── HTML styling ────────────────────────────────────────────────────────────
_TH  = ('style="background:#f2f2f2;text-align:left;padding:8px 14px;'
        'border:1px solid #cccccc;font-size:13px;"')
_THC = ('style="background:#f2f2f2;text-align:center;padding:8px 14px;'
        'border:1px solid #cccccc;font-size:13px;"')
_TD  = 'style="padding:8px 14px;border:1px solid #cccccc;font-size:13px;vertical-align:top;"'
_TDC = ('style="text-align:center;padding:8px 14px;border:1px solid #cccccc;'
        'font-size:13px;font-weight:bold;color:#b00020;vertical-align:top;"')
_TABLE = 'style="border-collapse:collapse;width:100%;margin:8px 0 20px;"'


def _load_cfg() -> dict:
    with open(TEAM_PATH) as fh:
        return json.load(fh)


def _friendly_date(d: date = None) -> str:
    d = d or date.today()
    return f"{d.strftime('%B')} {d.day}"


def _tr(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _parse_dt(raw: str):
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _is_terminal(stage: str, terminal: list) -> bool:
    s = (stage or "").strip().lower()
    if not s:
        return False
    if s in {t.lower() for t in terminal}:
        return True
    return s.startswith("closed") or s == "won"


def _read_deals() -> list:
    """All deals from the Airtable Deals table."""
    from utils.airtable_client import AirtableClient
    with open(os.path.join(os.path.dirname(os.path.dirname(__file__)),
                           "config", "field_map.json")) as fh:
        fm = json.load(fh)["deal"]
    rows = AirtableClient("Deals").table.all()
    out  = []
    for r in rows:
        f = r["fields"]
        out.append({
            "id":     str(f.get(fm["id"], "")).strip(),
            "name":   f.get(fm["name"], ""),
            "owner":  f.get(fm["assignedTo"], ""),
            "stage":  f.get(fm["pipelineStage"], ""),
            "updated": f.get(fm["updatedAt"], ""),
        })
    return out


def _find_rotten(deals: list, idle_days: int, terminal: list) -> list:
    """Return rotten deals enriched with idle_days + last comment, newest-idle first."""
    from utils.kylas_client import KylasClient
    now    = datetime.now(timezone.utc)
    kylas  = None
    rotten = []

    for d in deals:
        if _is_terminal(d["stage"], terminal):
            continue
        upd = _parse_dt(d["updated"])
        if upd is None:
            continue
        idle = (now - upd).days
        if idle < idle_days:
            continue   # moved recently — not a candidate

        # Candidate by updatedAt — confirm there's no recent note either.
        last_comment, note_dt = "", None
        if d["id"]:
            try:
                kylas = kylas or KylasClient()
                notes = kylas.get_deal_notes(d["id"])
                if notes:
                    last_comment = notes[0]["text"]
                    note_dt = _parse_dt(notes[0]["createdAt"])
            except Exception:
                pass

        last_activity = max([dt for dt in (upd, note_dt) if dt], default=upd)
        idle = (now - last_activity).days
        if idle < idle_days:
            continue   # recent note — deal is alive

        rotten.append({**d, "idle": idle, "last_comment": last_comment})

    rotten.sort(key=lambda x: x["idle"], reverse=True)
    return rotten


def _owner_emails(rotten: list, cfg: dict) -> list:
    name_to_email = cfg.get("kylas_user_emails", {})
    lookup = {k.lower(): v for k, v in name_to_email.items()}
    emails = []
    for d in rotten:
        owner = (d.get("owner") or "").strip()
        if not owner:
            continue
        addr = lookup.get(owner.lower())
        if not addr:   # partial match (e.g. "Bhaumik" vs "Bhaumik Sachdeva")
            for nm, em in lookup.items():
                if owner.lower() in nm or nm in owner.lower():
                    addr = em
                    break
        if addr and addr not in emails:
            emails.append(addr)
    return emails


def _build_body(rotten: list, friendly: str, idle_days: int) -> str:
    body_style = ('style="font-family:Arial,sans-serif;color:#333333;'
                  'max-width:720px;margin:0 auto;padding:24px 20px;"')

    if not rotten:
        return (
            f'<!DOCTYPE html><html><body {body_style}>'
            '<p>Hi team,</p>'
            f'<p style="font-weight:bold;font-size:14px;">Deal Rotting Alert &nbsp;&middot;&nbsp; {friendly}</p>'
            f'<p style="color:#2e7d32;">All open deals have moved within the last {idle_days} days. '
            'Nothing rotting. 🎉</p>'
            '<p style="color:#999;font-size:12px;margin-top:24px;">— Kylas Sync</p>'
            '</body></html>'
        )

    rows = "".join(
        f'<tr>'
        f'<td {_TD}>{_tr(d["name"], 48)}</td>'
        f'<td {_TD}>{_tr(d["owner"], 24)}</td>'
        f'<td {_TD}>{_tr(d["stage"], 28)}</td>'
        f'<td {_TDC}>{d["idle"]}</td>'
        f'<td {_TD}>{_tr(d["last_comment"], 70) or "—"}</td>'
        f'</tr>'
        for d in rotten
    )
    table = (
        f'<table {_TABLE}><thead><tr>'
        f'<th {_TH}>Deal Name</th><th {_TH}>Owner</th>'
        f'<th {_TH}>Pipeline Stage</th><th {_THC}>Idle (days)</th>'
        f'<th {_TH}>Last Comment</th>'
        f'</tr></thead><tbody>{rows}</tbody></table>'
    )
    return (
        f'<!DOCTYPE html><html><body {body_style}>'
        '<p>Hi team,</p>'
        f'<p style="font-weight:bold;font-size:14px;margin:0 0 4px;">'
        f'Deal Rotting Alert &nbsp;&middot;&nbsp; {friendly}</p>'
        f'<p style="font-size:13px;color:#666;margin:0 0 14px;">'
        f'These deals have had no stage change and no new comment for {idle_days}+ days.</p>'
        + table
        + f'<p style="font-size:13px;color:#555;">Total: {len(rotten)} rotting deal(s)</p>'
        '<p style="color:#999;font-size:12px;margin-top:20px;">— Kylas Sync</p>'
        '</body></html>'
    )


def run(to_override: list = None):
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    if not smtp_user or not smtp_pass:
        print("[Deal Rot] SMTP_USER / SMTP_PASS not set — skipping")
        return

    cfg = _load_cfg()
    dr  = cfg.get("deal_rot", {})
    if not dr.get("enabled", True):
        print("[Deal Rot] Disabled in config — skipping")
        return

    idle_days = int(dr.get("idle_days", 2))
    terminal  = dr.get("terminal_stages", [])

    try:
        deals = _read_deals()
        print(f"[Deal Rot] {len(deals)} deals read from Airtable")
    except Exception as exc:
        print(f"[Deal Rot] WARNING: could not read Deals table — {exc}")
        return

    rotten = _find_rotten(deals, idle_days, terminal)
    print(f"[Deal Rot] {len(rotten)} rotting (idle >= {idle_days}d)")

    to_list = to_override or dr.get("recipients", []) or cfg.get("cc", [])
    if not to_list:
        print("[Deal Rot] No recipients configured — skipping")
        return

    cc_list = []
    if not to_override and dr.get("cc_owner", True):
        cc_list = [e for e in _owner_emails(rotten, cfg) if e not in to_list]

    friendly = _friendly_date()
    body     = _build_body(rotten, friendly, idle_days)
    subject  = f"Deal Rotting Alert | {friendly} | {len(rotten)} deal(s)"

    msg            = MIMEMultipart("alternative")
    msg["From"]    = smtp_user
    msg["To"]      = ", ".join(to_list)
    msg["Subject"] = subject
    if cc_list:
        msg["CC"] = ", ".join(cc_list)
    msg.attach(MIMEText(body, "html", "utf-8"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as s:
            s.ehlo(); s.starttls()
            s.login(smtp_user, smtp_pass)
            s.sendmail(smtp_user, to_list + cc_list, msg.as_string())
        cc_s = f"  (cc: {', '.join(cc_list)})" if cc_list else ""
        print(f"[Deal Rot] Sent → {', '.join(to_list)}{cc_s}")
    except Exception as exc:
        print(f"[Deal Rot] WARNING: send failed — {exc}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--to", nargs="+", metavar="EMAIL",
                        help="Override recipients (default: team.json deal_rot.recipients)")
    args = parser.parse_args()
    from dotenv import load_dotenv; load_dotenv()
    run(to_override=args.to)
