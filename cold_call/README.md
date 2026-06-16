# Cold Call Analysis System (Enout · Phase 1)

A daily pipeline that turns BD sales-call recordings into coaching feedback.

```
Google Drive  →  Whisper (HF)  →  Gemini 1.5 Flash  →  Airtable  →  SMTP email
  (audio)         transcript        4-param score        Calls         per BD
```

Each BD drops audio into their own Drive sub-folder. Once a day the pipeline
picks up the new files, transcribes them, scores each call on 4 parameters
(Hook / Objection Handling / Enout Pitch / Discovery Booked), stores everything
in Airtable, and emails each BD a coaching summary.

## Drive folder layout

```
<calls folder>/          ← GOOGLE_DRIVE_FOLDER_ID points here
├── Priya/               ← folder name = BD name (must match config/team.json)
│   ├── call_001.mp4
│   └── rec_20240615.m4a
├── Rahul/
└── Amit/
```

Accepted formats: `.mp4 .m4a .mp3 .wav .ogg .aac .mpeg .mpga .opus .flac .webm`
(covers WhatsApp audio / voice notes). Files modified on/after 00:00 IST of the
run day are picked up.

## Layout

| File | Purpose |
|------|---------|
| `config.py` | env-driven settings + IST/date helpers |
| `drive.py` | list + download new Drive files (service account) |
| `transcribe.py` | Whisper via HF Inference API (503 retry, chunking) |
| `prompt.py` | the Gemini coaching system prompt |
| `analyze.py` | Gemini call → scorecard JSON (robust parsing) |
| `airtable_store.py` | duplicate check + insert into `Calls` |
| `email_coach.py` | per-BD coaching email via SMTP (Gmail) |
| `pipeline.py` | orchestrates the whole daily run |

## Setup

```bash
pip install -r cold_call/requirements.txt    # ffmpeg also needed for >25 MB files
cp .env.example .env                          # fill in the COLD CALL keys

# One-time: create the Airtable `Calls` table in the cold-call base
python scripts/setup_cold_call_airtable.py
```

### Environment variables

| Var | Notes |
|-----|-------|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | path to the key file **or** the raw JSON |
| `GOOGLE_DRIVE_FOLDER_ID` | optional — defaults to the Enout `calls/` folder in `config.py` |
| `HF_API_TOKEN` | Hugging Face token (Whisper) |
| `GEMINI_API_KEY` | Gemini API key |
| `AIRTABLE_PAT` | Airtable token (shared with the Kylas sync) |
| `COLD_CALL_AIRTABLE_BASE_ID` | **dedicated** base for the `Calls` table |
| `SMTP_USER` | Gmail address used to send coaching emails |
| `SMTP_PASS` | Gmail **App Password** (2FA must be on) |

Emails go out over Gmail SMTP (`smtp.gmail.com:587`), the same mechanism the
Kylas sync uses — so if `SMTP_USER`/`SMTP_PASS` are already set for that, the
cold-call coach reuses them. Optional `COLD_CALL_FROM_EMAIL` overrides the From.

> `COLD_CALL_AIRTABLE_BASE_ID` is separate from the Kylas sync's
> `AIRTABLE_BASE_ID` so the two systems never share a base. If it's unset the
> code falls back to `AIRTABLE_BASE_ID` — fine for local testing, but in
> production give cold-call its own base.

BD → email mapping is read from `config/team.json` (`bd_team`), so Drive folder
names should match the BD names there.

## Running

```bash
python -m cold_call.pipeline                 # today's files, full run
python -m cold_call.pipeline --dry-run       # just list what would be processed
python -m cold_call.pipeline --test          # first 3 files, no email
python -m cold_call.pipeline --date 2026-06-15 --bd Priya --no-email
```

Individual stages can be exercised on their own:

```bash
python -m cold_call.drive                     # list today's new files
python cold_call/transcribe.py path/to.m4a    # transcribe one file
python cold_call/email_coach.py --out /tmp/sample.html   # render a sample email
python tests/test_cold_call.py                # offline unit tests (no keys)
```

## Error handling

| Situation | Action |
|-----------|--------|
| Unsupported format | logged in Airtable as `status=error` |
| Duration < 10s | logged as `status=too_short` |
| Already processed | skipped silently (dup check on bd_name + filename) |
| Whisper 503 (cold start) | wait 20s, retry once |
| File > 25 MB | split into 60s chunks (needs ffmpeg) |
| Gemini returns non-JSON | raw logged, `status=error` |
| Airtable insert fails | warning printed, pipeline continues |
| No calls for a BD | no email sent |

## Scheduling

`.github/workflows/cold_call_daily.yml` runs at 8:30 PM IST (Mon–Sat) and can
also be triggered manually with `date` / `limit` / `no_email` inputs. It needs
the same keys as above stored as GitHub Actions secrets.
