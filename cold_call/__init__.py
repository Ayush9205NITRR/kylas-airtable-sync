"""Cold Call Analysis System (Enout · Phase 1).

A daily pipeline that pulls BD sales-call recordings from Google Drive,
transcribes them (Gemini), scores them on 4 parameters
(Gemini), stores the results in Airtable, and emails each BD a coaching
summary (SMTP).

Entry point: ``python -m cold_call.pipeline`` (see pipeline.py).
"""
