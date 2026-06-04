import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.kylas_client import KylasClient
from utils.airtable_client import AirtableClient
from utils.logger import SyncLogger

_FM = None


def _fm():
    global _FM
    if _FM is None:
        p = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "field_map.json")
        with open(p) as f:
            _FM = json.load(f)["deal"]
    return _FM


def _clean(d):
    return {k: v for k, v in d.items() if v is not None and (v != "" if isinstance(v, str) else True)}


def _assigned_name(raw: dict) -> str:
    a = raw.get("ownedBy") or {}
    if isinstance(a, dict):
        return a.get("name") or a.get("firstName") or "Unassigned"
    return str(a) if a else "Unassigned"


def _map(raw: dict) -> dict:
    fm = _fm()

    deal_val = raw.get("estimatedValue") or {}
    value    = deal_val.get("value", 0) if isinstance(deal_val, dict) else 0

    actual_val = raw.get("actualValue") or {}
    actual     = actual_val.get("value", 0) if isinstance(actual_val, dict) else 0

    pipeline = raw.get("pipeline") or {}
    stage    = raw.get("pipelineStage") or {}

    contacts_list = raw.get("associatedContacts") or []
    contact_name  = contacts_list[0].get("name", "").strip() if contacts_list else ""
    contact_id    = str(contacts_list[0].get("id", "")) if contacts_list else ""

    company  = raw.get("company") or {}
    co_name  = company.get("name", "") if isinstance(company, dict) else ""
    co_id    = str(company.get("id", "")) if isinstance(company, dict) else ""

    src    = raw.get("source") or {}
    source = src.get("name", "") if isinstance(src, dict) else ""

    cf        = raw.get("customFieldValues") or {}
    pax       = str(cf.get("cfPaxNumberOfParticipants") or "")
    exec_date = cf.get("cfExecutionDate") or ""
    if exec_date and "T" in str(exec_date):
        exec_date = str(exec_date)[:10]
    location  = str(cf.get("cfLocation") or "")

    return _clean({
        fm["id"]:                  str(raw["id"]),
        fm["name"]:                raw.get("name", ""),
        fm["dealValue"]:           value,
        fm["actualValue"]:         actual,
        fm["pipeline"]:            pipeline.get("name", "") if isinstance(pipeline, dict) else str(pipeline),
        fm["pipelineStage"]:       stage.get("name", "") if isinstance(stage, dict) else str(stage),
        fm["contactName"]:         contact_name,
        fm["contactId"]:           contact_id,
        fm["companyName"]:         co_name,
        fm["companyId"]:           co_id,
        fm["assignedTo"]:          _assigned_name(raw),
        fm["source"]:              source,
        fm["forecastType"]:        raw.get("forecastingType") or "",
        fm["paxCount"]:            pax,
        fm["executionDate"]:       exec_date,
        fm["location"]:            location,
        fm["expectedClosureDate"]: raw.get("estimatedClosureOn") or "",
        fm["createdAt"]:           raw.get("createdAt", ""),
        fm["updatedAt"]:           raw.get("updatedAt", ""),
    })


def run(test_mode: bool = False, logger: SyncLogger = None) -> dict:
    kylas    = KylasClient()
    airtable = AirtableClient("Deals")
    if logger is None:
        logger = SyncLogger()

    log_id = logger.start("Deals")
    created = updated = failed = 0
    per_user = {}

    try:
        try:
            cached = airtable.build_cache("Kylas Deal Id")
        except Exception as e:
            msg = f"Deals table not accessible — run 'Setup Airtable Schema' workflow first. Error: {e}"
            print(f"[Deals] WARNING: {msg}")
            logger.fail(log_id, msg)
            return {"created": 0, "updated": 0, "failed": 0, "per_user": {}}
        print(f"[Deals] Cache loaded: {cached} existing")

        deals = kylas.get_deals()
        if test_mode:
            deals = deals[:5]
        print(f"[Deals] Fetched {len(deals)} from Kylas")

        for deal in deals:
            try:
                user   = _assigned_name(deal)
                action, _ = airtable.upsert(
                    "Kylas Deal Id", str(deal["id"]),
                    _map(deal), deal.get("updatedAt", ""),
                    updated_at_field=_fm()["updatedAt"],
                )
                if action == "created":
                    created += 1
                    per_user.setdefault(user, {"created": 0, "updated": 0})["created"] += 1
                elif action == "updated":
                    updated += 1
                    per_user.setdefault(user, {"created": 0, "updated": 0})["updated"] += 1
            except Exception as e:
                failed += 1
                print(f"  [FAILED  ] Deal {deal.get('id')}: {e}")

        print(f"[Deals] Flushing {created} creates + {updated} updates to Airtable...")
        airtable.flush()

        logger.finish(log_id, created, updated, failed)
        print(f"[Deals] Done -> created={created} updated={updated} failed={failed}")

    except Exception as e:
        logger.fail(log_id, str(e))
        raise

    return {"created": created, "updated": updated, "failed": failed, "per_user": per_user}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()
    from dotenv import load_dotenv; load_dotenv()
    run(test_mode=args.test)
