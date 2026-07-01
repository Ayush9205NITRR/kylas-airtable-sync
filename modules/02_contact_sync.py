import argparse
import json
import os
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.kylas_client import KylasClient
from utils.airtable_client import AirtableClient
from utils.logger import SyncLogger
from utils.bd_metrics import BD_KEYS, contact_stage as _contact_stage, classify_bd as _classify_bd, company_info as _company_info
from utils.calendar_invite import send_invite as _send_invite

CUTOFF = datetime(2024, 6, 1, tzinfo=timezone.utc)

_FM = None

_PIPELINE_STAGE = {
    2862826: "Yet to Be Mined",
    2862827: "CNC (Could Not Connect) - 1",
    2862828: "MQL (Marketing Qualified Lead)",
    2862829: "Activation",
    2862831: "Not Interested",
    2864173: "Yet to Be Mined",
    2864175: "Invalid Contact",
    2867816: "CNC (Could Not Connect) - 2",
    2867817: "MQL (Marketing Qualified Lead)",
    2870484: "SQL (Sales Qualified Lead)",
    2870485: "Not a Decision Maker (NDM)",
    2873316: "Follow-up (1)",
    2873317: "Follow-up (2)",
    2873318: "Follow-up (3)",
    2873321: "POC - Organisation - Changed",
    2873487: "Followup - CNC",
    2909379: "Discovery Call Booked",
    2909380: "Reschedule Pending",
    2909381: "Closing Loops - Low Value",
    2909382: "Discovery Call No-Show",
    2909383: "Offsite Delayed",
    2910918: "Discovery Call Done - Awaiting Client Inputs",
}


def _fm():
    global _FM
    if _FM is None:
        p = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "field_map.json")
        with open(p) as f:
            _FM = json.load(f)["contact"]
    return _FM


def _clean(d):
    return {k: v for k, v in d.items() if v is not None and (v != "" if isinstance(v, str) else True)}


def _parse_call_date(raw: str) -> str:
    """Parse a Kylas date string to ISO YYYY-MM-DD. Returns '' on failure."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if raw[0].isdigit():
        return raw[:10]
    try:
        return datetime.strptime(raw.split(" at ")[0].strip(), "%b %d, %Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _owner_name(raw: dict, user_map: dict = None) -> str:
    ob = raw.get("ownedBy")
    if isinstance(ob, dict):
        name = ob.get("name") or f"{ob.get('firstName', '')} {ob.get('lastName', '')}".strip()
        if name:
            return name
    oid = raw.get("ownerId")
    if oid and user_map:
        name = user_map.get(int(oid)) or user_map.get(str(oid))
        if name:
            return name
    return "Unassigned"


def _map(raw: dict, user_map: dict = None, company_id_map: dict = None) -> dict:
    fm     = _fm()
    emails = raw.get("emails") or []
    phones = raw.get("phoneNumbers") or []
    cf     = raw.get("customFieldValues") or {}

    psd = cf.get("cfPipelineStageBd")
    if isinstance(psd, dict):
        pipeline_stage = psd.get("name", "")
    elif psd is not None:
        pipeline_stage = _PIPELINE_STAGE.get(int(psd), str(psd))
    else:
        pipeline_stage = ""

    src    = raw.get("source")
    source = src.get("name", "") if isinstance(src, dict) else (str(src) if src else "")

    co         = raw.get("company")
    company_id = str(co) if isinstance(co, (int, float)) else (
        str(co.get("id", "")) if isinstance(co, dict) else ""
    )

    fields = _clean({
        fm["id"]:            str(raw["id"]),
        fm["fullName"]:      raw.get("name") or "",
        fm["email"]:         emails[0].get("value", "") if emails else "",
        fm["phone"]:         phones[0].get("value", "") if phones else "",
        fm["assignedTo"]:    _owner_name(raw, user_map),
        fm["designation"]:   raw.get("designation") or "",
        fm["companyId"]:     company_id,
        fm["linkedin"]:      raw.get("linkedin") or "",
        fm["city"]:          raw.get("city") or "",
        fm["state"]:         raw.get("state") or "",
        fm["country"]:       raw.get("country") or "",
        fm["source"]:        source,
        fm["pipelineStage"]: pipeline_stage,
        fm["remarks"]:       cf.get("cfRemarks") or "",
        fm["createdAt"]:     raw.get("createdAt") or "",
    })

    # "Updated At" stores the NAME of the human who last updated this contact.
    # Only counts when the contact OWNER made the update — admin, system, and
    # other-user updates are excluded so metrics reflect genuine owner work.
    ob_id = (raw.get("ownedBy") or {}).get("id")
    ub    = raw.get("updatedBy") or {}
    ub_id = ub.get("id") if isinstance(ub, dict) else None
    if ob_id and ub_id and ob_id == ub_id:
        ub_name  = (ub.get("name") or
                    f"{ub.get('firstName','')} {ub.get('lastName','')}".strip())
        is_human = True
    else:
        ub_name  = ""
        is_human = False

    if is_human and ub_name and fm.get("updatedAt"):
        fields[fm["updatedAt"]] = ub_name

    lc_date = _parse_call_date(cf.get("cfLastCalledAt"))
    if lc_date and fm.get("lastCalledAt"):
        fields[fm["lastCalledAt"]] = lc_date

    nc_date = _parse_call_date(cf.get("cfNextCallDate"))
    if nc_date and fm.get("nextCallDate"):
        fields[fm["nextCallDate"]] = nc_date

    # Link to Companies table if Airtable record ID is available
    if company_id_map and company_id:
        airtable_id = company_id_map.get(company_id)
        if airtable_id:
            fields[fm["companyLink"]] = [airtable_id]

    return fields


def run(test_mode: bool = False, test_id: int = None,
        logger: SyncLogger = None, user_map: dict = None,
        company_id_map: dict = None, since: str = None) -> dict:
    kylas    = KylasClient()
    airtable = AirtableClient("Contacts")
    if logger is None:
        logger = SyncLogger()

    log_id = logger.start("Contacts")
    created = updated = failed = pre_cutoff = cal_sent = 0
    per_user       = {}
    bd_daily       = {}
    account_activity = {}
    today_iso      = date.today().isoformat()
    _dbg = {"upd_today": 0, "has_stage": 0, "owner_update": 0, "called_today": 0, "all3": 0}
    _dbg_samples = []

    try:
        cached = airtable.build_cache("Kylas Contact Id")
        print(f"[Contacts] Cache loaded: {cached} existing")

        # Build owner name → email map for calendar invites
        user_email_map: dict = {}
        try:
            _tp = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "team.json")
            with open(_tp) as _tf:
                user_email_map = json.load(_tf).get("kylas_user_emails", {})
        except Exception:
            pass
        try:
            user_email_map.update(kylas.get_user_emails())
        except Exception:
            pass

        if test_mode and test_id:
            contacts = [kylas.get_contact(test_id)]
        else:
            contacts = kylas.get_contacts(since=since)
            if test_mode:
                contacts = contacts[:5]
        print(f"[Contacts] Fetched {len(contacts)} from Kylas")

        for ct in contacts:
            try:
                created_str = ct.get("createdAt", "")
                if created_str:
                    dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                    if dt < CUTOFF:
                        pre_cutoff += 1
                        continue

                owner    = _owner_name(ct, user_map)
                kylas_id = str(ct["id"])

                # Capture old values before upsert for change detection
                _existing  = airtable._cache.get(kylas_id)
                nc_field   = _fm().get("nextCallDate", "")
                old_nc     = str(_existing["fields"].get(nc_field, "") if _existing else "").strip() if _existing else ""
                new_stage  = _contact_stage(ct)

                mapped_fields = _map(ct, user_map=user_map, company_id_map=company_id_map)
                new_nc        = str(mapped_fields.get(nc_field, "")).strip() if nc_field else ""

                action, _ = airtable.upsert(
                    "Kylas Contact Id", kylas_id,
                    mapped_fields,
                    ct.get("updatedAt", ""),
                    updated_at_field="",   # "Updated At" now stores name, not timestamp
                )
                if action == "created":
                    created += 1
                    per_user.setdefault(owner, {"created": 0, "updated": 0})["created"] += 1
                elif action == "updated":
                    updated += 1
                    per_user.setdefault(owner, {"created": 0, "updated": 0})["updated"] += 1

                # BD metrics: count a contact when the OWNER worked it TODAY.
                #   1. "Last Called At" date == today  → the proof the owner
                #      changed/worked this contact today (this is our signal).
                #   2. The last update to the record was made by the contact's
                #      owner (exclude admin/system edits).
                # We deliberately do NOT require the pipeline stage to differ
                # from yesterday — a same-stage call (e.g. CNC again) still
                # counts, matching the Kylas "BD - Daily Report".
                _ob_id = (ct.get("ownedBy") or {}).get("id")
                _ub    = ct.get("updatedBy") or {}
                _ub_id = _ub.get("id") if isinstance(_ub, dict) else None
                _cf    = ct.get("customFieldValues") or {}
                owner_update = bool(_ob_id and _ub_id and _ob_id == _ub_id)
                called_today = (_parse_call_date(_cf.get("cfLastCalledAt")) == today_iso)

                if os.environ.get("BD_DEBUG") and (ct.get("updatedAt") or "").startswith(today_iso):
                    _dbg["upd_today"]    += 1
                    _dbg["has_stage"]    += 1 if new_stage else 0
                    _dbg["owner_update"] += 1 if owner_update else 0
                    _dbg["called_today"] += 1 if called_today else 0
                    if new_stage and owner_update and called_today:
                        _dbg["all3"] += 1
                    if len(_dbg_samples) < 8:
                        _dbg_samples.append(
                            f"id={ct.get('id')} owner={owner!r} ob={_ob_id} ub={_ub_id} "
                            f"lastcalled={_cf.get('cfLastCalledAt')!r} "
                            f"parsed={_parse_call_date(_cf.get('cfLastCalledAt'))!r} "
                            f"stage={new_stage!r}"
                        )

                if bool(new_stage) and owner_update and called_today:
                    cats = _classify_bd(new_stage)
                    bd   = bd_daily.setdefault(owner, {k: 0 for k in BD_KEYS})
                    for key in BD_KEYS:
                        if cats[key]:
                            bd[key] += 1

                    co_id, co_name = _company_info(ct)
                    if co_id:
                        acc = account_activity.setdefault(co_id, {
                            "company_name": co_name, "pocs": 0,
                            "owners": set(),
                            **{k: 0 for k in BD_KEYS},
                        })
                        if not acc["company_name"] and co_name:
                            acc["company_name"] = co_name
                        acc["owners"].add(owner)
                        acc["pocs"] += 1
                        for key in BD_KEYS:
                            if cats[key]:
                                acc[key] += 1

                # Calendar invite: only for incremental syncs and future dates
                if nc_field and new_nc and new_nc != old_nc and since is not None:
                    try:
                        call_date = date.fromisoformat(new_nc)
                        if call_date >= date.today():
                            co_raw  = ct.get("company")
                            co_name = co_raw.get("name", "") if isinstance(co_raw, dict) else ""
                            cf_ct   = ct.get("customFieldValues") or {}
                            em_list = ct.get("emails") or []
                            ph_list = ct.get("phoneNumbers") or []
                            ob      = ct.get("ownedBy") or {}
                            owner_em = ""
                            if isinstance(ob, dict):
                                owner_em = (ob.get("email") or ob.get("emailId") or "").strip().lower()
                            if not owner_em:
                                owner_em = user_email_map.get(owner, "")
                            ok = _send_invite(
                                contact_id=kylas_id,
                                contact_name=ct.get("name") or "",
                                contact_email=em_list[0].get("value", "") if em_list else "",
                                contact_phone=ph_list[0].get("value", "") if ph_list else "",
                                company_name=co_name,
                                remarks=cf_ct.get("cfRemarks") or "",
                                call_date=call_date,
                                owner_email=owner_em,
                            )
                            if ok:
                                cal_sent += 1
                    except Exception as exc:
                        print(f"  [CalendarInvite] FAILED for contact {kylas_id}: {exc}")

            except Exception as e:
                failed += 1
                print(f"  [FAILED  ] Contact {ct.get('id')}: {e}")

        print(f"[Contacts] Flushing {created} creates + {updated} updates to Airtable...")
        airtable.flush()

        logger.finish(log_id, created, updated, failed)
        print(f"[Contacts] Done -> created={created} updated={updated} pre-cutoff={pre_cutoff} failed={failed} cal_invites={cal_sent}")

        total_bd = sum(v for m in bd_daily.values() for v in m.values())
        print(f"[Contacts] BD daily: {total_bd} stage transitions across {len(bd_daily)} owner(s)")
        print(f"[Contacts] Account activity: {len(account_activity)} companies with stage moves today")

        if os.environ.get("BD_DEBUG"):
            print(f"[BD DEBUG] today={today_iso}  of contacts updated today: "
                  f"upd_today={_dbg['upd_today']}  has_stage={_dbg['has_stage']}  "
                  f"owner_update={_dbg['owner_update']}  called_today={_dbg['called_today']}  "
                  f"all3={_dbg['all3']}")
            for s in _dbg_samples:
                print(f"[BD DEBUG] {s}")

    except Exception as e:
        logger.fail(log_id, str(e))
        raise

    return {"created": created, "updated": updated, "failed": failed,
            "per_user": per_user, "bd_daily": bd_daily, "account_activity": account_activity,
            "cal_invites": cal_sent}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--id", type=int, dest="contact_id")
    args = parser.parse_args()
    from dotenv import load_dotenv; load_dotenv()
    _user_map = {}
    try:
        _user_map = KylasClient().get_users()
    except Exception:
        pass
    run(test_mode=args.test, test_id=args.contact_id, user_map=_user_map)
