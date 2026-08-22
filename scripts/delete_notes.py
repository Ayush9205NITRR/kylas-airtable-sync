"""
Delete specific notes by id. One-off cleanup tool.

Written to remove notes 55053005 and 55053008: duplicate 2026-08-21 call
summaries posted onto deals 4436857 and 4443457 because already_noted read a
note shape get_all_notes never returns, so the [calls ...] marker matched
nothing and the re-run did not skip deals that were already done.

DELETE /v1/notes/{id} is the documented removal endpoint. This deletes ONLY the
ids passed in NOTE_IDS and prints what each one returned.

SAFETY: destructive. It names ids explicitly and never searches for what to
delete, so it cannot widen its own blast radius.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from dotenv import load_dotenv

load_dotenv()

BASE = "https://api.kylas.io/v1"
HEADERS = {"api-key": os.environ["KYLAS_API_KEY"], "Content-Type": "application/json"}


def main():
    ids = [x.strip() for x in (os.environ.get("NOTE_IDS") or "").split() if x.strip()]
    if not ids:
        print("NOTE_IDS empty — nothing to delete.")
        return 0
    print(f"Deleting {len(ids)} note(s): {' '.join(ids)}")
    failed = 0
    for nid in ids:
        try:
            r = requests.delete(f"{BASE}/notes/{nid}", headers=HEADERS, timeout=30)
        except Exception as exc:
            print(f"  note {nid}: EXC {str(exc)[:120]}")
            failed += 1
            continue
        ok = 200 <= r.status_code < 300
        print(f"  note {nid}: HTTP {r.status_code} {'deleted' if ok else r.text[:120]}")
        if not ok:
            failed += 1
    print(f"\nDeleted {len(ids) - failed}, failed {failed}.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
