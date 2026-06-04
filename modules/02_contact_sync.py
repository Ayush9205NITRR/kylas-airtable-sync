import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.kylas_client import KylasClient
from utils.airtable_client import AirtableClient
from utils.logger import SyncLogger

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


def _owner_name(raw: dict, user_map: dict = None) -> str:
    ob = raw.get("ownedBy")
    if isinstance(ob, dict) and ob.get("name"):
        return ob["name"]
    oid = raw.get("ownerId")
    if oid and user_map:
        name = user_map.get(int(oid)) or user_map.get(str(oid))
        if name:
            return name
    return "Unassigned"


def _map(raw: dict, user_map: dict = None) -> dict:
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

    return _clean({
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
        fm["updatedAt"]:     raw.get("updatedAt") or "",
    })


def run(test_mode: bool = False, test_id: int = None,
        logger: SyncLogger = None, user_map: dict = None) -> dict:
    kylas    = KylasClient()
    airtable = AirtableClient("Contacts")
    if logger is None:
        logger = SyncLogger()

    log_id = logger.start("Contacts")
    created = updated = failed = pre_cutoff = 0
    per_user = {}

    try:
        cached = airtable.build_cache("Kylas Contact Id")
        print(f"[Contacts] Cache loaded: {cached} existing")

        if test_mode and test_id:
            contacts = [kylas.get_contact(test_id)]
        else:
            contacts = kylas.get_contacts()
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

                owner  = _owner_name(ct, user_map)
                action, _ = airtable.upsert(
                    "Kylas Contact Id", str(ct["id"]),
                    _map(ct, user_map=user_map), ct.get("updatedAt", ""),
                    updated_at_field=_fm()["updatedAt"],
                )
                if action == "created":
                    created += 1
                    per_user.setdefault(owner, {"created": 0, "updated": 0})["created"] += 1
                elif action == "updated":
                    updated += 1
                    per_user.setdefault(owner, {"created": 0, "updated": 0})["updated"] += 1
            except Exception as e:
                failed += 1
                print(f"  [FAILED  ] Contact {ct.get('id')}: {e}")

        print(f"[Contacts] Flushing {created} creates + {updated} updates to Airtable...")
        airtable.flush()

        logger.finish(log_id, created, updated, failed)
        print(f"[Contacts] Done -> created={created} updated={updated} pre-cutoff={pre_cutoff} failed={failed}")

    except Exception as e:
        logger.fail(log_id, str(e))
        raise

    return {"created": created, "updated": updated, "failed": failed, "per_user": per_user}


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
