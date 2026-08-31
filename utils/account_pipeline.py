"""
Account Pipeline Stage (BD) — the granular account-level tracker.

For each company we look at all of its contacts, read each contact's
Pipeline Stage (BD), and give the account the BEST stage any single contact
has reached. "Best" is defined by config/account_pipeline_order.json, where
rank 1 is best.

This is deliberately separate from Account Health (BD), the higher-level
tracker in modules/06_account_health.py. Account Health answers "what shape is
this account in?" and has its own priority (Offsite Delayed > Offsite Done >
SQL > ...). This answers the narrower "how far has anyone at this account
actually got?" — so SQL ranks first here and Offsite Delayed sits at 12. The
two orders disagree on purpose; do not unify them.

Stage names are compared after normalization (see _norm), because the same
stage is spelled inconsistently across Kylas and this repo: en-dashes vs
hyphens, "Organisation" vs "Organization", and spacing around separators.
"""
import json
import os
import re

_CFG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "account_pipeline_order.json",
)

# Unicode dash zoo: hyphen, non-breaking hyphen, figure dash, en dash, em dash,
# horizontal bar, minus sign. Kylas and hand-typed configs mix these freely.
_DASHES = "‐‑‒–—―−"


def _norm(s) -> str:
    """
    Fold a stage name to a comparable key.

    Collapses the dash variants to '-', drops spacing around separators so
    "Closing Loops - Low Value" and "Closing Loops–Low Value" agree, and folds
    British "Organisation" to "Organization" (the static map in bd_metrics.py
    uses the British spelling, the live Kylas picklist uses the American one).
    """
    s = str(s or "").strip().lower()
    for d in _DASHES:
        s = s.replace(d, "-")
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s*-\s*", "-", s)
    return s.replace("organisation", "organization")


class AccountPipelineOrder:
    """Rank lookup built from config/account_pipeline_order.json."""

    def __init__(self, cfg: dict):
        self.order = list(cfg.get("order") or [])
        if not self.order:
            raise ValueError("account_pipeline_order.json has an empty 'order'")

        # rank is 1-based: rank 1 is the best stage.
        self.label_by_rank = {i + 1: lbl for i, lbl in enumerate(self.order)}
        self.rank_by_norm = {_norm(lbl): i + 1 for i, lbl in enumerate(self.order)}

        dupes = len(self.order) - len(self.rank_by_norm)
        if dupes:
            raise ValueError(
                f"account_pipeline_order.json 'order' has {dupes} duplicate "
                f"stage name(s) after normalization — ranks must be unique"
            )

        for alias, target in (cfg.get("aliases") or {}).items():
            rank = self.rank_by_norm.get(_norm(target))
            if rank is None:
                raise ValueError(
                    f"account_pipeline_order.json alias {alias!r} points at "
                    f"{target!r}, which is not in 'order'"
                )
            self.rank_by_norm[_norm(alias)] = rank

        self.unranked = {_norm(u) for u in (cfg.get("unranked") or [])}

        # Stage names seen at run time that we could not rank. Surfaced by
        # report_unranked() so a Kylas rename shows up as a loud warning
        # instead of silently pushing every account down to blank.
        self.unknown_seen = {}

    def rank_of(self, stage) -> int:
        """
        Rank for a stage name, or 0 if it does not rank.

        0 means "never wins the account" — it covers both deliberately
        unranked stages (Yet to Be Mined) and names we do not recognise.
        Unrecognised names are recorded for reporting; unranked ones are not.
        """
        key = _norm(stage)
        if not key:
            return 0
        rank = self.rank_by_norm.get(key)
        if rank:
            return rank
        if key not in self.unranked:
            self.unknown_seen[key] = self.unknown_seen.get(key, 0) + 1
        return 0

    def best(self, stages) -> tuple:
        """
        Best (label, rank) across an iterable of stage names.

        Returns ("", 0) when nothing ranks — no contacts, all Yet to Be Mined,
        or every stage unrecognised. The caller decides what a blank means.
        """
        best_rank = 0
        for s in stages:
            r = self.rank_of(s)
            if r and (best_rank == 0 or r < best_rank):
                best_rank = r
        return (self.label_by_rank.get(best_rank, ""), best_rank)

    def report_unranked(self, limit: int = 15) -> None:
        """Print any stage names we could not rank, worst offenders first."""
        if not self.unknown_seen:
            return
        total = sum(self.unknown_seen.values())
        print(f"  [account_pipeline] WARNING: {len(self.unknown_seen)} stage name(s) "
              f"across {total} contact(s) did not match any rank and were IGNORED. "
              f"Add them to 'order', 'aliases' or 'unranked' in "
              f"config/account_pipeline_order.json:")
        for key, n in sorted(self.unknown_seen.items(), key=lambda kv: -kv[1])[:limit]:
            print(f"  [account_pipeline]   {key!r} — {n} contact(s)")


def load_order(path: str = None) -> AccountPipelineOrder:
    with open(path or _CFG_PATH, encoding="utf-8") as fh:
        return AccountPipelineOrder(json.load(fh))


def _company_id(ct: dict) -> str:
    """
    Company id off a raw Kylas contact.

    Kylas returns 'company' as a bare int on search results but as a nested
    object on detail reads — compute_health has the same dual handling.
    """
    co = ct.get("company")
    if isinstance(co, (int, float)):
        return str(int(co))
    if isinstance(co, dict):
        return str(co.get("id", "")) or ""
    return ""


def compute_account_pipeline(contacts: list, order: AccountPipelineOrder = None,
                             stage_of=None) -> dict:
    """
    contacts: raw Kylas contact dicts.
    stage_of: callable(contact) -> stage name. Defaults to bd_metrics.contact_stage,
              which resolves the bare integer option ids Kylas search returns.

    Returns {company_id (str): {"stage": label, "rank": int}}. Companies whose
    contacts never rank are present with stage "" — the caller distinguishes
    "has contacts, none ranked" from "no contacts at all", which this cannot see.
    """
    if order is None:
        order = load_order()
    if stage_of is None:
        from utils.bd_metrics import contact_stage as stage_of

    best = {}
    for ct in contacts:
        cid = _company_id(ct)
        if not cid:
            continue
        rank = order.rank_of(stage_of(ct))
        cur = best.get(cid)
        if cur is None:
            best[cid] = rank
        elif rank and (cur == 0 or rank < cur):
            best[cid] = rank

    return {cid: {"stage": order.label_by_rank.get(r, ""), "rank": r}
            for cid, r in best.items()}
