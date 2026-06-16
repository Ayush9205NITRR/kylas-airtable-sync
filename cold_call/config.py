"""
Central configuration & shared helpers for the Cold Call Analysis System.

Every secret comes from an environment variable (see .env.example) — nothing is
hardcoded. Import this module to read settings, or use the small helpers for
IST-aware dates and audio-format checks.
"""
import os
from datetime import datetime, timedelta, timezone

# ── Timezone ───────────────────────────────────────────────────────────────────
IST = timezone(timedelta(hours=5, minutes=30))

# ── Audio handling ───────────────────────────────────────────────────────────────
# Includes WhatsApp/voice-note formats (.mpeg/.mpga/.opus) — HF Whisper decodes
# all of these via ffmpeg server-side.
SUPPORTED_FORMATS = {".mp4", ".m4a", ".mp3", ".wav", ".ogg", ".aac",
                     ".mpeg", ".mpga", ".opus", ".flac", ".webm"}
MIN_DURATION_SECONDS = 10
# The HF inference API is unhappy with very large request bodies; whisper-small
# is comfortable under ~25 MB. Bigger files are split into chunks first.
MAX_HF_BYTES = 25 * 1024 * 1024
CHUNK_SECONDS = 60

# ── External services ─────────────────────────────────────────────────────────────
HF_WHISPER_MODEL = os.environ.get("HF_WHISPER_MODEL", "openai/whisper-small")
# HF retired api-inference.huggingface.co; serverless inference is now served via
# the router (hf-inference provider). Override the whole URL with HF_API_URL if needed.
HF_API_URL = os.environ.get(
    "HF_API_URL",
    f"https://router.huggingface.co/hf-inference/models/{HF_WHISPER_MODEL}",
)
# Gemini 1.5 models are being retired and aren't available on newer API keys, so
# default to a current free-tier flash model. Override with GEMINI_MODEL — run
# `python cold_call/analyze.py --list-models` to see what your key exposes.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
# Gemini free tier = 15 req/min; a 4s gap between calls keeps us safely under.
GEMINI_DELAY_SECONDS = float(os.environ.get("GEMINI_DELAY_SECONDS", "4"))

# ── Airtable ───────────────────────────────────────────────────────────────────────
# Reuse the repo's existing PAT var; fall back to the brief's AIRTABLE_TOKEN name.
AIRTABLE_TOKEN = os.environ.get("AIRTABLE_PAT") or os.environ.get("AIRTABLE_TOKEN", "")
# Cold-call data lives in its OWN base so it never touches the Kylas CRM base.
# Fall back to AIRTABLE_BASE_ID only if a dedicated base id isn't provided.
AIRTABLE_BASE_ID = (
    os.environ.get("COLD_CALL_AIRTABLE_BASE_ID")
    or os.environ.get("AIRTABLE_BASE_ID", "")
)
TABLE_NAME = os.environ.get("COLD_CALL_TABLE_NAME", "Calls")

# ── Email (SMTP) ───────────────────────────────────────────────────────────────────
# Reuses the same Gmail SMTP credentials as the Kylas sync (modules/04_email_alert.py).
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
# Optional From override; defaults to the SMTP user (Gmail requires this anyway).
EMAIL_FROM = os.environ.get("COLD_CALL_FROM_EMAIL") or SMTP_USER

# ── Google Drive ───────────────────────────────────────────────────────────────────
# Id of the top-level `calls/` folder (each child folder = one BD). Defaults to the
# Enout calls folder; override with GOOGLE_DRIVE_FOLDER_ID if it ever moves.
DRIVE_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_FOLDER_ID") or "1tDvWUO-LM37aVIghIjyihVD1f8wXfxM8"
# Either a path to a service-account JSON key file, or the raw JSON string
# (handy for CI, where the whole key lives in a single secret).
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")


def today_ist():
    """Today's date in IST — the pipeline's notion of 'today'."""
    return datetime.now(IST).date()


def ist_day_start_utc(day=None) -> datetime:
    """Midnight IST for `day` (default: today IST) as an aware UTC datetime.

    Used to build the Drive ``modifiedTime >=`` filter so we only pick up files
    uploaded on/after 00:00 IST of the target day.
    """
    day = day or today_ist()
    start_ist = datetime(day.year, day.month, day.day, tzinfo=IST)
    return start_ist.astimezone(timezone.utc)


def now_ist_iso() -> str:
    """Current IST timestamp as ISO 8601 with offset (for `processed_at`)."""
    return datetime.now(IST).isoformat(timespec="seconds")


def is_supported(filename: str) -> bool:
    return os.path.splitext(filename)[1].lower() in SUPPORTED_FORMATS
