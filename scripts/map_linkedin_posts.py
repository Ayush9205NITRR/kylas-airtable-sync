"""
Map scraped LinkedIn posts (CSV) onto Airtable records by LinkedIn URL.

Match key:
    Airtable "linkedin - Appollo"  <->  CSV "URL (Mapping)"
URLs are normalised before comparing (http/https, www./in., trailing slash,
query string and case are ignored).

For every matched Airtable record:
    CSV "Post URL"     -> Airtable "Boolean (linked)"
    CSV "Calculation"  -> Airtable "Offsite Timeline (linked)"
When one company has several posts in the CSV, the most recent post
(by "Post Date") wins.

Every CSV row is written to the report CSV with a "Mapping Status" column:
"mapped" if its URL matched an Airtable record, else "not mapped".
Airtable records with no match are left untouched.

Usage:
    python scripts/map_linkedin_posts.py --dry-run   # preview, no writes
    python scripts/map_linkedin_posts.py             # apply
"""
import argparse
import csv
import os
import re
import sys
from urllib.parse import unquote, urlparse

import requests
from dotenv import load_dotenv
from pyairtable import Api

load_dotenv()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_BASE = "app55PsyRKqkf2CAQ"
DEFAULT_TABLE = "tbl2Jje9EBC4Cqydw"
DEFAULT_CSV = os.path.join(ROOT, "data", "linkedin_posts_mapping.csv")
DEFAULT_REPORT = os.path.join(ROOT, "data", "linkedin_posts_mapping_report.csv")

AT_KEY_FIELD = "linkedin - Appollo"
AT_POST_FIELD = "Boolean (linked)"
AT_TIMELINE_FIELD = "Offsite Timeline (linked)"

CSV_KEY_COL = "URL (Mapping)"
CSV_POST_COL = "Post URL"
CSV_TIMELINE_COL = "Calculation"
CSV_DATE_COL = "Post Date"

WRITABLE_TYPES = {
    "singleLineText", "multilineText", "richText", "url",
    "singleSelect", "multipleSelects", "checkbox",
}

URL_RE = re.compile(r"(?:https?://)?(?:[a-z]{2,3}\.)?linkedin\.com/[^\s,;\"'<>]+", re.I)


def normalize_url(url: str) -> str:
    """linkedin.com/company/foo — scheme, subdomain, query and trailing / dropped."""
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    p = urlparse(url)
    host = p.netloc.lower().split(":")[0]
    if host.endswith("linkedin.com"):
        host = "linkedin.com"
    elif host.startswith("www."):
        host = host[4:]
    path = unquote(p.path).rstrip("/").lower()
    return f"{host}{path}"


def extract_urls(value) -> list:
    """All LinkedIn URLs in an Airtable cell (text, url or list of either)."""
    if value is None:
        return []
    if isinstance(value, list):
        return [u for v in value for u in extract_urls(v)]
    text = str(value)
    found = URL_RE.findall(text)
    return found or ([text] if text.strip() else [])


def load_csv(path: str):
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    missing = {CSV_KEY_COL, CSV_POST_COL, CSV_TIMELINE_COL} - set(fieldnames)
    if missing:
        sys.exit(f"CSV is missing column(s): {sorted(missing)}")
    return rows, fieldnames


def latest_post_by_url(rows: list) -> dict:
    """normalised URL -> the CSV row with the newest Post Date."""
    best = {}
    for r in rows:
        key = normalize_url(r.get(CSV_KEY_COL, ""))
        if not key or not (r.get(CSV_POST_COL) or "").strip():
            continue
        cur = best.get(key)
        if cur is None or (r.get(CSV_DATE_COL) or "") > (cur.get(CSV_DATE_COL) or ""):
            best[key] = r
    return best


def field_types(pat: str, base_id: str, table_id: str) -> dict:
    """{field name: type} from the Airtable meta API, or {} if not permitted."""
    try:
        resp = requests.get(
            f"https://api.airtable.com/v0/meta/bases/{base_id}/tables",
            headers={"Authorization": f"Bearer {pat}"}, timeout=30,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"[warn] could not read table schema ({exc}); assuming text fields")
        return {}
    for t in resp.json().get("tables", []):
        if table_id in (t["id"], t["name"]):
            return {f["name"]: f["type"] for f in t["fields"]}
    print(f"[warn] table {table_id} not found in schema; assuming text fields")
    return {}


def to_cell(value: str, ftype: str):
    value = (value or "").strip()
    if ftype == "checkbox":
        return bool(value)
    if ftype == "multipleSelects":
        return [value] if value else []
    return value or None


def same(current, new) -> bool:
    if isinstance(new, list):
        return sorted(current or []) == sorted(new)
    return (current or None) == new


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="Preview only, write nothing")
    ap.add_argument("--csv", default=DEFAULT_CSV)
    ap.add_argument("--report", default=DEFAULT_REPORT)
    ap.add_argument("--base", default=os.environ.get("AIRTABLE_COMPANY_BASE_ID") or DEFAULT_BASE)
    ap.add_argument("--table", default=DEFAULT_TABLE)
    ap.add_argument("--view", default="", help="Only consider records in this view")
    args = ap.parse_args()

    pat = os.environ["AIRTABLE_PAT"]
    rows, fieldnames = load_csv(args.csv)
    posts = latest_post_by_url(rows)
    print(f"CSV: {len(rows)} rows, {len(posts)} unique LinkedIn URLs")

    types = field_types(pat, args.base, args.table)
    for name in (AT_KEY_FIELD, AT_POST_FIELD, AT_TIMELINE_FIELD):
        if types and name not in types:
            sys.exit(f"Airtable field {name!r} not found. Available: {sorted(types)}")
    for name in (AT_POST_FIELD, AT_TIMELINE_FIELD):
        ftype = types.get(name, "singleLineText")
        if ftype not in WRITABLE_TYPES:
            sys.exit(f"Airtable field {name!r} is type {ftype!r}, which this script can't write "
                     f"(it expects text / url / select / checkbox)")
    post_type = types.get(AT_POST_FIELD, "singleLineText")
    tl_type = types.get(AT_TIMELINE_FIELD, "singleLineText")
    print(f"Field types: {AT_POST_FIELD}={post_type}, {AT_TIMELINE_FIELD}={tl_type}")

    table = Api(pat).table(args.base, args.table)
    kwargs = {"view": args.view} if args.view else {}
    records = table.all(**kwargs)
    print(f"Airtable: {len(records)} records in {args.base}/{args.table}"
          + (f" (view {args.view!r})" if args.view else ""))

    matched_keys = set()
    updates, unchanged, no_url = [], 0, 0
    for rec in records:
        f = rec["fields"]
        keys = [normalize_url(u) for u in extract_urls(f.get(AT_KEY_FIELD))]
        keys = [k for k in keys if k]
        if not keys:
            no_url += 1
            continue
        hits = [posts[k] for k in keys if k in posts]
        if not hits:
            continue
        matched_keys.update(k for k in keys if k in posts)
        src = max(hits, key=lambda r: r.get(CSV_DATE_COL) or "")
        new = {
            AT_POST_FIELD: to_cell(src[CSV_POST_COL], post_type),
            AT_TIMELINE_FIELD: to_cell(src[CSV_TIMELINE_COL], tl_type),
        }
        if all(same(f.get(k), v) for k, v in new.items()):
            unchanged += 1
            continue
        updates.append({"id": rec["id"], "fields": new})

    print(f"Matched Airtable records: {len(updates) + unchanged} "
          f"({len(updates)} to update, {unchanged} already up to date); "
          f"{no_url} records have no LinkedIn URL")
    for u in updates[:10]:
        print(f"  {u['id']}: {u['fields']}")

    status_col = "Mapping Status"
    mapped_rows = 0
    os.makedirs(os.path.dirname(os.path.abspath(args.report)), exist_ok=True)
    with open(args.report, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames + [status_col])
        w.writeheader()
        for r in rows:
            is_mapped = normalize_url(r.get(CSV_KEY_COL, "")) in matched_keys
            mapped_rows += is_mapped
            w.writerow({**r, status_col: "mapped" if is_mapped else "not mapped"})
    print(f"CSV rows: {mapped_rows} mapped, {len(rows) - mapped_rows} not mapped "
          f"-> {args.report}")

    if args.dry_run:
        print("[dry-run] no Airtable writes")
        return
    if updates:
        table.batch_update(updates, typecast=True)
    print(f"Updated {len(updates)} Airtable records")


if __name__ == "__main__":
    main()
