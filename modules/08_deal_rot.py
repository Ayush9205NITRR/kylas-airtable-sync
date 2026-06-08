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

Stages listed in deal_rot.terminal_stages are skipped (e.g. Discovery Call,
Event Executed, Closed - Offsite Delayed, Won, Closed Unqualified, Closed
Lost); every other / middle stage is checked for rotting.

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


def _read_deals(kylas) -> list:
    """
    Read all deals directly from Kylas (full fields) so we can inspect
    customFieldValues for any embedded note/remark data.
    """
    _FIELDS = [
        "id", "name", "pipelineStage", "ownedBy", "ownerId",
        "createdAt", "updatedAt", "latestActivityCreatedAt", "customFieldValues",
    ]
    raw_deals = kylas._search_all("deal", fields=_FIELDS)

    # Log available custom field keys from the first deal — helps identify
    # if Kylas stores notes/remarks in a custom field.
    if raw_deals:
        cf_sample = raw_deals[0].get("customFieldValues") or {}
        if cf_sample:
            print(f"[Deal Rot] Deal custom field keys: {list(cf_sample.keys())}")

    out = []
    for r in raw_deals:
        cf    = r.get("customFieldValues") or {}
        owner = r.get("ownedBy") or {}
        if isinstance(owner, dict):
            owner_name = (owner.get("name") or
                          f"{owner.get('firstName', '')} {owner.get('lastName', '')}".strip()
                          or "Unassigned")
            owner_id = owner.get("id") or r.get("ownerId")
        else:
            owner_name = str(owner) if owner else "Unassigned"
            owner_id = r.get("ownerId")

        stage_raw = r.get("pipelineStage") or {}
        stage = stage_raw.get("name", "") if isinstance(stage_raw, dict) else str(stage_raw)

        # Look for an embedded last-note in any custom field whose key contains
        # "note", "remark", or "comment" (case-insensitive).
        last_cf_note = ""
        for k, v in cf.items():
            kl = k.lower()
            if any(x in kl for x in ("note", "remark", "comment")):
                val = v.get("name", "") if isinstance(v, dict) else str(v or "")
                if val.strip():
                    last_cf_note = val.strip()
                    break

        latest_activity = r.get("latestActivityCreatedAt") or ""

        # Notes API returns "Entity Definition does not exist" for this tenant —
        # notes are not queryable. Use latestActivityCreatedAt as the best
        # available proxy for "something happened on this deal."
        if last_cf_note:
            last_comment = last_cf_note
        elif latest_activity:
            la_dt = _parse_dt(latest_activity)
            last_comment = f"Activity: {la_dt.strftime('%b')} {la_dt.day}" if la_dt else "—"
        else:
            last_comment = "—"

        out.append({
            "id":              str(r.get("id", "")).strip(),
            "name":            r.get("name", ""),
            "owner":           owner_name,
            "owner_id":        owner_id,
            "stage":           stage,
            "updated":         r.get("updatedAt", ""),
            "latest_activity": latest_activity,
            "last_comment":    last_comment,
        })
    return out


def _find_rotten(deals: list, idle_days: int, terminal: list) -> list:
    """Return rotten deals sorted by idle days descending."""
    now    = datetime.now(timezone.utc)
    rotten = []
    for d in deals:
        if _is_terminal(d["stage"], terminal):
            continue
        upd = _parse_dt(d["updated"])
        lat = _parse_dt(d.get("latest_activity", ""))
        last_ts = max((x for x in [upd, lat] if x is not None), default=None)
        if last_ts is None:
            continue
        idle = (now - last_ts).days
        if idle < idle_days:
            continue
        rotten.append({**d, "idle": idle})
    rotten.sort(key=lambda x: x["idle"], reverse=True)
    return rotten


def _owner_emails(rotten: list, cfg: dict, kylas=None) -> list:
    """
    Resolve EVERY rotting-deal owner to an email address.

    Resolution order per owner:
      1. team.json kylas_user_emails by name — manual overrides / corrections
         (e.g. Vipul Bansal -> vipul.bansal@enout.in).
      2. Kylas GET /users/{ownerId} — authoritative, covers every user in the
         tenant (deals carry a numeric ownerId, so this never misses an owner
         the way the paginated team-members list did).
      3. Fuzzy name match against the team.json overrides as a last resort.

    Returns a de-duplicated, order-preserving list of lowercased emails.
    """
    overrides = {k.lower(): v.strip().lower()
                 for k, v in cfg.get("kylas_user_emails", {}).items()}

    emails, by_id, unresolved = [], {}, []

    def _add(addr):
        addr = (addr or "").strip().lower()
        if addr and addr not in emails:
            emails.append(addr)

    for d in rotten:
        owner = (d.get("owner") or "").strip()
        oid   = d.get("owner_id")

        # 1. manual override by exact name
        addr = overrides.get(owner.lower()) if owner else ""

        # 2. authoritative lookup by user id (cached per id)
        if not addr and oid and kylas:
            if oid not in by_id:
                by_id[oid] = kylas.get_user_email(oid)
            addr = by_id[oid]

        # 3. fuzzy name fallback (e.g. "Vipul" vs "Vipul Bansal")
        if not addr and owner:
            for nm, em in overrides.items():
                if owner.lower() in nm or nm in owner.lower():
                    addr = em
                    break

        if addr:
            _add(addr)
        elif owner and owner.lower() != "unassigned":
            unresolved.append(owner)

    print(f"[Deal Rot] Owner emails resolved: {len(emails)} "
          f"(via {len(by_id)} Kylas user lookups)")
    if unresolved:
        print(f"[Deal Rot] WARNING: could not resolve email for: "
              f"{', '.join(sorted(set(unresolved)))}")
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


def run(to_override: list = None, dry_run: bool = False):
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    if (not smtp_user or not smtp_pass) and not dry_run:
        print("[Deal Rot] SMTP_USER / SMTP_PASS not set — skipping")
        return

    cfg = _load_cfg()
    dr  = cfg.get("deal_rot", {})
    if not dr.get("enabled", True):
        print("[Deal Rot] Disabled in config — skipping")
        return

    idle_days = int(dr.get("idle_days", 2))
    terminal  = dr.get("terminal_stages", [])

    from utils.kylas_client import KylasClient
    kylas = KylasClient()

    try:
        deals = _read_deals(kylas)
        print(f"[Deal Rot] {len(deals)} deals read from Kylas")
    except Exception as exc:
        print(f"[Deal Rot] WARNING: could not read deals — {exc}")
        return

    rotten = _find_rotten(deals, idle_days, terminal)
    print(f"[Deal Rot] {len(rotten)} rotting (idle >= {idle_days}d)")

    to_list = to_override or dr.get("recipients", []) or cfg.get("cc", [])
    if not to_list:
        print("[Deal Rot] No recipients configured — skipping")
        return

    cc_list = []
    if not to_override and dr.get("cc_owner", True):
        cc_list = [e for e in _owner_emails(rotten, cfg, kylas=kylas) if e not in to_list]

    if dry_run:
        print(f"[Deal Rot] DRY RUN — no email sent")
        print(f"[Deal Rot]   To: {', '.join(to_list)}")
        print(f"[Deal Rot]   CC ({len(cc_list)} owners): {', '.join(cc_list) or '(none)'}")
        return

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
    parser.add_argument("--dry-run", action="store_true",
                        help="Resolve recipients + owners and print them, but do not send the email")
    args = parser.parse_args()
    from dotenv import load_dotenv; load_dotenv()
    run(to_override=args.to, dry_run=args.dry_run)
