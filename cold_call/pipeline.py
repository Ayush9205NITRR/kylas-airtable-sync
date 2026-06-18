"""
Cold Call Analysis — daily pipeline.

  1. Fetch today's new audio files from Drive (per BD sub-folder)
  2. Skip unsupported formats / duplicates / clips < 10s
  3. Transcribe + analyze with Gemini -> store (Airtable)
  4. Send one coaching email per BD (SMTP)

Run:
    python -m cold_call.pipeline                 (or: python cold_call/pipeline.py)
    python -m cold_call.pipeline --dry-run       (list files only)
    python -m cold_call.pipeline --test          (first 3 files, no email)
    python -m cold_call.pipeline --date 2026-06-15 --bd Priya --no-email
"""
import argparse
import os
import sys
import tempfile
import time
import traceback
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cold_call import config


def _ffprobe_duration(path: str):
    """Duration via ffprobe (reliable across containers; ships with ffmpeg)."""
    import subprocess
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30,
        )
        val = (out.stdout or "").strip()
        return float(val) if val else None
    except Exception:
        return None


def _safe_duration(path: str):
    """Audio length in seconds. Tries mutagen, then ffprobe; None if unknown.

    Some containers (e.g. WhatsApp .mp4) make mutagen report 0.0 — we don't trust
    a 0.0 and fall back to ffprobe so real calls aren't wrongly marked too_short.
    """
    try:
        from mutagen import File as MutagenFile
        audio = MutagenFile(path)
        if audio is not None and getattr(audio, "info", None) is not None:
            length = float(audio.info.length or 0)
            if length > 0:
                return length
    except Exception:
        pass
    return _ffprobe_duration(path)


def _format_objections(objs) -> str:
    """Readable, distilled objection list for the Airtable cell (not raw JSON)."""
    if not objs:
        return ""
    lines = []
    for i, o in enumerate(objs, 1):
        if not isinstance(o, dict):
            lines.append(f"{i}. {o}")
            continue
        tag = " · ".join(x for x in [(o.get("type") or "").strip(),
                                     (o.get("handled") or "").strip()] if x)
        head = (o.get("objection") or "").strip()
        lines.append(f"{i}. [{tag}] {head}" if tag else f"{i}. {head}")
        rep = (o.get("rep_response") or "").strip()
        if rep:
            lines.append(f"   Rep: {rep}")
        better = (o.get("better_response") or "").strip()
        if better:
            lines.append(f"   Better: {better}")
    return "\n".join(lines)


def _build_record(bd, fname, day_iso, duration, transcript, a) -> dict:
    """Map a Gemini analysis dict to Airtable fields.

    `total_score` is intentionally omitted — it's a computed formula field.
    """
    return {
        "bd_name": bd,
        "audio_filename": fname,
        "call_date": day_iso,
        "duration_seconds": duration,
        "transcript": transcript,
        "hook_score": a.get("hook_score"),
        "hook_feedback": a.get("hook_feedback", ""),
        "hook_better_line": a.get("hook_better_line", ""),
        "objection_score": a.get("objection_score"),
        "objections_list": _format_objections(a.get("objections_found", [])),
        "objection_feedback": a.get("objection_feedback", ""),
        "pitch_score": a.get("pitch_score"),
        "pitch_feedback": a.get("pitch_feedback", ""),
        "pitch_better_version": a.get("pitch_better_version", ""),
        "discovery_score": a.get("discovery_score"),
        "discovery_outcome": a.get("discovery_outcome", ""),
        "discovery_feedback": a.get("discovery_feedback", ""),
        "top_miss": a.get("top_miss", ""),
        "call_language": a.get("call_language", ""),
        "status": "processed",
        "processed_at": config.now_ist_iso(),
    }


def _safe_insert(store, fields: dict) -> None:
    try:
        store.insert_record(fields)
    except Exception as exc:
        print(f"  WARNING: Airtable insert failed: {exc}")


def run_pipeline(target_day=None, limit=None, send_email=True,
                 only_bd=None, dry_run=False, reprocess=False) -> dict:
    from cold_call import drive, transcribe, analyze, airtable_store, email_coach, patterns

    run_day = target_day or config.today_ist()
    day_iso = run_day.isoformat()
    window = f"day {day_iso}" if target_day else f"last {config.LOOKBACK_HOURS}h"
    print(f"=== Cold-call pipeline · {day_iso} · window: {window}"
          + ("  · REPROCESS (dedup off)" if reprocess else "") + " ===")

    # target_day=None -> drive uses the rolling lookback window.
    files = drive.fetch_new_files(target_day)
    if only_bd:
        files = [f for f in files if f["bd_name"].lower() == only_bd.lower()]
    if limit:
        files = files[:limit]
    print(f"Found {len(files)} candidate file(s)")

    if dry_run:
        for f in files:
            print(f"  would process: {f['bd_name']} / {f['filename']}")
        return {"counts": {}, "results_by_bd": {}}

    results_by_bd = {}
    counts = {"processed": 0, "too_short": 0, "error": 0, "skipped": 0, "unsupported": 0}

    for f in files:
        bd, fname = f["bd_name"], f["filename"]

        # Format check (defensive — fetch already filters by extension)
        if not config.is_supported(fname):
            print(f"SKIP {bd}/{fname} — unsupported format")
            counts["unsupported"] += 1
            _safe_insert(airtable_store, {
                "bd_name": bd, "audio_filename": fname, "call_date": day_iso,
                "status": "error", "top_miss": "unsupported format",
                "processed_at": config.now_ist_iso(),
            })
            continue

        # Duplicate check (disabled in --reprocess mode so existing files re-run)
        if not reprocess:
            try:
                if airtable_store.check_duplicate(bd, fname):
                    print(f"SKIP {bd}/{fname} — already processed")
                    counts["skipped"] += 1
                    continue
            except Exception as exc:
                print(f"  WARNING: duplicate check failed for {fname} ({exc}) — continuing")

        print(f"Processing: {bd} / {fname}")
        ext = os.path.splitext(fname)[1].lower()
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                tmp_path = tmp.name
            drive.download_file(f["drive_id"], tmp_path)

            # Duration filter
            duration = _safe_duration(tmp_path)
            if duration is None:
                print("  WARNING: could not read duration — assuming long enough")
                duration = float(config.MIN_DURATION_SECONDS)
            if duration < config.MIN_DURATION_SECONDS:
                print(f"  SKIP — too short ({duration:.1f}s)")
                counts["too_short"] += 1
                _safe_insert(airtable_store, {
                    "bd_name": bd, "audio_filename": fname, "call_date": day_iso,
                    "duration_seconds": int(duration), "status": "too_short",
                    "processed_at": config.now_ist_iso(),
                })
                continue

            # Transcribe
            print(f"  Transcribing ({duration:.0f}s)...")
            transcript = transcribe.transcribe(tmp_path)
            if not transcript.strip():
                raise RuntimeError("empty transcript from Gemini")

            # Analyze
            print("  Analyzing...")
            analysis = analyze.analyze_call(transcript, bd, day_iso)

            # Store
            airtable_store.insert_record(
                _build_record(bd, fname, day_iso, int(duration), transcript, analysis)
            )
            counts["processed"] += 1
            results_by_bd.setdefault(bd, []).append(analysis)
            print(f"  Stored — total {analysis.get('total_score', '?')}/100")

            time.sleep(config.GEMINI_DELAY_SECONDS)  # respect Gemini rate limit

        except Exception as exc:
            counts["error"] += 1
            print(f"  ERROR processing {fname}: {exc}")
            traceback.print_exc()
            _safe_insert(airtable_store, {
                "bd_name": bd, "audio_filename": fname, "call_date": day_iso,
                "status": "error", "top_miss": str(exc)[:500],
                "processed_at": config.now_ist_iso(),
            })
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)

    # Pattern report per BD — the headline output (where the rep keeps going wrong)
    pattern_reports = {}
    if results_by_bd:
        print("\n" + "=" * 12 + " PATTERNS " + "=" * 12)
    for bd, calls in results_by_bd.items():
        try:
            report = patterns.build_pattern_report(bd, calls)
            pattern_reports[bd] = report
            print(patterns.format_text(bd, report))
        except Exception as exc:
            print(f"  WARNING: pattern analysis failed for {bd}: {exc}")

    # Coaching emails — one per BD that had at least one processed call
    if send_email:
        print("Sending coaching emails...")
        for bd, calls in results_by_bd.items():
            try:
                email_coach.send_coaching_email(bd, None, calls)
            except Exception as exc:
                print(f"  WARNING: email to {bd} failed: {exc}")
    else:
        print("Email sending disabled")

    print(f"=== Done · {counts} ===")
    return {"counts": counts, "results_by_bd": results_by_bd,
            "pattern_reports": pattern_reports}


def main():
    parser = argparse.ArgumentParser(description="Cold Call Analysis daily pipeline")
    parser.add_argument("--date", help="target day YYYY-MM-DD (default: today IST)")
    parser.add_argument("--limit", type=int, help="process at most N files")
    parser.add_argument("--bd", help="only this BD (folder name)")
    parser.add_argument("--no-email", action="store_true", help="skip coaching emails")
    parser.add_argument("--dry-run", action="store_true", help="list files, don't process")
    parser.add_argument("--reprocess", action="store_true",
                        help="ignore the duplicate check and re-analyze matching files")
    parser.add_argument("--test", action="store_true",
                        help="alias for --limit 3 --no-email")
    args = parser.parse_args()

    from dotenv import load_dotenv
    load_dotenv()

    target = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else None
    limit = args.limit
    send_email = not args.no_email
    if args.test:
        limit = limit or 3
        send_email = False

    run_pipeline(target_day=target, limit=limit, send_email=send_email,
                 only_bd=args.bd, dry_run=args.dry_run, reprocess=args.reprocess)


if __name__ == "__main__":
    main()
