"""
Tests for archive-then-delete retention.

The load-bearing properties, in order of how badly they'd hurt if broken:
  1. A row with no usable date is never deleted.
  2. A failed archive aborts the delete rather than proceeding.
  3. Archiving the same month twice appends instead of overwriting.
Everything else here is arithmetic.
"""
import csv
import importlib.util
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("AIRTABLE_PAT", "x")
os.environ.setdefault("AIRTABLE_BASE_ID", "app_test")

_spec = importlib.util.spec_from_file_location(
    "prune_with_archive", os.path.join(_ROOT, "scripts", "prune_with_archive.py"))
pr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pr)

OLD = (date.today() - timedelta(days=400)).isoformat()
NEW = date.today().isoformat()


def _row(rid, d, **extra):
    fields = {"Value": 1, **extra}
    if d is not None:
        fields["Date"] = d
    return {"id": rid, "fields": fields}


def test_only_rows_older_than_the_cutoff_are_selected():
    rows = [_row("r1", OLD), _row("r2", NEW)]
    to_delete, undated = pr.expired(rows, "Date", (date.today() - timedelta(days=90)).isoformat())
    assert [r["id"] for r in to_delete] == ["r1"]
    assert undated == 0


def test_a_row_with_no_date_is_never_deleted():
    """An unparseable date is not evidence that a row is old. Deleting it
    would silently destroy exactly the rows whose provenance is already
    unclear."""
    rows = [_row("r1", None), _row("r2", ""), _row("r3", "not-a-date")]
    to_delete, undated = pr.expired(rows, "Date", NEW)
    assert to_delete == []
    assert undated == 3


def test_boundary_row_exactly_at_the_cutoff_is_kept():
    cutoff = (date.today() - timedelta(days=90)).isoformat()
    to_delete, _ = pr.expired([_row("r1", cutoff)], "Date", cutoff)
    assert to_delete == [], "retention is inclusive of the cutoff day"


def test_archive_writes_one_file_per_month_with_the_airtable_id(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "ARCHIVE_DIR", str(tmp_path))
    rows = [_row("rec1", "2026-06-04"), _row("rec2", "2026-06-20"),
            _row("rec3", "2026-07-02")]
    written = pr.archive("BD Metrics Daily", rows, "Date")
    assert len(written) == 2
    june = tmp_path / "bd_metrics_daily" / "2026-06.csv"
    with open(june, newline="") as fh:
        got = list(csv.DictReader(fh))
    assert [r["_airtable_id"] for r in got] == ["rec1", "rec2"]
    assert got[0]["Date"] == "2026-06-04"


def test_archiving_the_same_month_twice_appends_rather_than_overwrites(tmp_path, monkeypatch):
    """A second prune touching an already-archived month must not discard what
    the first one saved — that would delete from Airtable AND from the
    archive, which is the one outcome this whole design exists to prevent."""
    monkeypatch.setattr(pr, "ARCHIVE_DIR", str(tmp_path))
    pr.archive("Sync Log", [_row("rec1", "2026-06-04")], "Date")
    pr.archive("Sync Log", [_row("rec2", "2026-06-05")], "Date")
    with open(tmp_path / "sync_log" / "2026-06.csv", newline="") as fh:
        got = list(csv.DictReader(fh))
    assert [r["_airtable_id"] for r in got] == ["rec1", "rec2"]


def test_a_new_column_appearing_later_does_not_shift_existing_columns(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "ARCHIVE_DIR", str(tmp_path))
    pr.archive("Sync Log", [_row("rec1", "2026-06-04")], "Date")
    pr.archive("Sync Log", [_row("rec2", "2026-06-05", Extra="new")], "Date")
    with open(tmp_path / "sync_log" / "2026-06.csv", newline="") as fh:
        got = list(csv.DictReader(fh))
    assert got[0]["Date"] == "2026-06-04"
    assert got[1]["Extra"] == "new"


def test_a_failed_archive_aborts_the_delete(monkeypatch, capsys):
    """The point of archiving first. If the CSV cannot be written, the rows
    must stay in Airtable — a full base is an inconvenience, losing the only
    copy of a measured day is not."""
    deleted = []

    class _Tbl:
        def all(self):
            return [_row("r1", OLD)]

        def batch_delete(self, ids):
            deleted.extend(ids)

    class _At:
        def __init__(self, *a, **k):
            self.table = _Tbl()

    monkeypatch.setattr(pr, "AirtableClient", _At)
    monkeypatch.setattr(pr, "archive",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    result = pr.prune_table("BD Metrics Daily", 90, "Date", apply=True)
    assert deleted == [], "nothing may be deleted when the archive failed"
    assert "archive failed" in result["error"]
    assert "ABORTED" in capsys.readouterr().out


def test_dry_run_neither_archives_nor_deletes(monkeypatch):
    deleted, archived = [], []

    class _Tbl:
        def all(self):
            return [_row("r1", OLD)]

        def batch_delete(self, ids):
            deleted.extend(ids)

    class _At:
        def __init__(self, *a, **k):
            self.table = _Tbl()

    monkeypatch.setattr(pr, "AirtableClient", _At)
    monkeypatch.setattr(pr, "archive", lambda *a, **k: archived.append(1) or [])
    result = pr.prune_table("BD Metrics Daily", 90, "Date", apply=False)
    assert deleted == [] and archived == []
    assert result["would_delete"] == 1


def test_every_retention_entry_names_a_window_and_a_date_field():
    for table, (days, field) in pr.RETENTION.items():
        assert isinstance(days, int) and days > 0, table
        assert isinstance(field, str) and field, table


def test_archive_only_writes_the_csv_but_deletes_nothing(tmp_path, monkeypatch):
    """Archive and delete are separable on purpose: the workflow archives,
    commits, and only then deletes. A push that failed after the delete would
    strand rows that no longer exist anywhere else."""
    deleted = []

    class _Tbl:
        def all(self):
            return [_row("r1", OLD)]

        def batch_delete(self, ids):
            deleted.extend(ids)

    class _At:
        def __init__(self, *a, **k):
            self.table = _Tbl()

    monkeypatch.setattr(pr, "AirtableClient", _At)
    monkeypatch.setattr(pr, "ARCHIVE_DIR", str(tmp_path))
    result = pr.prune_table("BD Metrics Daily", 90, "Date",
                            apply=False, archive_only=True)
    assert deleted == []
    assert result["archived"] == 1
    assert (tmp_path / "bd_metrics_daily" / f"{OLD[:7]}.csv").exists()


def test_archiving_the_same_rows_twice_does_not_duplicate_them(tmp_path, monkeypatch):
    """archive -> commit -> delete gets retried as a whole when a push fails,
    so the same rows reach archive() more than once."""
    monkeypatch.setattr(pr, "ARCHIVE_DIR", str(tmp_path))
    rows = [_row("rec1", "2026-06-04"), _row("rec2", "2026-06-05")]
    pr.archive("BD Metrics Daily", rows, "Date")
    pr.archive("BD Metrics Daily", rows, "Date")
    with open(tmp_path / "bd_metrics_daily" / "2026-06.csv", newline="") as fh:
        got = list(csv.DictReader(fh))
    assert [r["_airtable_id"] for r in got] == ["rec1", "rec2"]
