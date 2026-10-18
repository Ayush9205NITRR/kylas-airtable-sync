"""
vedant@enout.in was taken off every email this system sends (2026-10-18).

Calendar invites had him hardcoded as an attendee on every event, and
team.json's `cc` / `hot_pipeline_to` lists put him on every digest. These tests
pin both so he cannot creep back in through either path.
"""
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from utils import calendar_invite as cal  # noqa: E402

VEDANT = "vedant@enout.in"
TEAM_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "config", "team.json")


class _CapturingSMTP:
    sent = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        pass

    def starttls(self):
        pass

    def login(self, *a):
        pass

    def sendmail(self, sender, recipients, raw):
        type(self).sent.append((list(recipients), raw))


def _invite(monkeypatch, owner="rep@enout.in"):
    _CapturingSMTP.sent = []
    monkeypatch.setenv("SMTP_USER", "bot@enout.in")
    monkeypatch.setenv("SMTP_PASS", "x")
    monkeypatch.setattr(cal.smtplib, "SMTP", _CapturingSMTP)
    ok = cal.send_invite(
        contact_id="1", contact_name="Jane Doe", contact_email="j@x.com",
        contact_phone="1", company_name="Acme", remarks="",
        call_date=date(2026, 10, 19), owner_email=owner, call_time="09:00:00")
    assert ok
    return _CapturingSMTP.sent[0]


def test_an_invite_goes_to_the_owner_and_ayush_only(monkeypatch):
    recipients, raw = _invite(monkeypatch)
    assert sorted(recipients) == ["ayush@enout.in", "rep@enout.in"]
    assert VEDANT not in raw


def test_an_invite_with_no_owner_goes_to_ayush_alone(monkeypatch):
    recipients, raw = _invite(monkeypatch, owner="")
    assert recipients == ["ayush@enout.in"]
    assert VEDANT not in raw


def test_the_ical_attendee_list_does_not_name_him():
    ics = cal._build_ical(
        contact_id="1", contact_name="Jane Doe", contact_email="", contact_phone="",
        company_name="Acme", remarks="", call_date=date(2026, 10, 19),
        owner_email="rep@enout.in", organizer_email="bot@enout.in",
        kylas_url="", call_time="09:00:00")
    assert VEDANT not in ics
    assert "mailto:ayush@enout.in" in ics and "mailto:rep@enout.in" in ics


def test_no_team_json_recipient_list_includes_him():
    with open(TEAM_PATH) as fh:
        cfg = json.load(fh)
    lists = {
        "cc": cfg.get("cc", []),
        "hot_pipeline_to": cfg.get("hot_pipeline_to", []),
        "deal_rot.recipients": (cfg.get("deal_rot") or {}).get("recipients", []),
    }
    for name, addrs in lists.items():
        assert VEDANT not in [a.lower() for a in addrs], f"{name} still lists {VEDANT}"
    assert "ayush@enout.in" in cfg["cc"], "Ayush must still be copied on digests"
