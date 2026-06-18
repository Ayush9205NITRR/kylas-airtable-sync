"""
Pattern analysis per BD — the headline output.

Instead of per-call feedback prose, we surface WHERE a rep is consistently going
wrong across their calls, in three areas: Hook / Objections raised / Objection
handling. We combine grounded deterministic stats (no hallucination) with a short
Gemini synthesis. Detailed per-call feedback is reviewed separately in Airtable.
"""
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cold_call import config

HOOK_MAX = 25
WEAK_HOOK_BELOW = 15  # below ~60% of 25 = a weak opening

PATTERNS_PROMPT = """
You are a sales-coaching analyst. You are given a compact summary of ONE rep's recent cold calls.
Identify the RECURRING PATTERN of mistakes (not per-call feedback) in exactly three areas:
hook/opening, objections raised by prospects, and objection handling.
Base everything ONLY on the data provided — do not invent anything.
Return ONLY a JSON object, no markdown:
{
  "hook_pattern": "<1-2 sentences: where the rep consistently goes wrong on openings, or 'No clear recurring issue'>",
  "objection_pattern": "<1-2 sentences: which objections keep coming up>",
  "handling_pattern": "<1-2 sentences: how the rep consistently mishandles objections, or 'No clear recurring issue'>"
}
Write in clear, professional English. Be concrete and concise.
""".strip()


def aggregate(calls: list) -> dict:
    """Grounded, deterministic stats across a rep's calls."""
    n = len(calls)
    hooks = [int(c.get("hook_score") or 0) for c in calls]
    obj_types, handled, misses = Counter(), Counter(), []
    for c in calls:
        for o in (c.get("objections_found") or []):
            obj_types[(o.get("type") or "other").strip().lower() or "other"] += 1
            h = (o.get("handled") or "").strip().lower()
            if h:
                handled[h] += 1
        m = (c.get("top_miss") or "").strip()
        if m:
            misses.append(m)
    total_obj = sum(handled.values())
    weak_missed = handled.get("weak", 0) + handled.get("missed", 0)
    return {
        "n_calls": n,
        "avg_hook": round(sum(hooks) / n, 1) if n else 0,
        "weak_hooks": sum(1 for h in hooks if h < WEAK_HOOK_BELOW),
        "objection_types": obj_types.most_common(),
        "handled": dict(handled),
        "total_objections": total_obj,
        "weak_missed_pct": round(100 * weak_missed / total_obj) if total_obj else 0,
        "common_miss": Counter(misses).most_common(1)[0][0] if misses else "",
    }


def _digest(calls: list) -> str:
    lines = []
    for i, c in enumerate(calls, 1):
        objs = "; ".join(
            f"{(o.get('type') or 'other')}/{(o.get('handled') or '?')}"
            for o in (c.get("objections_found") or [])
        ) or "none"
        lines.append(f"Call {i}: hook={c.get('hook_score', '?')}/25; "
                     f"objections=[{objs}]; top_miss={c.get('top_miss', '')}")
    return "\n".join(lines)


def synthesize(bd_name: str, calls: list, agg: dict) -> dict:
    """Short Gemini synthesis of the three patterns. Returns {} on any failure."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return {}
    try:
        import google.generativeai as genai
        from cold_call.analyze import _slice_braces, _strip_fences
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(config.GEMINI_MODEL, system_instruction=PATTERNS_PROMPT)
        user = (f"Rep: {bd_name}\nCalls analyzed: {agg['n_calls']}\n"
                f"Aggregate stats: {agg}\n\nPer-call summary:\n{_digest(calls)}")
        raw = (getattr(model.generate_content(user), "text", "") or "").strip()
        for cand in (_strip_fences(raw), _slice_braces(_strip_fences(raw))):
            if cand:
                try:
                    return json.loads(cand)
                except json.JSONDecodeError:
                    continue
    except Exception as exc:
        print(f"  [patterns] synthesis failed ({exc}) — using stats only")
    return {}


def build_pattern_report(bd_name: str, calls: list) -> dict:
    agg = aggregate(calls)
    return {"agg": agg, "patterns": synthesize(bd_name, calls, agg)}


def format_text(bd_name: str, report: dict) -> str:
    agg = report["agg"]
    p = report.get("patterns") or {}
    types = ", ".join(f"{t}×{n}" for t, n in agg["objection_types"]) or "none"
    out = [
        f"── Patterns · {bd_name} ({agg['n_calls']} calls) ──",
        f"  Hook: avg {agg['avg_hook']}/25, weak in {agg['weak_hooks']}/{agg['n_calls']}"
        + (f"  → {p['hook_pattern']}" if p.get("hook_pattern") else ""),
        f"  Objections raised: {types}"
        + (f"  → {p['objection_pattern']}" if p.get("objection_pattern") else ""),
        f"  Objection handling: {agg['weak_missed_pct']}% weak/missed "
        f"({agg['total_objections']} total)"
        + (f"  → {p['handling_pattern']}" if p.get("handling_pattern") else ""),
    ]
    if agg["common_miss"]:
        out.append(f"  Most repeated miss: {agg['common_miss']}")
    return "\n".join(out)
