"""Smoke test: syncs 1 contact. Pass --id=5381741 to test a specific contact."""
import argparse
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", type=int, dest="contact_id")
    args = parser.parse_args()

    print(f"Testing contact sync (id={args.contact_id or 'first'}) ...")
    result = _load("02_contact_sync.py").run(test_mode=True, test_id=args.contact_id)
    print(f"Result: {result}")
    assert result["failed"] == 0, f"FAILED: {result}"
    print("PASS")
