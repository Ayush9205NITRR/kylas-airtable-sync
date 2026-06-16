"""
Transcription via the Hugging Face Inference API (Whisper small), using the
official huggingface_hub InferenceClient — it targets HF's current router
endpoint and sets auth + content-type correctly (so we don't hand-roll the
HTTP call against a moving API).

- Hinglish: the multilingual Whisper model auto-detects Hindi + English.
- Model cold-start -> wait once, retry.
- Files larger than the HF body limit are split into CHUNK_SECONDS segments with
  pydub (needs ffmpeg) and transcribed piece by piece.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cold_call import config

_CLIENT = None


def _client():
    global _CLIENT
    if _CLIENT is None:
        from huggingface_hub import InferenceClient
        token = os.environ.get("HF_API_TOKEN", "")
        if not token:
            raise RuntimeError("HF_API_TOKEN not set")
        _CLIENT = InferenceClient(
            model=config.HF_WHISPER_MODEL,
            provider=config.HF_PROVIDER,
            token=token,
        )
    return _CLIENT


def _to_text(out) -> str:
    """Normalise the various ASR return shapes to a plain string."""
    if isinstance(out, str):
        return out
    text = getattr(out, "text", None)
    if text is not None:
        return text
    if isinstance(out, dict):
        return out.get("text", "")
    return str(out)


def _asr(audio) -> str:
    """Run ASR on bytes (or a path), retrying once on a model cold-start."""
    out = None
    for attempt in range(2):
        try:
            out = _client().automatic_speech_recognition(audio)
            break
        except Exception as exc:
            msg = str(exc).lower()
            if attempt == 0 and ("503" in msg or "loading" in msg):
                print("  [transcribe] model loading — waiting 20s then retrying")
                time.sleep(20)
                continue
            raise
    return _to_text(out)


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
        parts.append(_asr(data))
        time.sleep(1)
    return " ".join(p.strip() for p in parts if p).strip()


def transcribe(filepath: str) -> str:
    size = os.path.getsize(filepath)
    if size > config.MAX_HF_BYTES:
        print(f"  [transcribe] {size / 1e6:.1f} MB > limit — chunking")
        return _transcribe_chunked(filepath)
    with open(filepath, "rb") as f:
        return _asr(f.read()).strip()


if __name__ == "__main__":
    import argparse
    from dotenv import load_dotenv
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", help="path to an audio file")
    args = parser.parse_args()
    print(transcribe(args.audio))
