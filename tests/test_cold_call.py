"""
Offline unit tests for the Cold Call Analysis System.

These run without any API keys or network — they exercise the pure logic
(JSON cleaning, record mapping, email rendering, BD lookup, date math).

Run:  python tests/test_cold_call.py     (or: pytest tests/test_cold_call.py)
"""
import json
import os
import sys
from datetime import date, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cold_call import analyze, config, email_coach, pipeline


def test_is_supported():
    assert config.is_supported("call_001.mp4")
    assert config.is_supported("REC.M4A")  # case-insensitive
    assert not config.is_supported("notes.txt")
    assert not config.is_supported("noext")


def test_ist_day_start_utc():
    # 00:00 IST on 2026-06-15 == 18:30 UTC on 2026-06-14.
    utc = config.ist_day_start_utc(date(2026, 6, 15))
    assert utc.tzinfo == timezone.utc
    assert utc.strftime("%Y-%m-%dT%H:%M:%S") == "2026-06-14T18:30:00"


def test_strip_fences():
    assert analyze._strip_fences('```json\n{"a":1}\n```') == '{"a":1}'
    assert analyze._strip_fences('```\n{"a":1}\n```') == '{"a":1}'
    assert analyze._strip_fences('{"a":1}') == '{"a":1}'


def test_slice_braces():
    assert analyze._slice_braces('noise {"a":1} trailing') == '{"a":1}'
    assert analyze._slice_braces("no braces here") == ""


def test_analyze_call_parses(monkeypatch):
    payload = {"hook_score": 20, "total_score": 80}

    class _Resp:
        text = "Here is your result:\n```json\n" + json.dumps(payload) + "\n```"

    class _FakeModel:
        def generate_content(self, _prompt):
            return _Resp()

    monkeypatch.setattr(analyze, "_model", lambda: _FakeModel())
    out = analyze.analyze_call("transcript", "Priya", "2026-06-15")
    assert out == payload


def test_analyze_call_bad_json(monkeypatch):
    class _Resp:
        text = "sorry, I cannot do that"

    monkeypatch.setattr(analyze, "_model",
                        lambda: type("M", (), {"generate_content": lambda s, p: _Resp()})())
    try:
        analyze.analyze_call("t", "Priya", "2026-06-15")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_build_record_omits_total_and_serializes_objections():
    analysis = {
        "hook_score": 18, "objection_score": 20, "pitch_score": 17, "discovery_score": 7,
        "total_score": 62, "objections_found": [{"objection": "budget", "handled": "weak"}],
        "hook_feedback": "ok",
    }
    rec = pipeline._build_record("Priya", "c.mp4", "2026-06-15", 120, "hi", analysis)
    assert "total_score" not in rec               # formula field, never sent
    assert rec["status"] == "processed"
    assert rec["bd_name"] == "Priya"
    assert rec["duration_seconds"] == 120
    # objections_list is valid JSON, not a Python repr
    assert json.loads(rec["objections_list"])[0]["objection"] == "budget"


def test_resolve_bd_email():
    # team.json ships with Rubal -> rubal@enout.in
    assert email_coach.resolve_bd_email("Rubal") == "rubal@enout.in"
    assert email_coach.resolve_bd_email("rubal") == "rubal@enout.in"
    assert email_coach.resolve_bd_email("Nobody") == ""


def test_build_email_html():
    calls = [
        {"total_score": 60, "hook_score": 18, "objection_score": 20, "pitch_score": 15,
         "discovery_score": 7, "top_miss": "No clear next step",
         "objections_found": [{"objection": "Budget nahi", "handled": "weak",
                               "better_response": "ROI dikhata hoon"}]},
        {"total_score": 70, "hook_score": 22, "objection_score": 24, "pitch_score": 17,
         "discovery_score": 7, "top_miss": "No clear next step",
         "objections_found": []},
    ]
    html = email_coach.build_email_html("Priya", calls)
    assert "Priya" in html
    assert "Calls analyzed" in html
    assert "65/100" in html       # avg of 60 and 70
    assert "No clear next step" in html
    assert "ROI dikhata hoon" in html
    assert "Hook" in html and "Discovery Booked" in html


def _run_all():
    import inspect
    mod = sys.modules[__name__]
    tests = [(n, f) for n, f in inspect.getmembers(mod, inspect.isfunction)
             if n.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            if "monkeypatch" in inspect.signature(fn).parameters:
                fn(_MiniMonkeypatch())
            else:
                fn()
            print(f"PASS {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


class _MiniMonkeypatch:
    """Tiny stand-in for pytest's monkeypatch when run as a plain script."""
    def __init__(self):
        self._undo = []

    def setattr(self, target, name, value=None):
        if value is None:  # setattr(obj.attr, val) form not used here
            raise ValueError("use setattr(module, name, value)")
        self._undo.append((target, name, getattr(target, name)))
        setattr(target, name, value)


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
