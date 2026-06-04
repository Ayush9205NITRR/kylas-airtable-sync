"""Smoke test: syncs 1 deal from Kylas -> Airtable."""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from dotenv import load_dotenv
load_dotenv()


def _load(filename):
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "modules", filename)
    spec = importlib.util.spec_from_file_location("m", path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


if __name__ == "__main__":
    print("Testing deal sync (1 record) ...")
    result = _load("03_deal_sync.py").run(test_mode=True)
    print(f"Result: {result}")
    assert result["failed"] == 0, f"FAILED: {result}"
    print("PASS")
