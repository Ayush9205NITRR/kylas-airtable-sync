"""
BD Metrics Daily is derived from stage MOVEMENT, not current position.

The distinction these tests protect: a contact used to count on every day its
"current stage" was read, which quietly inflated every day after the move. Now
it counts once, on the day it actually moved.

Run: python -m pytest tests/test_metrics_from_stage_changes.py -q
"""
import importlib.util
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("KYLAS_API_KEY", "test:1")
os.environ.setdefault("AIRTABLE_PAT", "test")
os.environ.setdefault("AIRTABLE_BASE_ID", "app_test")


def _long():
    spec = importlib.util.spec_from_file_location(
        "bd_metrics_long",
        os.path.join(os.path.dirname(os.path.dirname(__file__)),
                     "scripts", "bd_metrics_long.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


long_mod = _long()


def _chg(date, to, rep="Anjali Athya", email="anjali.athya@enout.in", cid="501"):
    return {"date": date, "rep": rep, "email": email, "company_id": cid, "to": to}


def _rows(changes):
    rows, _ = long_mod.build_long(all_owners=True, changes=changes)
    return rows


def test_a_move_to_a_real_stage_counts_as_attempted_and_connected():
    rows = _rows([_chg("2026-09-08", "Follow-up (1)")])
    k = ("Anjali Athya", "anjali.athya@enout.in", "2026-09-08", "Contact")
    assert rows[(*k, "Call Attempted")] == 1
    assert rows[(*k, "Call Connected")] == 1


def test_a_move_to_a_cnc_stage_is_attempted_but_not_connected():
    """Tried and could not reach them — that is the whole point of the CNC
    stages, and it is the attempted/connected split the team cares about."""
    rows = _rows([_chg("2026-09-08", "CNC (Could Not Connect) - 1")])
    k = ("Anjali Athya", "anjali.athya@enout.in", "2026-09-08", "Contact")
    assert rows[(*k, "Call Attempted")] == 1
    assert rows[(*k, "Call Connected")] == 0


def test_a_move_to_the_bottom_stage_is_not_an_attempt():
    """Landing back on the un-mined stage — a rename or a regression — is a
    stage change but never evidence that anyone called."""
    rows = _rows([_chg("2026-09-08", "LinkedIn Outreach Initiated")])
    k = ("Anjali Athya", "anjali.athya@enout.in", "2026-09-08", "Contact")
    # An all-zero contact key emits no row at all, which reads as zero just the
    # same — .get() asserts the outcome rather than the storage detail.
    assert rows.get((*k, "Call Attempted"), 0) == 0
    assert rows.get((*k, "Call Connected"), 0) == 0


def test_a_contact_counts_only_on_the_day_it_moved():
    """The core behaviour change. Two moves on two days = one count each,
    never a running total carried forward."""
    rows = _rows([_chg("2026-09-08", "Follow-up (1)"),
                  _chg("2026-09-09", "MQL (Marketing Qualified Lead)")])
    base = ("Anjali Athya", "anjali.athya@enout.in")
    assert rows[(*base, "2026-09-08", "Contact", "Call Attempted")] == 1
    assert rows[(*base, "2026-09-09", "Contact", "Call Attempted")] == 1
    assert rows[(*base, "2026-09-08", "Contact", "MQL")] == 0
    assert rows[(*base, "2026-09-09", "Contact", "MQL")] == 1


def test_several_moves_by_one_rep_on_one_day_accumulate():
    rows = _rows([_chg("2026-09-08", "Follow-up (1)", cid="1"),
                  _chg("2026-09-08", "Follow-up (2)", cid="2"),
                  _chg("2026-09-08", "CNC (Could Not Connect) - 2", cid="3")])
    k = ("Anjali Athya", "anjali.athya@enout.in", "2026-09-08", "Contact")
    assert rows[(*k, "Call Attempted")] == 3
    assert rows[(*k, "Call Connected")] == 2


def test_reps_are_kept_separate():
    rows = _rows([_chg("2026-09-08", "Follow-up (1)"),
                  _chg("2026-09-08", "Follow-up (1)",
                       rep="Gaurav Kumar", email="gaurav@enout.in")])
    for rep, email in (("Anjali Athya", "anjali.athya@enout.in"),
                       ("Gaurav Kumar", "gaurav@enout.in")):
        assert rows[(rep, email, "2026-09-08", "Contact", "Call Attempted")] == 1


def test_sql_is_counted_from_the_move_into_sql():
    rows = _rows([_chg("2026-09-08", "SQL (Sales Qualified Lead)")])
    k = ("Anjali Athya", "anjali.athya@enout.in", "2026-09-08", "Contact")
    assert rows[(*k, "SQL")] == 1


def test_an_empty_log_produces_no_rows_rather_than_stale_numbers():
    """A day nobody moved anything is zero. Previously this read as whatever
    every contact's current stage happened to be."""
    assert _rows([]) == {}


def test_rows_missing_a_date_owner_or_stage_are_dropped_not_guessed():
    rows = _rows([
        _chg("", "Follow-up (1)"),                       # no date
        _chg("2026-09-08", ""),                          # no stage
        {"date": "2026-09-08", "rep": "", "email": "x@y.z",
         "company_id": "1", "to": "Follow-up (1)"},      # no owner
    ])
    assert rows == {}


def test_off_roster_owners_are_excluded_when_a_roster_applies(monkeypatch):
    monkeypatch.setattr(long_mod.funnel, "bd_roster",
                        lambda: {"anjali.athya@enout.in"})
    changes = [_chg("2026-09-08", "Follow-up (1)"),
               _chg("2026-09-08", "Follow-up (1)",
                    rep="Someone Else", email="someone@elsewhere.com")]
    rows, stats = long_mod.build_long(all_owners=False, changes=changes)
    assert stats["skipped"] == 1
    assert not any(k[0] == "Someone Else" for k in rows)


# ── daily / weekly / monthly digests off the same base data ──────────────────

def _rows_for(period, long_rows, today="2026-09-09"):
    return long_mod.team_digest_rows(long_rows, today, period)


def _long(*specs):
    """specs: (day, metric, value) for one rep, as build_long would emit."""
    out = {}
    for day, metric, value in specs:
        out[("Anjali Athya", "a@enout.in", day, "Contact", metric)] = value
    return out


def test_each_period_sums_only_its_own_window():
    # 2026-09-09 is a Wednesday. 09-07 is Monday (same week), 09-01 is the same
    # month but the previous week, 08-31 is the previous month entirely.
    data = _long(("2026-09-09", "Call Attempted", 1),
                 ("2026-09-07", "Call Attempted", 10),
                 ("2026-09-01", "Call Attempted", 100),
                 ("2026-08-31", "Call Attempted", 1000))
    assert _rows_for("daily", data)[0]["Call Attempted"] == 1
    assert _rows_for("weekly", data)[0]["Call Attempted"] == 11      # 09 + 07
    assert _rows_for("monthly", data)[0]["Call Attempted"] == 111    # 09 + 07 + 01


def test_weekly_and_monthly_carry_every_metric_for_their_window():
    """Same structure as daily — all eight columns, one consistent window."""
    for period in ("weekly", "monthly"):
        _w, columns, _s = long_mod.PERIODS[period]
        headers = [h for h, *_x in columns]
        assert headers == [h for h, *_x in long_mod._DIGEST_METRICS]
        assert all(win == ("week" if period == "weekly" else "month")
                   for *_x, win in columns), "one window per period"


def test_every_period_sorts_by_sql_highest_first():
    rows = {}
    for rep, sql in (("Low", 1), ("High", 9), ("Mid", 5)):
        rows[(rep, f"{rep}@enout.in", "2026-09-09", "Contact", "SQL")] = sql
    for period in ("weekly", "monthly"):
        assert [r["rep"] for r in _rows_for(period, rows)] == ["High", "Mid", "Low"]


def test_an_idle_rep_still_appears_with_zeros():
    """A rep who did nothing this week must not vanish from the team table."""
    data = {("Idle", "idle@enout.in", "2026-08-01", "Contact", "SQL"): 3}
    rows = _rows_for("weekly", data)
    assert [r["rep"] for r in rows] == ["Idle"]
    assert rows[0].get("Call Attempted", 0) == 0


def test_html_carries_a_team_total_row():
    data = _long(("2026-09-09", "Call Attempted", 2))
    for period in ("daily", "weekly", "monthly"):
        html = long_mod.build_digest_html(_rows_for(period, data), "2026-09-09", period)
        assert "TEAM TOTAL" in html


def test_titles_name_the_period():
    assert long_mod.digest_title("2026-09-09", "daily") == "BD Daily Digest — 2026-09-09"
    assert "Weekly" in long_mod.digest_title("2026-09-09", "weekly")
    assert long_mod.digest_title("2026-09-09", "monthly").endswith("2026-09")


def test_daily_digest_columns_are_unchanged():
    """The evening report the team already reads must not silently change."""
    _w, columns, sort_col = long_mod.PERIODS["daily"]
    assert [h for h, *_x in columns] == [
        "Call Attempted", "Call Connected", "Meeting Booked", "SQL (This Month)",
        "Companies Worked", "Companies Reached", "Requirements Stated",
        "Handoff Calls (This Week)"]
    assert sort_col == "SQL (This Month)"


# ── BD Metrics Daily as the base table ───────────────────────────────────────

class _FakeAT:
    """Stands in for AirtableClient over the BD Metrics Daily table."""
    def __init__(self, rows):
        self._cache = {f"k{i}": {"id": f"rec{i}", "fields": f}
                       for i, f in enumerate(rows)}
    def build_cache(self, key_field):
        return len(self._cache)


def _row(rep="Anjali Athya", email="a@enout.in", date="2026-09-09",
         group="Contact", metric="Call Attempted", value=3):
    return {"BD Associate": rep, "BD Email": email, "Date": date,
            "Week": "2026-W37", "Month": date[:7],
            "Metric Group": group, "Metric": metric, "Value": value}


def _patch_at(monkeypatch, rows):
    import utils.airtable_client as ac
    monkeypatch.setattr(ac, "AirtableClient", lambda *a, **k: _FakeAT(rows))


def test_the_base_table_round_trips_into_long_rows(monkeypatch):
    """read_metrics_daily() must reproduce exactly what push() wrote, or the
    weekly/monthly roll-ups would disagree with the daily numbers."""
    _patch_at(monkeypatch, [_row(), _row(metric="SQL", value=2)])
    out = long_mod.read_metrics_daily()
    assert out[("Anjali Athya", "a@enout.in", "2026-09-09", "Contact",
                "Call Attempted")] == 3
    assert out[("Anjali Athya", "a@enout.in", "2026-09-09", "Contact", "SQL")] == 2


def test_digests_work_identically_off_the_base_table(monkeypatch):
    """The whole point of the shape: a digest cannot tell whether its rows were
    computed or read back."""
    _patch_at(monkeypatch, [
        _row(date="2026-09-09", value=1),
        _row(date="2026-09-07", value=10),
        _row(date="2026-09-01", value=100),
    ])
    stored = long_mod.read_metrics_daily()
    assert long_mod.team_digest_rows(stored, "2026-09-09", "daily")[0]["Call Attempted"] == 1
    assert long_mod.team_digest_rows(stored, "2026-09-09", "weekly")[0]["Call Attempted"] == 11
    assert long_mod.team_digest_rows(stored, "2026-09-09", "monthly")[0]["Call Attempted"] == 111


def test_unparseable_or_incomplete_stored_rows_are_skipped(monkeypatch):
    _patch_at(monkeypatch, [
        _row(value="not-a-number"),
        _row(rep=""),                       # no associate
        {"BD Associate": "X", "Date": "2026-09-09"},   # no group/metric
        _row(value=7),                      # the only good one
    ])
    out = long_mod.read_metrics_daily()
    assert list(out.values()) == [7]


def test_a_blank_base_table_yields_no_rows_rather_than_an_error(monkeypatch):
    _patch_at(monkeypatch, [])
    assert long_mod.read_metrics_daily() == {}


# ── digest recipients come from the ACTIVE roster ────────────────────────────

def test_recipients_come_from_the_active_roster_not_team_json(monkeypatch):
    """Unticking Active in Airtable 'BD Members' must stop the email reaching
    someone. This previously read team.json's bd_team unconditionally, so the
    digest went to everyone listed there regardless of who was active."""
    sent = {}

    class _SMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def ehlo(self): pass
        def starttls(self): pass
        def login(self, *a): pass
        def sendmail(self, frm, to, msg): sent["to"] = to

    import smtplib
    monkeypatch.setattr(smtplib, "SMTP", _SMTP)
    monkeypatch.setenv("SMTP_USER", "bot@enout.in")
    monkeypatch.setenv("SMTP_PASS", "x")
    # Airtable says only these two are active; team.json lists far more.
    monkeypatch.setattr(long_mod.funnel, "bd_roster",
                        lambda: {"active1@enout.in", "active2@enout.in"})

    long_mod.send_team_digest({}, "2026-09-05", "daily")

    assert "inactive@enout.in" not in sent["to"]
    assert {"active1@enout.in", "active2@enout.in"} <= set(sent["to"])


def test_recipients_fall_back_to_team_json_when_the_roster_is_unreadable(monkeypatch):
    """A broken Airtable read must not silently email nobody."""
    sent = {}

    class _SMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def ehlo(self): pass
        def starttls(self): pass
        def login(self, *a): pass
        def sendmail(self, frm, to, msg): sent["to"] = to

    import smtplib
    monkeypatch.setattr(smtplib, "SMTP", _SMTP)
    monkeypatch.setenv("SMTP_USER", "bot@enout.in")
    monkeypatch.setenv("SMTP_PASS", "x")
    monkeypatch.setattr(long_mod.funnel, "bd_roster", lambda: set())

    long_mod.send_team_digest({}, "2026-09-05", "daily")
    assert sent.get("to"), "must still send to somebody rather than nobody"
