import argparse
import json
import os
import sys

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


def _build_fields(raw: dict, fm: dict) -> dict:
    industry = raw.get("industry") or {}
    cf       = raw.get("customFieldValues") or {}

    psd = cf.get("cfPipelineStageBd")
    if isinstance(psd, dict):
        stage_bd = psd.get("name", "")
    elif psd is not None:
        stage_bd = str(psd)
    else:
        stage_bd = ""

    return _clean({
        fm["id"]:              str(raw["id"]),
        fm["name"]:            raw.get("name", ""),
        fm["industry"]:        industry.get("name", "") if isinstance(industry, dict) else str(industry),
        fm["assignedTo"]:      _assigned_name(raw),
        fm["updatedAt"]:       raw.get("updatedAt", ""),
        fm["batch"]:           cf.get("cfBatch") or "",
        fm["pipelineStageBd"]: stage_bd,
        fm["sourceOfData"]:    cf.get("cfSourceOfData") or "",
    })


def run(test_mode: bool = False, logger: SyncLogger = None) -> dict:
    kylas        = KylasClient()
    company_base = os.environ.get("AIRTABLE_COMPANY_BASE_ID") or os.environ["AIRTABLE_BASE_ID"]
    crm_base     = os.environ["AIRTABLE_BASE_ID"]

    tbl_list = AirtableClient("Company List", base_id=company_base)
    tbl_crm  = AirtableClient("Companies",    base_id=crm_base)

    if logger is None:
        logger = SyncLogger()

    log_id = logger.start("Companies")
    created = updated = failed = 0
    per_user = {}
    crm_ok = True

    try:
        list_ok = True
        try:
            list_cached = tbl_list.build_cache("Kylas Company Id")
            print(f"[Companies] Company List cache: {list_cached} existing")
        except Exception as e:
            list_ok = False
            print(f"[Companies] WARNING: Company List not accessible — {e}")
            print("[Companies] Check that AIRTABLE_COMPANY_BASE_ID secret = app55PsyRKqkf2CAQ")
            print("[Companies] Skipping Company List sync; CRM Companies table will still sync.")

        try:
            crm_cached = tbl_crm.build_cache("Kylas Company Id")
            print(f"[Companies] CRM Companies cache: {crm_cached} existing")
        except Exception as e:
            print(f"[Companies] WARNING: CRM Companies table not ready ({e}) — run Setup Airtable Schema first")
            crm_ok = False

        companies = kylas.get_companies()
        if test_mode:
            companies = companies[:5]
        print(f"[Companies] Fetched {len(companies)} from Kylas")

        for co in companies:
            try:
                user = _assigned_name(co)
                list_action = crm_action = "skipped"
                if list_ok:
                    list_action, _ = tbl_list.upsert(
                        "Kylas Company Id", str(co["id"]),
                        _build_fields(co, _fm()), co.get("updatedAt", ""),
                        updated_at_field=_fm()["updatedAt"],
                    )
                if crm_ok:
                    crm_action, _ = tbl_crm.upsert(
                        "Kylas Company Id", str(co["id"]),
                        _build_fields(co, _fm_crm()), co.get("updatedAt", ""),
                        updated_at_field=_fm_crm()["updatedAt"],
                    )
                # Use Company List action as primary; fall back to CRM action
                action = list_action if list_ok else crm_action
                if action == "created":
                    created += 1
                    per_user.setdefault(user, {"created": 0, "updated": 0})["created"] += 1
                elif action == "updated":
                    updated += 1
                    per_user.setdefault(user, {"created": 0, "updated": 0})["updated"] += 1
            except Exception as e:
                failed += 1
                print(f"  [FAILED  ] Company {co.get('id')}: {e}")

        print(f"[Companies] Flushing {created} creates + {updated} updates...")
        if list_ok:
            tbl_list.flush()
        if crm_ok:
            tbl_crm.flush()

        logger.finish(log_id, created, updated, failed)
        print(f"[Companies] Done -> created={created} updated={updated} failed={failed}")

    except Exception as e:
        logger.fail(log_id, str(e))
        raise

    # Return Kylas ID → Airtable record ID map for contact/deal linking
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
