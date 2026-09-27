"""
Tests for daemon/reverse.py, the sending half of reverse sync.

A marker on a record (source="dashboard", or archived=True) means work is
outstanding, so it may only be cleared once Todoist has accepted the
change. Most of these tests check that a marker survives every kind of
failure, so the next pass can try again.
"""

from __future__ import annotations

import logging

from core import reverse
from daemon import reverse as runner

from tests.fakes import FakeTodoist

LOG = logging.getLogger("test")


class FakePB:
    """Enough of a PocketBase to hold records and be written to."""

    def __init__(self, records):
        self.records = {r["id"]: dict(r) for r in records}
        self.failing = set()

    def list_all(self, collection, filter_str="", sort=""):
        return [dict(r) for r in self.records.values()]

    def update(self, collection, record_id, payload):
        if record_id in self.failing:
            raise RuntimeError("PocketBase said no")
        self.records[record_id].update(payload)
        return self.records[record_id]


class BrokenTodoist(FakeTodoist):
    def update_task(self, task_id, **fields):
        raise RuntimeError("Todoist said no")

    def complete_task(self, task_id):
        raise RuntimeError("Todoist said no")

    def add_comment(self, task_id, content):
        return False


def record(**kw):
    base = {"id": "a1", "todoist_id": "9001", "content": "Northstar, belts",
            "description": "", "due_date": "", "priority": 0, "labels": "",
            "source": "sync", "archived": False, "archived_reason": "",
            "archived_at": "", "status": "open"}
    base.update(kw)
    return base


def run(records, todoist=None):
    pb = FakePB(records)
    todoist = todoist or FakeTodoist()
    item = reverse.plan(pb.list_all("assignments"))
    done, failures = runner.apply(todoist, pb, item, LOG)
    return pb, todoist, item, done, failures


def test_an_edit_reaches_todoist_and_the_marker_is_cleared():
    pb, todoist, _, done, failures = run(
        [record(source="dashboard", content="Northstar, belts and guides")])

    assert todoist.rescheduled[0][0] == "9001"
    assert todoist.rescheduled[0][1]["content"] == "Northstar, belts and guides"
    assert pb.records["a1"]["source"] == "sync"
    assert done["edited"] == 1
    assert failures == []


def test_an_edit_todoist_refused_leaves_the_marker_alone():
    """When Todoist refuses an edit the marker stays, so the next pass
    tries again."""
    pb, _, _, done, failures = run([record(source="dashboard")],
                                   todoist=BrokenTodoist())

    assert pb.records["a1"]["source"] == "dashboard"
    assert done["edited"] == 0
    assert len(failures) == 1


def test_an_archive_gets_its_comment_before_the_task_is_closed():
    pb, todoist, _, done, _ = run(
        [record(archived=True, archived_reason="COMPLETE: parts arrived")])

    assert todoist.comments == [("9001", reverse.comment("complete", "parts arrived"))]
    assert todoist.completed == ["9001"]
    assert pb.records["a1"]["status"] == "completed"
    assert done["closed"] == 1


def test_a_task_todoist_would_not_close_is_not_marked_completed_here():
    """When Todoist will not close the task, the record stays open, so
    the dashboard and Todoist agree."""
    pb, _, _, done, failures = run(
        [record(archived=True, archived_reason="archive")], todoist=BrokenTodoist())

    assert pb.records["a1"]["status"] == "open"
    assert done["closed"] == 0
    assert len(failures) == 1


def test_a_note_that_would_not_post_does_not_stop_the_task_closing():
    """A comment that fails to post does not stop the task closing."""

    class NoComments(FakeTodoist):
        def add_comment(self, task_id, content):
            return False

    pb, todoist, _, done, failures = run(
        [record(archived=True, archived_reason="archive")], todoist=NoComments())

    assert todoist.completed == ["9001"]
    assert pb.records["a1"]["status"] == "completed"
    assert failures == []


def test_the_delete_button_closes_the_task_and_keeps_the_record():
    """The delete button closes the Todoist task and marks the record
    completed. The record itself is kept, unchanged."""
    pb, todoist, _, done, _ = run(
        [record(archived=True, archived_reason="DELETE: duplicate")])

    assert todoist.completed == ["9001"]
    assert pb.records["a1"]["status"] == "completed"
    assert pb.records["a1"]["content"] == "Northstar, belts"


def test_a_record_with_no_todoist_task_is_settled_without_sending_anything():
    pb, todoist, _, done, _ = run([record(source="dashboard", todoist_id="")])

    assert todoist.rescheduled == []
    assert pb.records["a1"]["source"] == "sync"
    assert done["settled"] == 1


def test_an_archived_record_with_no_todoist_task_is_closed_locally():
    pb, todoist, _, done, _ = run(
        [record(archived=True, todoist_id="", archived_reason="archive")])

    assert todoist.completed == []
    assert todoist.comments == []
    assert pb.records["a1"]["status"] == "completed"
    assert done["closed"] == 1


def test_one_bad_record_does_not_stop_the_rest_of_the_pass():
    pb = FakePB([record(id="a1", source="dashboard"),
                 record(id="a2", todoist_id="9002", source="dashboard")])
    pb.failing.add("a1")
    todoist = FakeTodoist()
    item = reverse.plan(pb.list_all("assignments"))
    done, failures = runner.apply(todoist, pb, item, LOG)

    assert done["edited"] == 1
    assert len(failures) == 1
    assert pb.records["a2"]["source"] == "sync"


def test_verify_reads_the_work_back_and_says_nothing_when_it_landed():
    pb, _, item, _, _ = run([record(source="dashboard"),
                             record(id="a2", todoist_id="9002", archived=True,
                                    archived_reason="archive")])

    assert runner.verify(pb, item) == []


def test_verify_says_so_when_a_marker_is_still_sitting_there():
    """verify checks the whole plan, not just the parts that succeeded,
    so an edit that never reached Todoist is reported here as well as in
    the failures from apply."""
    pb, _, item, _, _ = run([record(source="dashboard")], todoist=BrokenTodoist())

    assert "still marked as a dashboard edit" in " ".join(runner.verify(pb, item))


def test_verify_complains_when_a_task_reported_closed_is_still_open():
    pb, _, item, _, _ = run([record(archived=True, archived_reason="archive")])
    pb.records["a1"]["status"] = "open"

    assert "still open" in " ".join(runner.verify(pb, item))
