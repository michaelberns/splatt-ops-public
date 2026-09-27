"""
Tests for daemon/summary.py, the daily summary.

The day boundary is local midnight in Auckland, not UTC midnight.
Auckland is twelve or thirteen hours ahead of UTC, so counting a UTC day
would put the previous evening's completed work in today's numbers.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from daemon import summary

AUCKLAND = ZoneInfo("Pacific/Auckland")
UTC = ZoneInfo("UTC")


def task(due=None, description=""):
    return {"id": "9001", "content": "Northstar, belts", "due": due,
            "description": description}


def overdue_by(days):
    when = datetime.now(AUCKLAND).date().toordinal() - days
    return task(due={"date": datetime.fromordinal(when).date().isoformat()})


def record(**kw):
    base = {"id": "a1", "status": "open", "archived": False, "client": "c1"}
    base.update(kw)
    return base


# where a day starts

def test_a_day_starts_at_midnight_in_auckland_and_not_at_midnight_in_utc():
    now = datetime(2026, 8, 8, 9, 0, tzinfo=AUCKLAND)
    start = summary.day_start("Pacific/Auckland", now)

    assert start.astimezone(AUCKLAND).hour == 0
    assert start.astimezone(AUCKLAND).date().isoformat() == "2026-08-08"
    # Local midnight on the 8th is still the 7th in UTC.
    assert start.astimezone(UTC).date().isoformat() == "2026-08-07"


def test_the_boundary_is_the_same_moment_whatever_time_of_day_it_is_asked():
    morning = summary.day_start("Pacific/Auckland",
                                datetime(2026, 8, 8, 7, 0, tzinfo=AUCKLAND))
    evening = summary.day_start("Pacific/Auckland",
                                datetime(2026, 8, 8, 23, 30, tzinfo=AUCKLAND))

    assert morning == evening


def test_a_moment_given_in_utc_is_moved_to_the_local_day_first():
    """Eight in the evening UTC on the seventh is eight in the morning on
    the eighth in Auckland, so the day being counted is the eighth."""
    start = summary.day_start("Pacific/Auckland",
                              datetime(2026, 8, 7, 20, 0, tzinfo=UTC))

    assert start.astimezone(AUCKLAND).date().isoformat() == "2026-08-08"


# the counts

def test_the_counts_come_from_what_was_read_and_nothing_else():
    counts = summary.numbers([task(), overdue_by(3)], ["one", "two"],
                             [record(), record(id="a2")], max_pushes=3)

    assert counts["active"] == 2
    assert counts["completed_today"] == 2
    assert counts["overdue"] == 1


def test_a_task_pushed_to_its_limit_is_counted_as_needing_a_decision():
    pushed = overdue_by(5)
    pushed["description"] = ("auto rescheduled\nauto rescheduled\n"
                             "auto rescheduled")

    counts = summary.numbers([pushed], [], [], max_pushes=3)

    assert counts["needing_decision"] == 1


def test_a_task_that_is_late_but_still_being_pushed_is_not_a_decision_yet():
    counts = summary.numbers([overdue_by(2)], [], [], max_pushes=3)

    assert counts["overdue"] == 1
    assert counts["needing_decision"] == 0


def test_a_task_that_is_not_late_is_never_a_decision_however_many_pushes():
    old = task(description="auto rescheduled\nauto rescheduled\nauto rescheduled")

    assert summary.numbers([old], [], [], max_pushes=3)["needing_decision"] == 0


def test_a_task_with_no_due_date_is_not_late_and_does_not_crash_the_count():
    """A task with no due date (days_overdue returns None) is not
    counted as overdue and does not raise."""
    counts = summary.numbers([task(), task(due={})], [], [], max_pushes=3)

    assert counts["overdue"] == 0
    assert counts["active"] == 2


def test_open_records_with_no_client_are_counted_because_nobody_can_find_them():
    records = [record(), record(id="a2", client=""), record(id="a3", client="")]

    assert summary.numbers([], [], records, max_pushes=3)["unlinked"] == 2


def test_a_finished_record_with_no_client_is_not_worth_counting():
    records = [record(id="a2", client="", status="completed"),
               record(id="a3", client="", archived=True)]

    assert summary.numbers([], [], records, max_pushes=3)["unlinked"] == 0
