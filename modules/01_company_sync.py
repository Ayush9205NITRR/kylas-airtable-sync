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
            _FM = json.load(f)["company"]
    return _FM


def _clean(d):
    return {k: v for k, v in d.items() if v is not None and (v != "" if isinstance(v, str) else True)}


def _map(raw: dict) -> dict:
    fm = _fm()
    phones = raw.get("phoneNumbers") or []
    emails = raw.get("emails") or []
    industry = raw.get("industry") or {}
    addr = raw.get("address") or {}
    return _clean({
        fm["id"]:          str(raw["id"]),
        fm["name"]:        raw.get("name", ""),
        fm["industry"]:    industry.get("name", "") if isinstance(industry, dict) else str(industry),
        fm["website"]:     raw.get("website", ""),
        fm["phone"]:       phones[0].get("value", "") if phones else "",
        fm["email"]:       emails[0].get("value", "") if emails else "",
        fm["city"]:        addr.get("city", raw.get("city", "")),
        fm["state"]:       addr.get("state", raw.get("state", "")),
        fm["country"]:     addr.get("country", raw.get("country", "")),
        fm["description"]: raw.get("description", ""),
        fm["createdAt"]:   raw.get("createdAt", ""),
        fm["updatedAt"]:   raw.get("updatedAt", ""),
    })


def run(test_mode: bool = False, logger: SyncLogger = None) -> dict:
    kylas = KylasClient()
    company_base = os.environ.get("AIRTABLE_COMPANY_BASE_ID") or os.environ["AIRTABLE_BASE_ID"]
    airtable = AirtableClient("Company List", base_id=company_base)
    if logger is None:
        logger = SyncLogger()

    log_id = logger.start("Companies")
    created = updated = failed = 0

    try:
        cached = airtable.build_cache("Kylas Company Id")
        print(f"[Companies] Cache loaded: {cached} existing")

        companies = kylas.get_companies()
        if test_mode:
            companies = companies[:1]
        print(f"[Companies] Fetched {len(companies)} from Kylas")

        for co in companies:
            try:
                action, _ = airtable.upsert(
                    "Kylas Company Id", str(co["id"]),
                    _map(co), co.get("updatedAt", "")
                )
                if action == "created":   created += 1
                elif action == "updated": updated += 1
                print(f"  [{action.upper():8}] {co.get('name', co['id'])}")
            except Exception as e:
                failed += 1
                print(f"  [FAILED  ] Company {co.get('id')}: {e}")

        logger.finish(log_id, created, updated, failed)
        print(f"[Companies] Done -> created={created} updated={updated} failed={failed}")

    except Exception as e:
        logger.fail(log_id, str(e))
        raise

    return {"created": created, "updated": updated, "failed": failed}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()
    from dotenv import load_dotenv; load_dotenv()
    run(test_mode=args.test)
