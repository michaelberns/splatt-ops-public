"""
Tests for the join between the write ledger's writers and its reader.

The ledger (core/ledger.py) only works if everything that records claims
and the validator that re-reads them use the same file and the same
vocabulary. These tests pin down:

  1. One file. The path comes from `ledger_path()` inside the repo, and no
     other source file names a ledger file of its own. If a writer used a
     different file, the validator would see an empty run and pass it.
  2. One Todoist collection name. The overdue engine records Todoist
     moves under TODOIST_COLLECTION, and the validator recognises the same
     constant and checks those claims against Todoist instead of
     PocketBase (where they would all look missing).
  3. The validator's handling of Todoist claims: present, vanished,
     Todoist unavailable (skipped, not failed), deletes, missing ids.
  4. The round trip: run the real overdue engine against a real ledger
     file, hand that file to the real validator check, and require a
     pass; then make Todoist forget the task and require a failure.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from core.ledger import TODOIST_COLLECTION, WriteLedger, ledger_path
from daemon.overdue import OverdueEngine
from tests.fakes import FakePB, FakeTodoist
from validator import result
from validator.checks.standing import reread_claimed_writes
from validator.context import Context
from validator.rules import Rule

TODAY = date(2026, 8, 8)

# Files allowed to spell out a ledger file name: only the module that
# defines it.
LEDGER_NAME_IS_ALLOWED_IN = {"core/ledger.py"}

# Files allowed to spell the Todoist collection name as a literal.
COLLECTION_NAME_IS_ALLOWED_IN = {"core/ledger.py"}

SOURCE_DIRS = ("core", "daemon", "validator", "tools", "bin")


def make_rule(**kwargs):
    raw = {
        "id": "writes_landed",
        "severity": "block",
        "bypassable": False,
        "check": "reread_claimed_writes",
        "last_reviewed": datetime.now(timezone.utc).date(),
    }
    raw.update(kwargs)
    return Rule("standing", raw)


def make_ctx(ledger, tasks=None, data=None):
    return Context(
        pb=FakePB(data or {}),
        todoist=FakeTodoist(tasks) if tasks is not None else None,
        ledger=ledger,
        started_at=datetime.now(timezone.utc),
        run_id="testrun",
    )


def task(task_id, days_late):
    due = TODAY - timedelta(days=days_late)
    return {
        "id": task_id,
        "content": "chase the freight quote",
        "description": "",
        "priority": 1,
        "labels": [],
        "due": {"date": due.isoformat()},
    }


class FakeSettings:
    def __init__(self, repo_root=None):
        self.repo_root = repo_root
        self.values = {
            "overdue.reschedule_after_days": 1,
            "overdue.escalate_after_days": 3,
            "overdue.alert_after_days": 7,
            "overdue.max_auto_reschedules": 3,
            "overdue.priority_on_escalate": "p1",
            "overdue.escalated_label": "escalated",
        }

    def get(self, dotted, default=None):
        return self.values.get(dotted, default)


def source_files():
    for folder in SOURCE_DIRS:
        root = REPO_ROOT / folder
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path


# one file, named in one place
def test_the_ledger_path_comes_from_the_repo_root():
    assert ledger_path().is_relative_to(REPO_ROOT)
    assert ledger_path().name == "writes.jsonl"
    assert ledger_path().parent.name == "state"


def test_settings_can_move_the_ledger_but_only_as_a_whole():
    """Passing settings moves the whole ledger with the repo root, but
    the file name and folder stay fixed, so no caller can choose its own
    file."""
    conf = FakeSettings(repo_root=Path("/somewhere/else"))
    assert ledger_path(conf) == Path("/somewhere/else/state/writes.jsonl")


def test_the_ledger_is_no_longer_outside_the_repo():
    """The default ledger is exactly <repo>/state/writes.jsonl.

    The check compares against the repo root rather than looking for
    folder names, so it gives the same answer wherever the repo is
    checked out. It also rejects the `.splatt-ops-ledger` dot-file name,
    a ledger location outside the repo.
    """
    path = ledger_path()
    assert ".splatt-ops-ledger" not in str(path), (
        "the ledger points at a dot-file outside the repo: %s" % path)
    assert path == REPO_ROOT / "state" / "writes.jsonl", (
        "the ledger belongs inside this repo, not at %s" % path)


def test_nothing_else_names_a_ledger_file_for_itself():
    """No source file other than core/ledger.py contains a ledger file
    name, so every writer must ask core.ledger for the path."""
    offenders = []
    for path in source_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in LEDGER_NAME_IS_ALLOWED_IN:
            continue
        body = path.read_text(encoding="utf-8")
        for needle in ("writes.jsonl", ".splatt-ops-ledger"):
            if needle in body:
                offenders.append("%s names %s" % (rel, needle))
    assert offenders == [], offenders


def test_every_writer_gets_its_path_from_the_shared_function():
    """A file that builds a WriteLedger has to build it from ledger_path."""
    offenders = []
    for path in source_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in LEDGER_NAME_IS_ALLOWED_IN:
            continue
        body = path.read_text(encoding="utf-8")
        if "WriteLedger(" in body and "ledger_path" not in body:
            offenders.append(rel)
    assert offenders == [], offenders


# one collection name, named in one place
def test_the_writer_and_the_reader_share_the_collection_name():
    import daemon.overdue as overdue
    import validator.checks.standing as standing

    assert overdue.TODOIST_COLLECTION is TODOIST_COLLECTION
    assert standing.TODOIST_COLLECTION is TODOIST_COLLECTION


def test_nothing_spells_the_collection_name_out_by_hand():
    offenders = []
    for path in source_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in COLLECTION_NAME_IS_ALLOWED_IN:
            continue
        if '"todoist_task"' in path.read_text(encoding="utf-8"):
            offenders.append(rel)
    assert offenders == [], offenders


def test_the_collection_is_not_a_pocketbase_collection():
    """TODOIST_COLLECTION must never be a real PocketBase collection;
    if it were, the validator's special case for it would be wrong."""
    from core import schema

    assert TODOIST_COLLECTION not in schema.collections()


# the validator reading Todoist claims
def test_a_todoist_claim_is_not_looked_for_in_pocketbase(tmp_path):
    ledger = WriteLedger(tmp_path / "writes.jsonl", run_id="testrun")
    ledger.record("update", TODOIST_COLLECTION, "6aaaaaaaaaaaaaaa",
                  ["moved"], "overdue_engine")
    ctx = make_ctx(ledger, tasks=[task("6aaaaaaaaaaaaaaa", 1)])

    res = reread_claimed_writes(make_rule(), ctx)
    assert res.status == result.PASS, res.evidence
    assert "PocketBase" not in " ".join(res.evidence or [])


def test_a_todoist_claim_for_a_task_that_vanished_is_caught(tmp_path):
    ledger = WriteLedger(tmp_path / "writes.jsonl", run_id="testrun")
    ledger.record("update", TODOIST_COLLECTION, "6aaaaaaaaaaaaaaa",
                  ["moved"], "overdue_engine")
    ctx = make_ctx(ledger, tasks=[])

    res = reread_claimed_writes(make_rule(), ctx)
    assert res.status == result.FAIL
    assert "not in Todoist" in " ".join(res.evidence)


def test_the_kind_of_a_todoist_action_is_not_read_as_a_field(tmp_path):
    """For Todoist claims the overdue engine puts the kind of action
    ('moved', 'rescheduled', 'escalated') in the fields list. These are
    not task fields, and the validator must not try to re-read them."""
    ledger = WriteLedger(tmp_path / "writes.jsonl", run_id="testrun")
    ledger.record("update", TODOIST_COLLECTION, "6aaaaaaaaaaaaaaa",
                  ["moved", "rescheduled", "escalated"], "overdue_engine")
    ctx = make_ctx(ledger, tasks=[task("6aaaaaaaaaaaaaaa", 1)])

    res = reread_claimed_writes(make_rule(), ctx)
    assert res.status == result.PASS, res.evidence


def test_todoist_being_unavailable_is_a_skip_not_a_failure(tmp_path):
    """With Todoist unavailable, a Todoist claim cannot be checked either
    way, so it is skipped with a note rather than reported as failed.
    False failures would train people to ignore the check."""
    ledger = WriteLedger(tmp_path / "writes.jsonl", run_id="testrun")
    ledger.record("update", TODOIST_COLLECTION, "6aaaaaaaaaaaaaaa",
                  ["moved"], "overdue_engine")
    ctx = make_ctx(ledger, tasks=None)

    res = reread_claimed_writes(make_rule(), ctx)
    assert res.status == result.PASS
    assert any("not available" in note for note in ctx.notes), ctx.notes


def test_a_skipped_todoist_claim_is_not_counted_as_confirmed(tmp_path):
    """The pass summary counts only claims actually re-read, so skipped
    claims are not reported as confirmed."""
    ledger = WriteLedger(tmp_path / "writes.jsonl", run_id="testrun")
    ledger.record("update", TODOIST_COLLECTION, "6aaaaaaaaaaaaaaa",
                  ["moved"], "overdue_engine")
    ctx = make_ctx(ledger, tasks=None)

    res = reread_claimed_writes(make_rule(), ctx)
    assert "all 0 claimed writes confirmed" in res.summary


def test_pocketbase_claims_are_still_re_read_alongside(tmp_path):
    """PocketBase claims in the same run are still re-read, and a missing
    PocketBase record still fails the check."""
    ledger = WriteLedger(tmp_path / "writes.jsonl", run_id="testrun")
    ledger.record("update", TODOIST_COLLECTION, "6aaaaaaaaaaaaaaa",
                  ["moved"], "overdue_engine")
    ledger.record("create", "clients", "missing1", ["name"], "sync")
    ctx = make_ctx(ledger, tasks=[task("6aaaaaaaaaaaaaaa", 1)], data={"clients": []})

    res = reread_claimed_writes(make_rule(), ctx)
    assert res.status == result.FAIL
    assert "does not exist in PocketBase" in " ".join(res.evidence)


def test_a_todoist_delete_claim_is_confirmed_the_other_way_round(tmp_path):
    """A Todoist delete claim passes when the task is gone.

    No current writer produces one (the overdue engine never closes a
    task), but the branch is tested because an inverted check would raise
    no error: it would pass failed deletes and fail successful ones.
    """
    ledger = WriteLedger(tmp_path / "writes.jsonl", run_id="testrun")
    ledger.record("delete", TODOIST_COLLECTION, "6aaaaaaaaaaaaaaa", [], "somebody")
    ctx = make_ctx(ledger, tasks=[])

    res = reread_claimed_writes(make_rule(), ctx)
    assert res.status == result.PASS, res.evidence


def test_a_todoist_delete_claim_on_a_task_still_open_is_caught(tmp_path):
    ledger = WriteLedger(tmp_path / "writes.jsonl", run_id="testrun")
    ledger.record("delete", TODOIST_COLLECTION, "6aaaaaaaaaaaaaaa", [], "somebody")
    ctx = make_ctx(ledger, tasks=[task("6aaaaaaaaaaaaaaa", 1)])

    res = reread_claimed_writes(make_rule(), ctx)
    assert res.status == result.FAIL
    assert "still open" in " ".join(res.evidence)


def test_a_todoist_claim_with_no_task_id_is_still_reported(tmp_path):
    ledger = WriteLedger(tmp_path / "writes.jsonl", run_id="testrun")
    ledger.record("update", TODOIST_COLLECTION, "", ["moved"], "overdue_engine")
    ctx = make_ctx(ledger, tasks=[])

    res = reread_claimed_writes(make_rule(), ctx)
    assert res.status == result.FAIL
    assert "no record id" in " ".join(res.evidence)


# the round trip
def test_a_real_sweep_survives_a_real_validation(tmp_path):
    """End to end: the overdue engine writes claims to a real ledger
    file, and the validator reopens the same file by run id and confirms
    every claim. The ledger itself is not faked, because it is the thing
    under test."""
    path = tmp_path / "state" / "writes.jsonl"
    ledger = WriteLedger(path, run_id="testrun")
    tasks = [task("6aaaaaaaaaaaaaaa", 4), task("6bbbbbbbbbbbbbbb", 1)]

    engine = OverdueEngine(FakeTodoist(tasks), FakeSettings(), ledger=ledger)
    engine.run(today=TODAY)
    assert engine.actions, "the sweep did nothing, so this proves nothing"

    reader = WriteLedger(path, run_id="testrun")
    assert len(reader.entries()) == len(engine.actions)

    ctx = make_ctx(reader, tasks=tasks)
    res = reread_claimed_writes(make_rule(), ctx)
    assert res.status == result.PASS, res.evidence
    assert ctx.notes == []


def test_the_round_trip_still_fails_when_the_sweep_lies(tmp_path):
    """Same round trip, but Todoist no longer has the task. The check must
    fail and name the task, proving it can fail at all."""
    path = tmp_path / "state" / "writes.jsonl"
    ledger = WriteLedger(path, run_id="testrun")
    tasks = [task("6aaaaaaaaaaaaaaa", 4)]

    engine = OverdueEngine(FakeTodoist(tasks), FakeSettings(), ledger=ledger)
    engine.run(today=TODAY)

    ctx = make_ctx(WriteLedger(path, run_id="testrun"), tasks=[])
    res = reread_claimed_writes(make_rule(), ctx)
    assert res.status == result.FAIL
    assert "6aaaaaaaaaaaaaaa" in " ".join(res.evidence)


def test_no_ledger_at_all_is_a_skip_rather_than_a_pass(tmp_path):
    """An empty ledger and a missing ledger are reported differently.

    Both produce zero claims. An empty ledger means nothing was written,
    so the check passes. No ledger at all means nothing can be verified,
    so the check is skipped and the report shows it as unproven rather
    than proven.
    """
    empty = make_ctx(WriteLedger(tmp_path / "empty.jsonl", run_id="testrun"))
    assert reread_claimed_writes(make_rule(), empty).status == result.PASS

    absent = make_ctx(ledger=None)
    res = reread_claimed_writes(make_rule(), absent)
    assert res.status == result.SKIP
    assert res.blocking is False
