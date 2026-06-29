"""
Create the "Offsite Timeline (BD - New)" multi-select picklist field on a Kylas entity.

This script POSTs to /entities/{entity}/fields to create the field. Kylas has
no field-update endpoint — type and multiValue cannot be changed after creation.
If the field already exists, the POST will fail (Kylas returns 400/409); the
existing field is not modified.

UI fallback (if you prefer the Kylas interface):
  1. Log in to Kylas → Settings → Customization → Form Fields.
  2. Select "Company" from the entity drop-down.
  3. Click "Add Field" → choose "Picklist (Multi-select)".
  4. Set Display Name to "Offsite Timeline (BD - New)".
  5. Add the option labels: Jan - Mar, Apr - Jun, Jul - Sep, Oct - Dec.
     (These must match the option labels in the contact's "Offsite Timeline"
      single-select field so the label-bridge in the rollup script works.)
  6. Save.

Usage:
    python scripts/create_offsite_field.py --dry-run            (preview body, no write)
    python scripts/create_offsite_field.py                      (create the field)
    python scripts/create_offsite_field.py --display "My Field" --options "A,B,C"
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from utils.kylas_client import KylasClient

DEFAULT_OPTIONS = "Jan - Mar,Apr - Jun,Jul - Sep,Oct - Dec"


def run(display: str, entity: str, options_str: str, dry_run: bool):
    option_labels = [o.strip() for o in options_str.split(",") if o.strip()]
    body = {
        "displayName": display,
        "description": "Company offsite timeline (multi-select, rolled up from contacts)",
        "type": "PICK_LIST",
        "multiValue": True,
        "standard": False,
        "required": False,
        "filterable": True,
        "sortable": True,
        "important": False,
        "pickLists": [
            {"displayName": o, "value": o}
            for o in option_labels
        ],
    }

    if dry_run:
        print("[DRY RUN] Would POST the following body to "
              f"/entities/{entity}/fields?entityType={entity}")
        print(json.dumps(body, indent=2))
        return

    load_dotenv()
    client = KylasClient()
    try:
        r = client._request(
            "POST", f"entities/{entity}/fields",
            params={"entityType": entity},
            json=body,
        )
        if r.ok:
            try:
                data = r.json()
                created = data.get("data", data) if isinstance(data, dict) else data
                key  = (created.get("fieldName") or created.get("apiName")
                        or created.get("name") or "(unknown key)")
                name = created.get("displayName") or display
                print(f"Created: key={key!r}  displayName={name!r}")
            except Exception:
                print(f"Created (HTTP {r.status_code}): {r.text[:300]}")
        else:
            try:
                err = r.json()
            except Exception:
                err = r.text
            print(f"ERROR HTTP {r.status_code}: {json.dumps(err, indent=2)[:600]}")
            sys.exit(1)
    except Exception as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Create a multi-select picklist field on a Kylas entity."
    )
    parser.add_argument("--display", default="Offsite Timeline (BD - New)",
                        help="Display name for the new field")
    parser.add_argument("--entity", default="company",
                        help="Kylas entity type (default: company)")
    parser.add_argument("--options", default=DEFAULT_OPTIONS,
                        help="Comma-separated option labels (default: quarterly periods)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the request body without POSTing")
    args = parser.parse_args()
    run(display=args.display, entity=args.entity,
        options_str=args.options, dry_run=args.dry_run)
