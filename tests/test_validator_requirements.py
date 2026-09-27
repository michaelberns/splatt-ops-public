"""
Tests for project_md_changelog_entry, the requirement behind gate_status_logged.

The requirement passes when a markdown file in the job's client folder
mentions the job's status and was modified no more than a day before the
job record last changed (jobs.updated). Its failure message is one of the
most frequently read lines in a report, so each fault gets its own
message naming the fix:

  no markdown in the folder at all                 write one
  markdown is stale but already says it            add a Change Log row
  markdown is current but does not say it          write the status in

The second case happens when a record changes for another reason, for
example a corrected job value: jobs.updated moves past the file's mtime
even though the file already says the right thing.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tests.fakes import FakePB
from validator.checks.requirements import project_md_changelog_entry
from validator.context import Context

CLIENT = {"id": "c1", "name": "Bright Fizz"}


def build(tmp_path, files, updated="2026-08-23T00:49:00Z", status="paid"):
    """A projects tree with one client folder, and the job pointing at it.

    files maps a filename to (text, age_in_days). Age is applied to the
    mtime, because mtime against jobs.updated is the whole mechanism.
    """
    folder = tmp_path / "Bright Fizz" / "RIALTO Filler Spares"
    folder.mkdir(parents=True)
    for name, (text, age_days) in files.items():
        path = folder / name
        path.write_text(text, encoding="utf-8")
        when = time.time() - age_days * 86400
        os.utime(path, (when, when))

    ctx = Context(FakePB({"clients": [CLIENT]}), project_root=tmp_path)
    subject = {"id": "j1", "client": "c1", "status": status, "updated": updated}
    return subject, ctx


def run(tmp_path, files, **kwargs):
    subject, ctx = build(tmp_path, files, **kwargs)
    return project_md_changelog_entry({}, subject, ctx)


# the passing case
def test_a_current_file_that_records_the_status_passes(tmp_path):
    ok, why = run(tmp_path, {"PROJECT.md": ("Status: Paid in full", 0)})
    assert ok is True
    assert "PROJECT.md" in why


def test_the_window_is_a_day_wide_not_a_moment(tmp_path):
    """A person writes the file, then the sync writes the record a few
    minutes later. Demanding the file be newer than the record would fail
    every correctly filed job."""
    ok, _ = run(tmp_path, {"PROJECT.md": ("Status: Paid", 0.9)},
                updated=_now_iso())
    assert ok is True


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# the three failures, each of which needs a different thing done to it
def test_an_empty_folder_says_the_file_is_missing(tmp_path):
    ok, why = run(tmp_path, {})
    assert ok is False
    assert "no markdown file" in why
    assert "does not say" not in why, "it blamed a file that is not there"


def test_a_stale_file_that_already_says_it_is_told_apart(tmp_path):
    """The file already says the status but is older than the record's
    last change, so the fix is a Change Log row, not a rewrite."""
    ok, why = run(tmp_path, {"PROJECT.md": ("Status: Paid in full", 40)})
    assert ok is False
    assert "already says" in why
    assert "Change Log" in why


def test_a_current_file_missing_the_status_says_so(tmp_path):
    ok, why = run(tmp_path, {"PROJECT.md": ("Status: Quoting", 0)})
    assert ok is False
    assert "does not say" in why
    assert "paid" in why


def test_a_stale_file_missing_the_status_names_both_problems(tmp_path):
    ok, why = run(tmp_path, {"PROJECT.md": ("Status: Quoting", 40)})
    assert ok is False
    assert "not touched" in why
    assert "does not say" in why


# Cases where a careless message would blame the wrong thing.
def test_a_blank_status_does_not_blame_the_file(tmp_path):
    """An empty status can never be found in any text, so the message
    points at the blank field on the record, not at the file."""
    ok, why = run(tmp_path, {"PROJECT.md": ("Status: Paid", 0)}, status="")
    assert ok is False
    assert "no status" in why


def test_another_clients_folder_is_not_searched(tmp_path):
    """Only the job's client folder is searched, so another client's file
    cannot make the check pass."""
    other = tmp_path / "Hilltop" / "Some Job"
    other.mkdir(parents=True)
    (other / "PROJECT.md").write_text("Status: Paid", encoding="utf-8")

    ok, why = run(tmp_path, {})
    assert ok is False
    assert "no markdown file" in why


def test_a_folder_that_cannot_be_found_is_not_a_missing_file(tmp_path):
    """A missing client folder and an empty one get different messages,
    because they need different fixes."""
    ctx = Context(FakePB({"clients": [CLIENT]}), project_root=tmp_path)
    subject = {"id": "j1", "client": "c1", "status": "paid",
               "updated": "2026-08-23T00:49:00Z"}
    ok, why = project_md_changelog_entry({}, subject, ctx)
    assert ok is False
    assert "no project folder" in why


def test_a_non_markdown_file_does_not_count(tmp_path):
    """Only .md files count. A recent non-markdown file in the folder is
    not a record of the status change."""
    ok, why = run(tmp_path, {"invoice-paid.txt": ("paid", 0)})
    assert ok is False
    assert "no markdown file" in why


def test_the_status_match_ignores_case(tmp_path):
    """Files written by hand say 'Paid' while PocketBase stores 'paid',
    so the match ignores case."""
    ok, _ = run(tmp_path, {"PROJECT.md": ("**Status:** PAID", 0)})
    assert ok is True
