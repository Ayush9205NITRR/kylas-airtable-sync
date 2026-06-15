"""
Transcription via the Hugging Face Inference API (Whisper small).

- Hinglish handled by passing language="hi".
- 503 (model cold-start) -> wait 20s once, retry.
- Files larger than the HF body limit are split into CHUNK_SECONDS segments with
  pydub (needs ffmpeg) and transcribed piece by piece.
"""
import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cold_call import config


def _headers() -> dict:
    token = os.environ.get("HF_API_TOKEN", "")
    if not token:
        raise RuntimeError("HF_API_TOKEN not set")
    return {"Authorization": f"Bearer {token}"}


def _post_bytes(data: bytes) -> str:
    """POST raw audio bytes to HF, retrying once on a 503 cold-start."""
    resp = None
    for attempt in range(2):
        resp = requests.post(
            config.HF_API_URL,
            headers=_headers(),
            data=data,
            params={"language": "hi"},
            timeout=300,
        )
        if resp.status_code == 503 and attempt == 0:
            print("  [transcribe] model loading (503) — waiting 20s then retrying")
            time.sleep(20)
            continue
        resp.raise_for_status()
        break

    payload = resp.json()
    if isinstance(payload, dict):
        if "error" in payload:
            raise RuntimeError(f"HF error: {payload['error']}")
        return payload.get("text", "")
    if isinstance(payload, list) and payload:
        return payload[0].get("text", "")
    return ""


def _transcribe_chunked(filepath: str) -> str:
    """Split large audio into CHUNK_SECONDS segments and transcribe each."""
    try:
        from pydub import AudioSegment
    except ImportError as exc:
        raise RuntimeError(
            "file exceeds HF size limit and pydub is not installed for chunking"
        ) from exc

    audio = AudioSegment.from_file(filepath)
    step = config.CHUNK_SECONDS * 1000
    parts = []
    for i, start in enumerate(range(0, len(audio), step)):
        chunk = audio[start:start + step]
        buf = chunk.export(format="wav")  # wav avoids needing an mp3 encoder
        try:
            data = buf.read()
        finally:
            buf.close()
        print(f"  [transcribe] chunk {i + 1} ({len(data) / 1e6:.1f} MB)...")
        parts.append(_post_bytes(data))
        time.sleep(1)
    return " ".join(p.strip() for p in parts if p).strip()


def transcribe(filepath: str) -> str:
    size = os.path.getsize(filepath)
    if size > config.MAX_HF_BYTES:
        print(f"  [transcribe] {size / 1e6:.1f} MB > limit — chunking")
        return _transcribe_chunked(filepath)
    with open(filepath, "rb") as f:
        return _post_bytes(f.read()).strip()


if __name__ == "__main__":
    import argparse
    from dotenv import load_dotenv
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", help="path to an audio file")
    args = parser.parse_args()
    print(transcribe(args.audio))
