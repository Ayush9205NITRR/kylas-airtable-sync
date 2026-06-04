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


def _fm():
    global _FM
    if _FM is None:
        p = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "field_map.json")
        with open(p) as f:
            _FM = json.load(f)["contact"]
    return _FM


def _clean(d):
    return {k: v for k, v in d.items() if v is not None and (v != "" if isinstance(v, str) else True)}


def _assigned_name(raw: dict) -> str:
    a = raw.get("ownedBy") or raw.get("assignedTo") or {}
    if isinstance(a, dict):
        return a.get("name") or a.get("firstName") or "Unassigned"
    return str(a) if a else "Unassigned"


def _map(raw: dict) -> dict:
    fm      = _fm()
    emails  = raw.get("emails") or []
    phones  = raw.get("phoneNumbers") or []
    company = raw.get("company") or {}
    return _clean({
        fm["id"]:          str(raw["id"]),
        fm["firstName"]:   raw.get("firstName", ""),
        fm["lastName"]:    raw.get("lastName", ""),
        fm["email"]:       emails[0].get("value", "") if emails else "",
        fm["phone"]:       phones[0].get("value", "") if phones else "",
        fm["companyId"]:   str(company.get("id", "")) if isinstance(company, dict) else "",
        fm["designation"]: raw.get("designation", ""),
        fm["assignedTo"]:  _assigned_name(raw),
        fm["createdAt"]:   raw.get("createdAt", ""),
        fm["updatedAt"]:   raw.get("updatedAt", ""),
    })


def run(test_mode: bool = False, test_id: int = None, logger: SyncLogger = None) -> dict:
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
                contacts = contacts[:1]
        print(f"[Contacts] Fetched {len(contacts)} from Kylas")

        for ct in contacts:
            try:
                created_str = ct.get("createdAt", "")
                if created_str:
                    dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                    if dt < CUTOFF:
                        pre_cutoff += 1
                        continue

                user   = _assigned_name(ct)
                action, _ = airtable.upsert(
                    "Kylas Contact Id", str(ct["id"]),
                    _map(ct), ct.get("updatedAt", "")
                )
                if action == "created":
                    created += 1
                    per_user.setdefault(user, {"created": 0, "updated": 0})["created"] += 1
                elif action == "updated":
                    updated += 1
                    per_user.setdefault(user, {"created": 0, "updated": 0})["updated"] += 1
                name = f"{ct.get('firstName', '')} {ct.get('lastName', '')}".strip()
                print(f"  [{action.upper():8}] {name or ct['id']} ({user})")
            except Exception as e:
                failed += 1
                print(f"  [FAILED  ] Contact {ct.get('id')}: {e}")

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
    run(test_mode=args.test, test_id=args.contact_id)
