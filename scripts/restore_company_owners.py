"""
restore_company_owners.py — Restore Kylas company owners from the Airtable snapshot.

Background: field PUTs to /companies/{id} that omit the owner make Kylas
silently reassign the company to the API user. Several runs on 2026-07-01
(the scheduled daily push at 16:07 UTC and two backfill runs at 19:52 and
20:58 UTC) did exactly that. The Kylas→Airtable company sync last wrote
owner data at 13:00 UTC that day — BEFORE all damage — so Airtable's
"Owner - Kylas" / "Owner Email" columns are a clean pre-damage snapshot.

Modes:
  --scan             Read-only. Compare every Kylas company's current owner
                     to the Airtable snapshot; print a histogram of who the
                     mismatched companies now belong to (the API user will
                     dominate), plus a sample and per-company detail lines.
  --restore          Restore owners. Only touches companies whose CURRENT
                     owner id equals --from-owner-id (the API user that
                     absorbed the resets) AND whose Airtable owner resolves
                     to a different Kylas user. Uses the dedicated
                     PUT /companies/{id}/owner endpoint (never a field PUT).
  --from-owner-id N  Required with --restore: the uid whose companies get
                     restored. Take it from the --scan histogram.
  --dry-run          With --restore: print the plan, write nothing.

Examples:
  python scripts/restore_company_owners.py --scan
  python scripts/restore_company_owners.py --restore --from-owner-id 12345 --dry-run
  python scripts/restore_company_owners.py --restore --from-owner-id 12345
"""
import argparse
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _norm(s) -> str:
    return str(s or "").strip().lower()


def _load_airtable_snapshot():
    """Return {kylas_company_id(str): {"name":..., "owner_name":..., "owner_email":...}}
    from the Company List table (falls back to the CRM Companies table for
    records missing there)."""
    from utils.airtable_client import AirtableClient

    fm_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "config", "field_map.json")
    with open(fm_path) as f:
        fm_all = json.load(f)

    snap = {}
    sources = [
        ("Company List", os.environ.get("AIRTABLE_COMPANY_BASE_ID") or os.environ["AIRTABLE_BASE_ID"],
         fm_all["company"]),
        ("Companies", os.environ["AIRTABLE_BASE_ID"], fm_all["company_crm"]),
    ]
    for table_name, base_id, fm in sources:
        try:
            tbl = AirtableClient(table_name, base_id=base_id)
            records = tbl.table.all()
        except Exception as exc:
            print(f"[restore] WARN: could not read Airtable {table_name}: {exc}")
            continue
        added = 0
        for r in records:
            f = r.get("fields", {})
            kid = str(f.get(fm["id"], "")).strip()
            if not kid or kid in snap:
                continue
            snap[kid] = {
                "name":        str(f.get(fm["name"], "")).strip(),
                "owner_name":  str(f.get(fm["assignedTo"], "")).strip(),
                "owner_email": _norm(f.get(fm.get("ownerEmail", "Owner Email"), "")),
            }
            added += 1
        print(f"[restore] Airtable {table_name}: {added} companies added to snapshot")
    return snap


def _build_user_maps(kylas):
    """Return (uid_to_name, email_to_uid, name_to_uid) merging team.json + Kylas API."""
    uid_to_name = {}
    name_to_uid = {}
    email_to_uid = {}

    tp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "config", "team.json")
    try:
        with open(tp) as f:
            team = json.load(f)
        for uid, name in (team.get("kylas_users") or {}).items():
            uid_to_name[str(uid)] = name
            name_to_uid.setdefault(_norm(name), int(uid))
        emails = team.get("kylas_user_emails") or {}
        for name, email in emails.items():
            if _norm(name) in name_to_uid:
                email_to_uid.setdefault(_norm(email), name_to_uid[_norm(name)])
    except Exception as exc:
        print(f"[restore] WARN: team.json not usable: {exc}")

    try:
        for uid, name in (kylas.get_users() or {}).items():
            uid_to_name[str(uid)] = name
            name_to_uid.setdefault(_norm(name), int(uid))
    except Exception as exc:
        print(f"[restore] WARN: get_users failed: {exc}")
    try:
        for email, uid in (kylas.get_users_by_email() or {}).items():
            email_to_uid.setdefault(_norm(email), int(uid))
    except Exception as exc:
        print(f"[restore] WARN: get_users_by_email failed: {exc}")

    print(f"[restore] User maps: {len(uid_to_name)} ids, {len(email_to_uid)} emails")
    return uid_to_name, email_to_uid, name_to_uid


def main():
    ap = argparse.ArgumentParser(description="Restore Kylas company owners from Airtable snapshot")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--scan",    action="store_true", help="Read-only mismatch report")
    mode.add_argument("--restore", action="store_true", help="Restore owners (see --from-owner-id)")
    ap.add_argument("--from-owner-id", type=int, metavar="UID",
                    help="Only restore companies currently owned by this uid (the API user)")
    ap.add_argument("--dry-run", action="store_true", help="With --restore: print plan, write nothing")
    args = ap.parse_args()

    if args.restore and not args.from_owner_id:
        ap.error("--restore requires --from-owner-id (run --scan first to identify it)")

    from dotenv import load_dotenv
    load_dotenv()
    from utils.kylas_client import KylasClient

    kylas = KylasClient()

    print("[restore] Loading Airtable owner snapshot...")
    snap = _load_airtable_snapshot()
    print(f"[restore] Snapshot: {len(snap)} companies with Kylas ids")

    uid_to_name, email_to_uid, name_to_uid = _build_user_maps(kylas)

    print("[restore] Fetching all Kylas companies (id, name, ownedBy)...")
    companies = kylas._search_all("company", fields=["id", "name", "ownedBy", "updatedAt"])
    print(f"[restore] {len(companies)} companies fetched")

    # ── Compare current owner vs snapshot ─────────────────────────────────────
    API_USER_UID = 74725     # Enout Super Admin — the account any ownerless PUT lands on
    api_owned = []           # (co_id, name, in_snapshot, resolvable_target_uid, target_name)

    mismatches = []      # (co_id, co_name, cur_uid, cur_name, want_name, want_email, want_uid)
    no_snapshot = 0
    for co in companies:
        co_id = co.get("id")
        ob = co.get("ownedBy") or {}
        cur_uid  = ob.get("id") if isinstance(ob, dict) else None
        cur_name = (ob.get("name") if isinstance(ob, dict) else str(ob or "")) or ""

        # Census: every company CURRENTLY owned by the API user, snapshot or not.
        if str(cur_uid) == str(API_USER_UID):
            s2 = snap.get(str(co_id)) or {}
            wn = s2.get("owner_name", "")
            tgt = None
            if wn and wn.lower() != "unassigned" and _norm(wn) != _norm(cur_name):
                tgt = (email_to_uid.get(s2.get("owner_email", ""))
                       or name_to_uid.get(_norm(wn))
                       or (email_to_uid.get(_norm(wn)) if "@" in wn else None))
            api_owned.append((co_id, co.get("name", ""), bool(s2), tgt, wn))

        s = snap.get(str(co_id))
        if not s:
            no_snapshot += 1
            continue
        want_name = s["owner_name"]
        if not want_name or want_name.lower() == "unassigned":
            continue                     # snapshot has no owner to restore to
        if _norm(cur_name) == _norm(want_name):
            continue                     # owner intact
        want_uid = (email_to_uid.get(s["owner_email"])
                    or name_to_uid.get(_norm(want_name))
                    # some snapshots hold an email in the owner-name column
                    or (email_to_uid.get(_norm(want_name)) if "@" in want_name else None))
        mismatches.append((co_id, co.get("name", ""), cur_uid, cur_name,
                           want_name, s["owner_email"], want_uid))

    print(f"\n[restore] {len(mismatches)} companies whose current Kylas owner "
          f"differs from the Airtable snapshot ({no_snapshot} companies not in snapshot)")

    hist = Counter((m[2], m[3]) for m in mismatches)
    print("\n[restore] Current owner of mismatched companies (histogram):")
    for (uid, name), cnt in hist.most_common(10):
        print(f"  uid={uid:<10} {name:<30} {cnt} companies")

    # ── Census of companies CURRENTLY owned by the API user (the real damage) ──
    restorable = [a for a in api_owned if a[3]]
    orphan     = [a for a in api_owned if not a[3]]
    print(f"\n[restore] {len(api_owned)} companies CURRENTLY owned by the API user "
          f"(Enout Super Admin, uid {API_USER_UID}):")
    print(f"[restore]   {len(restorable)} have a known prior BD owner in the snapshot "
          f"(auto-restorable)")
    print(f"[restore]   {len(orphan)} have NO restorable owner "
          f"(new/ownerless imports — need a manual owner decision)")
    if orphan:
        print("[restore]   orphan (currently Enout, no snapshot owner) — first 40:")
        for co_id, nm, in_snap, _t, _wn in orphan[:40]:
            print(f"      {co_id} | {nm[:45]:<45} | in_snapshot={in_snap}")

    if args.scan:
        print("\n[restore] Detail (company_id | company | current_owner -> snapshot_owner [target_uid]):")
        for co_id, co_name, cur_uid, cur_name, want_name, want_email, want_uid in mismatches:
            tag = f"uid:{want_uid}" if want_uid else "UNRESOLVED"
            print(f"  {co_id} | {co_name[:40]:<40} | {cur_name} -> {want_name} [{tag}]")
        unresolved = [m for m in mismatches if not m[6]]
        if unresolved:
            print(f"\n[restore] WARNING: {len(unresolved)} snapshot owners could not be "
                  f"resolved to a Kylas uid — fix team.json or Kylas users before --restore")
        print("\n[restore] Scan complete (read-only). To restore, re-run with:")
        if hist:
            top_uid = hist.most_common(1)[0][0][0]
            print(f"  --restore --from-owner-id {top_uid} --dry-run   (then without --dry-run)")
        return

    # ── Restore ───────────────────────────────────────────────────────────────
    targets = [m for m in mismatches if str(m[2]) == str(args.from_owner_id)]
    print(f"\n[restore] {len(targets)} companies currently owned by uid {args.from_owner_id} "
          f"({uid_to_name.get(str(args.from_owner_id), '?')}) will be restored "
          f"({'DRY RUN' if args.dry_run else 'LIVE'})")

    restored = failed = skipped = 0
    for i, (co_id, co_name, cur_uid, cur_name, want_name, want_email, want_uid) in enumerate(targets, 1):
        if not want_uid:
            skipped += 1
            print(f"  [{i:>4}/{len(targets)}] {co_id} {co_name[:35]}: SKIP — "
                  f"cannot resolve {want_name!r} to a Kylas uid")
            continue
        if args.dry_run:
            restored += 1
            print(f"  [{i:>4}/{len(targets)}] {co_id} {co_name[:35]}: would restore "
                  f"{cur_name} -> {want_name} (uid {want_uid})")
            continue
        ok = kylas.update_company_owner(int(co_id), int(want_uid))
        if ok:
            restored += 1
            print(f"  [{i:>4}/{len(targets)}] {co_id} {co_name[:35]}: RESTORED -> "
                  f"{want_name} (uid {want_uid})")
        else:
            failed += 1
            print(f"  [{i:>4}/{len(targets)}] {co_id} {co_name[:35]}: FAILED")

    print(f"\n[restore] Done: restored={restored} failed={failed} "
          f"unresolved_skipped={skipped} ({'dry run — nothing written' if args.dry_run else 'live'})")


if __name__ == "__main__":
    main()
