import os
import uuid
from datetime import datetime, timezone
from pyairtable import Api


class SyncLogger:
    def __init__(self):
        try:
            api = Api(os.environ["AIRTABLE_PAT"])
            self.table = api.table(os.environ["AIRTABLE_BASE_ID"], "Sync Log")
        except Exception as e:
            print(f"[SyncLogger] Init failed (logging disabled): {e}")
            self.table = None
        self.run_id = str(uuid.uuid4())[:8].upper()

    def start(self, module: str) -> str:
        if self.table is None:
            return ""
        try:
            record = self.table.create({
                "Run ID": self.run_id,
                "Module": module,
                "Status": "running",
                "Started At": _now(),
            }, typecast=True)
            return record["id"]
        except Exception as e:
            print(f"[SyncLogger] start() failed: {e}")
            return ""

    def finish(self, record_id: str, created: int, updated: int, failed: int, error: str = ""):
        if not self.table or not record_id:
            return
        try:
            self.table.update(record_id, {
                "Status": "failed" if (error or failed) else "success",
                "Created": created,
                "Updated": updated,
                "Failed": failed,
                "Error": error,
                "Finished At": _now(),
            }, typecast=True)
        except Exception as e:
            print(f"[SyncLogger] finish() failed: {e}")

    def fail(self, record_id: str, error: str):
        if not self.table or not record_id:
            return
        try:
            self.table.update(record_id, {
                "Status": "failed",
                "Error": str(error)[:5000],
                "Finished At": _now(),
            }, typecast=True)
        except Exception as e:
            print(f"[SyncLogger] fail() failed: {e}")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
