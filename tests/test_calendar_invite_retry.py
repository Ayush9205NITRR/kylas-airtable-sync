"""
Tests for the calendar-invite retry behaviour.

Every invite opens its own Gmail connection and logs in again. A 156-invite
catch-up on 2026-10-18 sent 83 and lost 73 to "Connection unexpectedly
closed", and the job still reported success. The rules pinned down here:

  * a dropped connection / timeout / Gmail 4xx is retried, with a gap
  * an auth failure or a permanent 5xx rejection is NOT retried -- repeating
    it cannot help, and a bad password would just hammer Gmail
  * after the last attempt the failure is reported, never swallowed
"""
import os
import smtplib
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from utils import calendar_invite as cal  # noqa: E402


class _FakeSMTP:
    """smtplib.SMTP stand-in. `plan` is a list consumed one entry per
    connection: None = succeed, or an exception to raise at login."""
    plan: list = []
    connections = 0

    def __init__(self, *a, **k):
        type(self).connections += 1
        self._outcome = type(self).plan.pop(0) if type(self).plan else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        pass

    def starttls(self):
        pass

    def login(self, *a):
        if self._outcome is not None:
            raise self._outcome

    def sendmail(self, *a):
        pass


def _send(monkeypatch, plan):
    _FakeSMTP.plan = list(plan)
    _FakeSMTP.connections = 0
    sleeps = []
    monkeypatch.setenv("SMTP_USER", "bot@enout.in")
    monkeypatch.setenv("SMTP_PASS", "x")
    monkeypatch.setattr(cal.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(cal.time, "sleep", sleeps.append)
    ok = cal.send_invite(
        contact_id="1", contact_name="Jane Doe", contact_email="j@x.com",
        contact_phone="1", company_name="Acme", remarks="",
        call_date=date(2026, 10, 19), owner_email="rep@enout.in")
    return ok, sleeps, _FakeSMTP.connections


def test_a_clean_send_makes_one_connection_and_never_sleeps(monkeypatch):
    ok, sleeps, conns = _send(monkeypatch, [None])
    assert ok and conns == 1 and sleeps == []


def test_a_dropped_connection_is_retried_then_succeeds(monkeypatch):
    """The exact failure from 2026-10-18."""
    boom = smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
    ok, sleeps, conns = _send(monkeypatch, [boom, boom, None])
    assert ok is True
    assert conns == 3
    assert sleeps == [10, 30], "backs off between attempts, not a tight loop"


def test_it_gives_up_after_the_last_attempt_and_says_so(monkeypatch, capsys):
    boom = smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
    ok, sleeps, conns = _send(monkeypatch, [boom, boom, boom])
    assert ok is False, "a send that never succeeded must report failure"
    assert conns == 3 and sleeps == [10, 30]
    assert "ERROR for 'Jane Doe'" in capsys.readouterr().out


def test_an_auth_failure_is_not_retried(monkeypatch):
    """A wrong app password stays wrong. Retrying it three times just
    hammers Gmail with bad logins."""
    bad = smtplib.SMTPAuthenticationError(535, b"bad credentials")
    ok, sleeps, conns = _send(monkeypatch, [bad, None, None])
    assert ok is False and conns == 1 and sleeps == []


def test_a_permanent_5xx_rejection_is_not_retried(monkeypatch):
    perm = smtplib.SMTPResponseException(550, b"mailbox unavailable")
    ok, sleeps, conns = _send(monkeypatch, [perm, None])
    assert ok is False and conns == 1 and sleeps == []


def test_gmails_try_again_later_4xx_is_retried(monkeypatch):
    later = smtplib.SMTPResponseException(421, b"4.7.0 try again later")
    ok, sleeps, conns = _send(monkeypatch, [later, None])
    assert ok is True and conns == 2 and sleeps == [10]


def test_a_socket_timeout_is_retried(monkeypatch):
    ok, sleeps, conns = _send(monkeypatch, [TimeoutError("timed out"), None])
    assert ok is True and conns == 2 and sleeps == [10]


def test_is_transient_classification():
    assert cal._is_transient(smtplib.SMTPServerDisconnected("x"))
    assert cal._is_transient(smtplib.SMTPConnectError(421, b"x"))
    assert cal._is_transient(ConnectionResetError("x"))
    assert not cal._is_transient(smtplib.SMTPAuthenticationError(535, b"x"))
    assert not cal._is_transient(smtplib.SMTPResponseException(550, b"x"))
    assert not cal._is_transient(ValueError("x"))
