"""
Transcription via Gemini (multimodal). Hugging Face's serverless tier stopped
serving Whisper, so we transcribe the audio directly with the same Gemini model
used for analysis — one provider, one key, no extra service.

Flow: upload the audio to the Gemini File API -> ask the model to transcribe ->
delete the uploaded file. If the source format is awkward, fall back to a
16 kHz mono WAV (via pydub/ffmpeg), which Gemini always accepts.
"""
import mimetypes
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cold_call import config

# Source extension -> a MIME type Gemini accepts. .mpeg etc. otherwise resolve to
# video/* via the stdlib, which we don't want for audio.
_AUDIO_MIME = {
    ".mp3": "audio/mpeg", ".mpeg": "audio/mpeg", ".mpga": "audio/mpeg",
    ".wav": "audio/wav", ".aac": "audio/aac", ".ogg": "audio/ogg",
    ".opus": "audio/ogg", ".flac": "audio/flac", ".m4a": "audio/mp4",
    ".mp4": "video/mp4", ".webm": "video/webm", ".aiff": "audio/aiff",
}

TRANSCRIBE_PROMPT = (
    "Transcribe this sales call audio verbatim. Keep the original language as "
    "spoken — it is Hinglish (a mix of Hindi and English). Return ONLY the "
    "transcript text: no timestamps, no commentary, no markdown."
)


def _genai():
    import google.generativeai as genai
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")
    genai.configure(api_key=api_key)
    return genai


def _mime_for(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return _AUDIO_MIME.get(ext) or mimetypes.guess_type(path)[0] or "audio/mpeg"


def _transcribe_via_gemini(genai, filepath: str, mime_type: str) -> str:
    f = genai.upload_file(path=filepath, mime_type=mime_type)
    try:
        for _ in range(30):  # wait until the file is processed (usually seconds)
            if getattr(f.state, "name", "ACTIVE") != "PROCESSING":
                break
            time.sleep(2)
            f = genai.get_file(f.name)
        if getattr(f.state, "name", "") == "FAILED":
            raise RuntimeError("Gemini failed to process the audio file")
        model = genai.GenerativeModel(config.GEMINI_MODEL)
        resp = model.generate_content([TRANSCRIBE_PROMPT, f])
        return (getattr(resp, "text", "") or "").strip()
    finally:
        try:
            genai.delete_file(f.name)
        except Exception:
            pass


def _to_wav_16k_mono(filepath: str) -> str:
    """Re-encode to 16 kHz mono WAV (speech-friendly, always Gemini-accepted)."""
    from pydub import AudioSegment
    audio = AudioSegment.from_file(filepath).set_frame_rate(16000).set_channels(1)
    out = filepath + ".16k.wav"
    audio.export(out, format="wav")
    return out


def transcribe(filepath: str) -> str:
    genai = _genai()
    try:
        return _transcribe_via_gemini(genai, filepath, _mime_for(filepath))
    except Exception as exc:
        print(f"  [transcribe] direct upload failed ({exc}); converting to WAV and retrying")
        wav = _to_wav_16k_mono(filepath)
        try:
            return _transcribe_via_gemini(genai, wav, "audio/wav")
        finally:
            if os.path.exists(wav):
                os.remove(wav)


if __name__ == "__main__":
    import argparse
    from dotenv import load_dotenv
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", help="path to an audio file")
    args = parser.parse_args()
    print(transcribe(args.audio))
