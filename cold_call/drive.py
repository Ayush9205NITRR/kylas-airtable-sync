"""
Google Drive (v3) access for the cold-call pipeline.

Folder layout (per the brief):

    <calls folder>/
        Priya/   call_001.mp4 ...
        Rahul/   ...

BD name = the sub-folder's name. We list audio files modified on/after 00:00
IST of the target day, and download them on demand.

Auth: a service account. GOOGLE_SERVICE_ACCOUNT_JSON may be a path to the key
file, or the raw JSON string (convenient for CI secrets). Shared Drives are
supported.
"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cold_call import config

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
_SERVICE = None


def _credentials():
    from google.oauth2 import service_account
    raw = (config.GOOGLE_SERVICE_ACCOUNT_JSON or "").strip()
    if not raw:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON not set")
    if raw.startswith("{"):
        info = json.loads(raw)
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return service_account.Credentials.from_service_account_file(raw, scopes=SCOPES)


def _service():
    global _SERVICE
    if _SERVICE is None:
        from googleapiclient.discovery import build
        _SERVICE = build("drive", "v3", credentials=_credentials(), cache_discovery=False)
    return _SERVICE


def _list(query: str, fields: str = "files(id,name,mimeType,modifiedTime,size)") -> list:
    """Run a paginated Drive list query, with Shared Drive support."""
    svc = _service()
    out, token = [], None
    while True:
        resp = svc.files().list(
            q=query,
            fields=f"nextPageToken,{fields}",
            pageToken=token,
            pageSize=100,
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            orderBy="modifiedTime desc",
        ).execute()
        out.extend(resp.get("files", []))
        token = resp.get("nextPageToken")
        if not token:
            break
    return out


def _bd_folders(parent_id: str) -> list:
    q = (f"'{parent_id}' in parents and "
         "mimeType='application/vnd.google-apps.folder' and trashed=false")
    return [(f["name"], f["id"]) for f in _list(q, fields="files(id,name)")]


def fetch_new_files(target_day=None) -> list:
    """Audio files in each BD sub-folder modified on/after 00:00 IST of `target_day`.

    Returns a list of dicts: {bd_name, filename, drive_id, mime_type, size}.
    """
    if not config.DRIVE_FOLDER_ID:
        raise RuntimeError("GOOGLE_DRIVE_FOLDER_ID not set")

    # RFC 3339, UTC ('Z'). With an explicit day -> from 00:00 IST of that day;
    # otherwise a rolling lookback window so recent uploads aren't missed.
    cutoff_dt = config.ist_day_start_utc(target_day) if target_day else config.lookback_cutoff_utc()
    cutoff = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    folders = _bd_folders(config.DRIVE_FOLDER_ID)
    if folders:
        print(f"[drive] {len(folders)} BD subfolder(s) under calls/: "
              + ", ".join(n for n, _ in folders))
    else:
        print(f"[drive] 0 subfolders visible under folder id {config.DRIVE_FOLDER_ID} — "
              "check that calls/ is SHARED with the service account's client_email "
              "and that the folder id is correct.")

    results = []
    for bd_name, folder_id in folders:
        q = f"'{folder_id}' in parents and trashed=false and modifiedTime >= '{cutoff}'"
        matched, skipped = 0, []
        for f in _list(q):
            if not config.is_supported(f["name"]):
                skipped.append(f["name"])
                continue
            matched += 1
            results.append({
                "bd_name": bd_name,
                "filename": f["name"],
                "drive_id": f["id"],
                "mime_type": f.get("mimeType", ""),
                "size": int(f.get("size", 0) or 0),
            })
        msg = f"[drive]   {bd_name}: {matched} audio file(s) since {cutoff}"
        if skipped:
            msg += (f"; {len(skipped)} skipped (unsupported format): "
                    + ", ".join(skipped[:5]) + (" ..." if len(skipped) > 5 else ""))
        print(msg)
    return results


def download_file(drive_id: str, dest_path: str) -> str:
    """Download a Drive file's contents to `dest_path`."""
    from googleapiclient.http import MediaIoBaseDownload
    svc = _service()
    request = svc.files().get_media(fileId=drive_id, supportsAllDrives=True)
    with io.FileIO(dest_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    return dest_path


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    files = fetch_new_files()
    print(f"Found {len(files)} new audio file(s) for {config.today_ist()}:")
    for f in files:
        size_mb = f["size"] / 1e6 if f["size"] else 0
        print(f"  {f['bd_name']:<12} {f['filename']}  ({size_mb:.1f} MB)")
