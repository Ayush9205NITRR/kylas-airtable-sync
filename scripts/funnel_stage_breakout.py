#!/usr/bin/env python3
"""
Stage-wise breakout of "Companies Reached", reconciled against BD Company
Funnel's own monthly totals.

WHAT "REACHED" MEANS
BD Company Funnel counts a company once per month, at the BEST stage any of
its contacts reached that month, and then:

    Companies Reached = Companies Worked - companies whose best stage was one
                        of CNC (Could Not Connect) - 1 / - 2 / - 3

So "reached" is the whole month's companies minus the three hard
could-not-connect stages -- it says somebody got through, not how far they
then got. This script opens that number up: for each rep and month, how many
companies sat at EACH stage, which of those stages count as reached, and
whether the reached subtotal equals what the funnel table reports.

WHY IT RECONCILES RATHER THAN RECOMPUTES
The per-company ranks come from build_funnel(with_raw=True) -- the same pass
that produces the funnel's own figures -- and the reference totals are read
back from the Airtable table. A second implementation of the counting loop
would drift from the first silently, and a reconciliation that can't fail
isn't one. Any MISMATCH row means the two genuinely disagree.

    python scripts/funnel_stage_breakout.py --months 2026-07,2026-08
    python scripts/funnel_stage_breakout.py --months 2026-08 --out-dir /tmp
"""
import argparse
import csv
import importlib.util
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.kylas_client import KylasClient      # noqa: E402
from utils.airtable_client import AirtableClient  # noqa: E402
from utils.account_pipeline import load_order   # noqa: E402


def _load_funnel():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "bd_company_funnel.py")
    spec = importlib.util.spec_from_file_location("bd_company_funnel", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def reference_totals(table: str = "BD Company Funnel") -> dict:
    """{(rep, month): {column: value}} as the Airtable table reports it."""
    out = {}
    for rec in AirtableClient(table).table.all():
        f = rec.get("fields", {})
        rep, month = str(f.get("BD Associate", "")), str(f.get("Month", ""))
        if rep and month:
            out[(rep, month)] = {c: int(f.get(c, 0) or 0)
                                 for c in ("Companies Worked", "Companies Reached")}
    return out


def breakout(by_month: dict, order, months: set) -> list:
    """[{month, rep, stage, rank, companies, reached}] — one row per stage a
    rep actually had companies at, in rank order (1 = best)."""
    rows = []
    cnc = {order.rank_of(s) for s in _FUNNEL.NOT_REACHED_STAGES} - {0}
    for (rep, _email, period), cells in by_month.items():
        if period not in months:
            continue
        per_stage = defaultdict(int)
        for _cid, rank in cells.items():
            per_stage[rank] += 1
        for rank in sorted(per_stage):
            rows.append({
                "Month": period,
                "BD Associate": rep,
                "Rank": rank,
                "Stage": order.label_by_rank.get(rank, f"(rank {rank})"),
                "Companies": per_stage[rank],
                "Counts As Reached": "no" if rank in cnc else "yes",
            })
    rows.sort(key=lambda r: (r["Month"], r["BD Associate"], r["Rank"]))
    return rows


def combine_months(by_month: dict, months: set, label: str) -> dict:
    """Fold several months into one period, each company at its BEST rank
    across them.

    A company worked in both July and August is ONE company here, not two.
    Adding the monthly rows instead would double-count it and answer a
    different question -- "company-months worked" rather than "companies
    worked" -- and would also let a company be counted at two different stages
    at once, which the funnel's own one-row-per-company-per-period rule exists
    to prevent.
    """
    merged = defaultdict(dict)
    for (rep, email, period), cells in by_month.items():
        if period not in months:
            continue
        cell = merged[(rep, email, label)]
        for cid, rank in cells.items():
            cur = cell.get(cid)
            if cur is None or rank < cur:
                cell[cid] = rank
    return dict(merged)


def reconcile(rows: list, reference: dict, months: set) -> list:
    """Per rep and month: derived vs the funnel table, and whether they agree."""
    derived = defaultdict(lambda: {"worked": 0, "reached": 0})
    for r in rows:
        cell = derived[(r["BD Associate"], r["Month"])]
        cell["worked"] += r["Companies"]
        if r["Counts As Reached"] == "yes":
            cell["reached"] += r["Companies"]

    out = []
    for (rep, month) in sorted(set(derived) | {k for k in reference if k[1] in months}):
        d = derived.get((rep, month), {"worked": 0, "reached": 0})
        ref = reference.get((rep, month))
        row = {
            "Month": month,
            "BD Associate": rep,
            "Reached (stage breakout)": d["reached"],
            "Reached (BD Company Funnel)": "" if ref is None else ref["Companies Reached"],
            "Worked (stage breakout)": d["worked"],
            "Worked (BD Company Funnel)": "" if ref is None else ref["Companies Worked"],
        }
        if ref is None:
            row["Match"] = "NO FUNNEL ROW"
        elif (d["reached"] == ref["Companies Reached"]
              and d["worked"] == ref["Companies Worked"]):
            row["Match"] = "match"
        else:
            row["Match"] = "MISMATCH"
        out.append(row)
    return out


def write_csv(path: str, rows: list, columns: list) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"[breakout] wrote {len(rows):4,} row(s) → {path}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Stage-wise breakout of Companies Reached, reconciled "
                    "against BD Company Funnel")
    ap.add_argument("--months", default="", metavar="YYYY-MM,YYYY-MM",
                    help="Months to report (default: the two most recent "
                         "closed months present in the data)")
    ap.add_argument("--out-dir", default=".", metavar="DIR")
    ap.add_argument("--combine", action="store_true",
                    help="Also emit a cumulative view treating the requested "
                         "months as ONE period, each company counted once at "
                         "its best stage across them")
    args = ap.parse_args()

    global _FUNNEL
    _FUNNEL = _load_funnel()
    order = load_order()

    print("[breakout] rebuilding the funnel (same pass it uses itself)...")
    _month_grid, _day_grid, stats = _FUNNEL.build_funnel(KylasClient(), with_raw=True)
    by_month = stats["by_month"]

    available = sorted({p for _r, _e, p in by_month})
    months = ({m.strip() for m in args.months.split(",") if m.strip()}
              if args.months else set(available[-3:-1]))
    print(f"[breakout] months available: {', '.join(available)}")
    print(f"[breakout] reporting: {', '.join(sorted(months))}")

    missing = months - set(available)
    if missing:
        print(f"[breakout] WARNING: no data at all for {', '.join(sorted(missing))}")

    rows = breakout(by_month, order, months)
    print(f"[breakout] reading 'BD Company Funnel' for the reference totals...")
    recon = reconcile(rows, reference_totals(), months)

    os.makedirs(args.out_dir, exist_ok=True)
    for month in sorted(months):
        write_csv(os.path.join(args.out_dir, f"companies_reached_breakout_{month}.csv"),
                  [r for r in rows if r["Month"] == month],
                  ["Month", "BD Associate", "Rank", "Stage", "Companies",
                   "Counts As Reached"])
    write_csv(os.path.join(args.out_dir, "companies_reached_reconciliation.csv"),
              recon, ["Month", "BD Associate", "Reached (stage breakout)",
                      "Reached (BD Company Funnel)", "Worked (stage breakout)",
                      "Worked (BD Company Funnel)", "Match"])

    print("\n" + "=" * 78)
    for month in sorted(months):
        sub = [r for r in rows if r["Month"] == month]
        reached = sum(r["Companies"] for r in sub if r["Counts As Reached"] == "yes")
        worked = sum(r["Companies"] for r in sub)
        print(f"\n{month}   worked {worked:,} | reached {reached:,} "
              f"| not reached {worked - reached:,}")
        per_stage = defaultdict(int)
        for r in sub:
            per_stage[(r["Rank"], r["Stage"], r["Counts As Reached"])] += r["Companies"]
        for (rank, stage, is_reached), n in sorted(per_stage.items()):
            flag = "  " if is_reached == "yes" else " ✗"
            print(f"   {rank:3}{flag} {stage:44} {n:5,}")

    if args.combine and len(months) > 1:
        label = f"{min(months)}..{max(months)}"
        merged = combine_months(by_month, months, label)
        crows = breakout(merged, order, {label})
        write_csv(os.path.join(args.out_dir,
                               f"companies_reached_breakout_{label}.csv"),
                  crows, ["Month", "BD Associate", "Rank", "Stage", "Companies",
                          "Counts As Reached"])
        c_worked = sum(r["Companies"] for r in crows)
        c_reached = sum(r["Companies"] for r in crows
                        if r["Counts As Reached"] == "yes")
        s_worked = sum(r["Companies"] for r in rows)
        s_reached = sum(r["Companies"] for r in rows
                        if r["Counts As Reached"] == "yes")
        print(f"\n{'=' * 78}\n\n{label}  (each company counted ONCE, at its best "
              f"stage across the period)")
        print(f"   worked {c_worked:,} | reached {c_reached:,} "
              f"| not reached {c_worked - c_reached:,}")
        print(f"   vs adding the months: worked {s_worked:,} | "
              f"reached {s_reached:,}")
        print(f"   → {s_worked - c_worked:,} company-month(s) are the SAME "
              f"company worked in both months")
        per_stage = defaultdict(int)
        for r in crows:
            per_stage[(r["Rank"], r["Stage"], r["Counts As Reached"])] += r["Companies"]
        for (rank, stage, is_reached), n in sorted(per_stage.items()):
            flag = "  " if is_reached == "yes" else " ✗"
            print(f"   {rank:3}{flag} {stage:44} {n:5,}")

    bad = [r for r in recon if r["Match"] != "match"]
    print("\n" + "=" * 78)
    if bad:
        print(f"[breakout] {len(bad)} row(s) DO NOT reconcile:")
        for r in bad:
            print(f"   {r['Month']}  {r['BD Associate']:22} "
                  f"reached {r['Reached (stage breakout)']} vs "
                  f"{r['Reached (BD Company Funnel)']}  [{r['Match']}]")
    else:
        print(f"[breakout] all {len(recon)} rep-month row(s) reconcile exactly")
    return 0


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    sys.exit(main())
