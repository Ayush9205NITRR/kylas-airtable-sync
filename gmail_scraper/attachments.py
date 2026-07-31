"""
Pull attachment bytes out of Gmail and push them into Airtable so they're
downloadable straight from the record.

Airtable's content API takes the file inline (base64) — no public URL, no
intermediate hosting. It caps a single upload at 5 MB, so anything larger is
skipped; its filename still shows in the `Attachments` text column, and the
`Gmail Link` on the row goes to the original mail.
"""
import base64
import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gmail_scraper import config, gmail_client

_TRANSIENT = (429, 500, 502, 503)
UPLOAD_HOST = "https://content.airtable.com"


def _upload(record_id: str, filename: str, content: bytes, content_type: str) -> bool:
    url = (f"{UPLOAD_HOST}/v0/{config.AIRTABLE_BASE_ID}/{record_id}/"
           f"{requests.utils.quote(config.ATTACHMENT_FIELD)}/uploadAttachment")
    payload = {
        "contentType": content_type or "application/octet-stream",
        "file": base64.b64encode(content).decode("ascii"),
        "filename": filename,
    }
    headers = {"Authorization": f"Bearer {config.AIRTABLE_TOKEN}",
               "Content-Type": "application/json"}
    for attempt in range(4):
        r = requests.post(url, headers=headers, json=payload, timeout=120)
        if r.status_code in _TRANSIENT and attempt < 3:
            time.sleep(2 ** attempt)
            continue
        if r.status_code >= 400:
            print(f"[attach]   ! {filename}: Airtable {r.status_code} "
                  f"{r.text[:200]}")
            return False
        return True
    return False


def upload_for_thread(record_id: str, refs: list, subject: str = "") -> dict:
    """Upload a thread's attachments onto its Airtable record.

    Returns {'uploaded': n, 'skipped': [filenames]}. Attachments accumulate in
    the field across runs, so we only upload names the record doesn't have yet
    — passing `existing` from the caller avoids duplicating on a re-run.
    """
    limit_bytes = int(config.MAX_ATTACHMENT_MB * 1024 * 1024)
    uploaded, skipped = 0, []

    for ref in refs[:config.MAX_ATTACHMENTS_PER_THREAD]:
        if ref["size"] > limit_bytes:
            skipped.append(f"{ref['filename']} ({ref['size'] / 1e6:.1f} MB)")
            continue
        try:
            content = gmail_client.download_attachment(
                ref["message_id"], ref["attachment_id"])
        except Exception as exc:                      # noqa: BLE001 - keep going
            print(f"[attach]   ! {ref['filename']}: Gmail download failed ({exc})")
            skipped.append(ref["filename"])
            continue
        # Gmail reports the encoded size; the decoded file can still exceed the
        # cap, so re-check before spending an upload call.
        if len(content) > limit_bytes:
            skipped.append(f"{ref['filename']} ({len(content) / 1e6:.1f} MB)")
            continue
        if _upload(record_id, ref["filename"], content, ref["mime_type"]):
            uploaded += 1
        else:
            skipped.append(ref["filename"])

    dropped = len(refs) - config.MAX_ATTACHMENTS_PER_THREAD
    if dropped > 0:
        skipped.append(f"+{dropped} beyond the per-thread cap")

    if uploaded or skipped:
        label = subject[:45] or record_id
        note = f"[attach] {label}: {uploaded} uploaded"
        if skipped:
            note += f", {len(skipped)} skipped ({', '.join(skipped[:3])})"
        print(note)
    return {"uploaded": uploaded, "skipped": skipped}


def existing_filenames(record: dict) -> set:
    """Filenames already sitting in the record's attachment field."""
    cells = record.get("fields", {}).get(config.ATTACHMENT_FIELD) or []
    return {c.get("filename", "") for c in cells if isinstance(c, dict)}
