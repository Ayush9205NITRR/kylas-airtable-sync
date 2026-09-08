#!/usr/bin/env python3
"""
Discovery probe for Otter.ai's Public API -- run this ONCE, by hand, before
writing the real Otter -> Kylas pipeline.

Otter's own docs (help.otter.ai), apitracker.io, and semarize.com are all
blocked by this environment's network egress proxy, so the exact base URL,
endpoint paths, and response shape below are UNVERIFIED -- pieced together
from web search summaries, not confirmed documentation. Rather than write the
real Kylas-writing pipeline against a guess -- exactly the kind of repeated
guess-and-check cycle this script exists to avoid -- this tries the most
plausible candidates and prints exactly what each one returns, so the next
step is written against real data instead of another guess.

Known for reasonably certain (from Otter's own docs, via web search):
  - Auth: Bearer token in the Authorization header
  - Rate limit: 10 requests/second (Enterprise plan)
  - Otter calls a meeting record a "speech"; the public API's own docs
    reference a "speeches" endpoint

NOT verified -- this probe exists to nail these down before anything else is
written:
  - The exact base URL and path for "list recent speeches" / "get one speech"
  - Whether, and under what field name, a speech's participants/guests and
    their emails appear on the API response (vs. the Zapier trigger's
    "Calendar Guests" field, which is Zapier's own naming, not necessarily
    the API's)

    OTTER_API_KEY=xxx python scripts/probe_otter_api.py
"""
import json
import os
import sys

import requests

API_KEY = os.environ.get("OTTER_API_KEY", "")
if not API_KEY:
    print("ERROR: set OTTER_API_KEY")
    sys.exit(1)

# Tried in this order since the exact auth scheme isn't confirmed either --
# most APIs styled like this use "Bearer", a few use the raw key with no
# prefix. Report which one a candidate accepts (401 vs 2xx) rather than
# assume.
HEADER_VARIANTS = [
    {"Authorization": f"Bearer {API_KEY}"},
    {"Authorization": API_KEY},
]

# (base_url, path) candidates for "list recent speeches/conversations".
# https://otter.ai/forward/api/v1/... is a pattern the OTTER WEB APP itself
# is known to call (unofficial, reverse-engineered); https://api.otter.ai/...
# is the more likely shape for a proper documented "Public API" product,
# since it's pitched as separate from the legacy internal web API. Trying
# both rather than betting on one.
CANDIDATES = [
    ("https://api.otter.ai", "/v1/speeches"),
    ("https://api.otter.ai", "/v1/conversations"),
    ("https://api.otter.ai", "/v1/channels"),
    ("https://otter.ai", "/forward/api/v1/speeches"),
    ("https://otter.ai", "/forward/api/v1/speech_search"),
]


def _try(base: str, path: str, headers: dict) -> bool:
    """Returns True if this candidate looks like a real, working endpoint."""
    url = base + path
    try:
        r = requests.get(url, headers=headers, timeout=15)
    except Exception as exc:
        print(f"    EXCEPTION: {exc}")
        return False
    print(f"    HTTP {r.status_code}")
    if not r.ok:
        print(f"    Body (first 300 chars): {r.text[:300]}")
        return False
    try:
        body = r.json()
    except Exception:
        print(f"    2xx but non-JSON body (first 300 chars): {r.text[:300]}")
        return False
    shape = list(body.keys()) if isinstance(body, dict) else f"{type(body).__name__} (len {len(body)})"
    print(f"    JSON top-level shape: {shape}")
    print(f"    First 1500 chars:\n{json.dumps(body, indent=2)[:1500]}")
    return True


def main() -> int:
    found_working = False
    for base, path in CANDIDATES:
        for headers in HEADER_VARIANTS:
            auth_style = "Bearer <key>" if headers["Authorization"].startswith("Bearer") else "<key> (no prefix)"
            print(f"\n=== GET {base}{path}  [Authorization: {auth_style}] ===")
            if _try(base, path, headers):
                found_working = True
                print("    ^ this one looks real -- stopping further "
                      "auth-variant attempts for this path")
                break

    print("\n" + "=" * 70)
    if found_working:
        print("At least one candidate returned real JSON above. Send me that "
              "block (base URL, path, and the JSON shape/keys shown) and "
              "I'll write the real pipeline against it.")
    else:
        print("Nothing worked. Send me the FULL output above anyway -- the "
              "exact status codes and error bodies (401 vs 404 vs something "
              "else) tell us whether it's a wrong path, wrong auth scheme, "
              "or the key itself isn't enabled for API access yet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
