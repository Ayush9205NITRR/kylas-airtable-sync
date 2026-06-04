"""
Fetch 2 raw records from each Kylas entity (no field filter) and
print every key/value so we can build a complete field map.
"""
import json
import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()

BASE = "https://api.kylas.io/v1"
HEADERS = {
    "api-key": os.environ["KYLAS_API_KEY"],
    "Content-Type": "application/json",
}


def fetch_raw(entity: str, n: int = 2) -> list:
    r = requests.post(
        f"{BASE}/search/{entity}",
        params={"page": 0, "size": n, "sort": "updatedAt,desc"},
        json={"fields": None, "jsonRule": None},
        headers=HEADERS,
        timeout=60,
    )
    r.raise_for_status()
    return r.json().get("content", [])


def show(label: str, entity: str):
    print(f"\n{'='*60}")
    print(f"ENTITY: {label}")
    print("=" * 60)
    records = fetch_raw(entity, n=2)
    if not records:
        print("  (no records found)")
        return
    rec = records[0]
    for key, value in rec.items():
        print(f"  {key:<35} {json.dumps(value, default=str)[:120]}")


show("Contact", "contact")
time.sleep(0.5)
show("Company", "company")
time.sleep(0.5)
show("Deal", "deal")
