import os
import uuid
from datetime import datetime, timezone
from pyairtable import Api


class SyncLogger:
    def __init__(self):
        api = Api(os.environ["AIRTABLE_PAT"])
        self.table = api.table(os.environ["AIRTABLE_BASE_ID"], "Sync Log")
        self.run_id = str(uuid.uuid4())[:8].upper()

    def start(self, module: str) -> str:
        record = self.table.create({
            "Run ID": self.run_id,
            "Module": module,
            "Status": "running",
            "Started At": _now(),
        })
        return record["id"]

    def finish(self, record_id: str, created: int, updated: int, failed: int, error: str = ""):
        self.table.update(record_id, {
            "Status": "failed" if (error or failed) else "success",
            "Created": created,
            "Updated": updated,
            "Failed": failed,
            "Error": error,
            "Finished At": _now(),
        })

    def fail(self, record_id: str, error: str):
        self.table.update(record_id, {
            "Status": "failed",
            "Error": str(error)[:5000],
            "Finished At": _now(),
        })


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
