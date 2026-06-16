import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.kylas_client import KylasClient
from utils.airtable_client import AirtableClient
from utils.logger import SyncLogger

_FM = None
_FM_CRM = None


def _fm():
    global _FM
    if _FM is None:
        p = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "field_map.json")
        with open(p) as f:
            _FM = json.load(f)["company"]
    return _FM


def _fm_crm():
    global _FM_CRM
    if _FM_CRM is None:
        p = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "field_map.json")
        with open(p) as f:
            _FM_CRM = json.load(f)["company_crm"]
    return _FM_CRM


def _clean(d):
    return {k: v for k, v in d.items() if v is not None and (v != "" if isinstance(v, str) else True)}


def _assigned_name(raw: dict) -> str:
    a = raw.get("ownedBy") or raw.get("assignedTo") or {}
    if isinstance(a, dict):
        return a.get("name") or a.get("firstName") or "Unassigned"
    return str(a) if a else "Unassigned"


def _build_fields(raw: dict, fm: dict, user_email_map: dict = None) -> dict:
    industry   = raw.get("industry") or {}
    cf         = raw.get("customFieldValues") or {}
    owner_name = _assigned_name(raw)

    psd = cf.get("cfPipelineStageBd")
    if isinstance(psd, dict):
        stage_bd = psd.get("name", "")
    elif psd is not None:
        stage_bd = str(psd)
    else:
        stage_bd = ""

    fields = _clean({
        fm["id"]:              str(raw["id"]),
        fm["name"]:            raw.get("name", ""),
        fm["industry"]:        industry.get("name", "") if isinstance(industry, dict) else str(industry),
        fm["assignedTo"]:      owner_name,
        fm["updatedAt"]:       raw.get("updatedAt", ""),
        fm["batch"]:           cf.get("cfBatch") or "",
        fm["pipelineStageBd"]: stage_bd,
        fm["sourceOfData"]:    cf.get("cfSourceOfData") or "",
    })
    if "ownerEmail" in fm:
        # 1. Try ownedBy.email if Kylas includes it in the payload
        ob = raw.get("ownedBy") or {}
        email = ""
        if isinstance(ob, dict):
            email = (ob.get("email") or ob.get("emailId") or "").strip().lower()
        # 2. Fall back to name-based map (team.json + Kylas API)
        if not email and user_email_map:
            email = user_email_map.get(owner_name, "")
        if email:
            fields[fm["ownerEmail"]] = email
    return fields


def _load_table(tbl: AirtableClient, id_field: str, name_field: str):
    """
    Fetch all records once. Populate tbl._cache keyed by id_field (non-blank).
    Returns (id_count, name_count, name_lookup) where name_lookup maps
    lowercase company name → record for records that have no Kylas ID yet
    (e.g. CSV-imported companies). Used to match by name and avoid duplicates.
    """
    for attempt in range(4):
        try:
            records = tbl.table.all()
            break
        except Exception as exc:
            if attempt < 3:
                time.sleep(2 ** attempt)
            else:
                raise

    tbl._cache  = {}
    name_lookup = {}
    for r in records:
        kid = str(r["fields"].get(id_field, "")).strip()
        if kid:
            tbl._cache[kid] = r
        else:
            name = str(r["fields"].get(name_field, "")).strip().lower()
            if name:
                name_lookup[name] = r

    return len(tbl._cache), len(name_lookup), name_lookup


def run(test_mode: bool = False, logger: SyncLogger = None, since: str = None) -> dict:
    kylas        = KylasClient()
    company_base = os.environ.get("AIRTABLE_COMPANY_BASE_ID") or os.environ["AIRTABLE_BASE_ID"]
    crm_base     = os.environ["AIRTABLE_BASE_ID"]

    tbl_list = AirtableClient("Company List", base_id=company_base)
    tbl_crm  = AirtableClient("Companies",    base_id=crm_base)

    if logger is None:
        logger = SyncLogger()

    # Build owner name → email map: team.json base + live Kylas API (overrides)
    user_email_map = {}
    try:
        _tp = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "team.json")
        with open(_tp) as _f:
            user_email_map = json.load(_f).get("kylas_user_emails", {})
    except Exception:
        pass
    try:
        api_emails = kylas.get_user_emails()
        user_email_map.update(api_emails)   # API is authoritative
        print(f"[Companies] User email map: {len(user_email_map)} entries")
    except Exception as e:
        print(f"[Companies] WARNING: Kylas user email fetch failed ({e}) — using team.json only")

    log_id = logger.start("Companies")
    created = updated = failed = skipped = 0
    per_user = {}
    crm_ok = True

    try:
        list_ok         = True
        list_name_lookup = {}
        try:
            id_ct, nm_ct, list_name_lookup = _load_table(tbl_list, _fm()["id"], _fm()["name"])
            print(f"[Companies] Company List: {id_ct} by ID, {nm_ct} by name (no Kylas ID)")
        except Exception as e:
            list_ok = False
            print(f"[Companies] WARNING: Company List not accessible — {e}")
            print("[Companies] Check that AIRTABLE_COMPANY_BASE_ID secret = app55PsyRKqkf2CAQ")
            print("[Companies] Skipping Company List sync; CRM Companies table will still sync.")

        crm_name_lookup = {}
        try:
            id_ct, nm_ct, crm_name_lookup = _load_table(tbl_crm, _fm_crm()["id"], _fm_crm()["name"])
            print(f"[Companies] CRM Companies: {id_ct} by ID, {nm_ct} by name (no Kylas ID)")
        except Exception as e:
            print(f"[Companies] WARNING: CRM Companies table not ready ({e}) — run Setup Airtable Schema first")
            crm_ok = False

        companies = kylas.get_companies(since=since)
        if test_mode:
            companies = companies[:5]
        print(f"[Companies] Fetched {len(companies)} from Kylas")

        for co in companies:
            try:
                user     = _assigned_name(co)
                kylas_id = str(co["id"])
                co_name  = (co.get("name") or "").strip().lower()

                # Name-based fallback: if a CSV-imported record (blank Kylas ID)
                # matches this company's name, link it into the cache so upsert
                # updates instead of creating a duplicate.
                if list_ok and kylas_id not in tbl_list._cache and co_name in list_name_lookup:
                    tbl_list._cache[kylas_id] = list_name_lookup.pop(co_name)
                if crm_ok and kylas_id not in tbl_crm._cache and co_name in crm_name_lookup:
                    tbl_crm._cache[kylas_id] = crm_name_lookup.pop(co_name)

                list_action = crm_action = "skipped"
                if list_ok:
                    list_action, _ = tbl_list.upsert(
                        _fm()["id"], kylas_id,
                        _build_fields(co, _fm(), user_email_map), co.get("updatedAt", ""),
                        updated_at_field=_fm()["updatedAt"],
                    )
                if crm_ok:
                    crm_action, _ = tbl_crm.upsert(
                        _fm_crm()["id"], kylas_id,
                        _build_fields(co, _fm_crm(), user_email_map), co.get("updatedAt", ""),
                        updated_at_field=_fm_crm()["updatedAt"],
                    )

                action = list_action if list_ok else crm_action
                if action == "created":
                    created += 1
                    per_user.setdefault(user, {"created": 0, "updated": 0})["created"] += 1
                elif action == "updated":
                    updated += 1
                    per_user.setdefault(user, {"created": 0, "updated": 0})["updated"] += 1
                elif action == "skipped":
                    skipped += 1

            except Exception as e:
                failed += 1
                print(f"  [FAILED  ] Company {co.get('id')}: {e}")

        print(f"[Companies] Flushing {created} creates + {updated} updates...")
        if list_ok:
            tbl_list.flush()
        if crm_ok:
            tbl_crm.flush()

        logger.finish(log_id, created, updated, failed)
        print(f"[Companies] Done -> created={created} updated={updated} skipped={skipped} failed={failed}")

    except Exception as e:
        logger.fail(log_id, str(e))
        raise

    id_map = {kid: rec["id"] for kid, rec in tbl_crm._cache.items()} if crm_ok else {}
    print(f"[Companies] CRM id_map: {len(id_map)} entries available for linking")

    return {"created": created, "updated": updated, "failed": failed,
            "per_user": per_user, "id_map": id_map}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()
    from dotenv import load_dotenv; load_dotenv()
    run(test_mode=args.test)
