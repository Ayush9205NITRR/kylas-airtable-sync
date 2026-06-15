"""
Gemini analysis of a call transcript -> structured 4-parameter scorecard.

Returns the parsed JSON dict described in the brief. Raises ValueError if the
model returns something that isn't valid JSON, so the pipeline can log the raw
text and mark the call status=error.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cold_call import config
from cold_call.prompt import SYSTEM_PROMPT, build_user_prompt

_MODEL = None


def _model():
    global _MODEL
    if _MODEL is None:
        import google.generativeai as genai
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not set")
        genai.configure(api_key=api_key)
        _MODEL = genai.GenerativeModel(config.GEMINI_MODEL, system_instruction=SYSTEM_PROMPT)
    return _MODEL


def _strip_fences(raw: str) -> str:
    """Remove a ```json ... ``` markdown wrapper if Gemini added one."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.lstrip().lower().startswith("json"):
            raw = raw.lstrip()[4:]
    return raw.strip()


def _slice_braces(text: str) -> str:
    """Best-effort: the substring from the first '{' to the last '}'."""
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if start != -1 and end > start else ""


def analyze_call(transcript: str, bd_name: str, call_date: str) -> dict:
    resp = _model().generate_content(build_user_prompt(transcript, bd_name, call_date))
    raw = (getattr(resp, "text", "") or "").strip()
    cleaned = _strip_fences(raw)
    for candidate in (cleaned, _slice_braces(cleaned)):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise ValueError(f"Gemini did not return valid JSON.\n--- raw response ---\n{raw}")


if __name__ == "__main__":
    import argparse
    from dotenv import load_dotenv
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("transcript_file", help="path to a .txt transcript")
    parser.add_argument("--bd", default="Test")
    args = parser.parse_args()
    with open(args.transcript_file) as fh:
        text = fh.read()
    print(json.dumps(analyze_call(text, args.bd, config.today_ist().isoformat()), indent=2))
