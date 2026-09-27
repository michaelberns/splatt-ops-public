"""
Tests for the scheduling arithmetic (core/planner.py).

The two rules checked most closely:

  overdue work parks on yesterday, date only, and midnight never counts
  as booked working time, so forty parked tasks do not make the day look
  full

  a chase gets a real slot in a real gap, never on top of something
  already booked

Also covered: reading estimates, business-day arithmetic, and reading a
task's date.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import planner as planner_mod
from core.planner import Day, Planner

# A Thursday, so the weekend is two days away and easy to step over.
TODAY = date(2026, 8, 27)


class FakeSettings:
    def __init__(self, overrides=None):
        self.values = dict(overrides or {})

    def get(self, dotted, default=None):
        return self.values.get(dotted, default)


def at(hour, minute=0, day=TODAY):
    return datetime.combine(day, time(hour, minute))


def booked(hour, minute=0, minutes=60, day=TODAY):
    """A task already sitting at a time of day."""
    return {
        "id": "6t%02d%02d" % (hour, minute),
        "description": "⏱ Est: %dm" % minutes,
        "due": {"date": at(hour, minute, day).isoformat()},
    }


# estimates
def test_an_estimate_is_read_out_of_the_description():
    assert planner_mod.estimate_minutes({"description": "⏱ Est: 45m"}) == 45
    assert planner_mod.estimate_minutes({"description": "Est: 30 mins"}) == 30
    assert planner_mod.estimate_minutes({"description": "Est: 2h"}) == 120


def test_no_estimate_means_a_short_one():
    """No estimate means DEFAULT_MINUTES (15): such tasks are usually
    quick replies."""
    assert planner_mod.estimate_minutes({}) == 15
    assert planner_mod.estimate_minutes({"description": "no numbers here"}) == 15


def test_a_zero_estimate_is_a_typo_not_an_instruction():
    """A zero estimate falls back to the default, because a task booked
    for no time would overlap whatever comes next."""
    assert planner_mod.estimate_minutes({"description": "Est: 0m"}) == 15


# business days
def test_weekends_do_not_count_as_days():
    """Waiting three days over a weekend means Monday, not Sunday."""
    friday = date(2026, 8, 28)
    assert planner_mod.add_business_days(friday, 1) == date(2026, 8, 31)
    assert planner_mod.add_business_days(friday, 3) == date(2026, 9, 2)


def test_five_business_days_from_a_thursday_is_the_next_thursday():
    assert planner_mod.add_business_days(TODAY, 5) == date(2026, 9, 3)


def test_a_weekend_date_rolls_forward_to_monday():
    assert planner_mod.next_business_day(date(2026, 8, 29)) == date(2026, 8, 31)
    assert planner_mod.next_business_day(TODAY) == TODAY


# the parking rule
def test_overdue_work_parks_on_yesterday_date_only():
    assert planner_mod.park_string(TODAY) == "2026-08-26"


def test_parking_writes_a_literal_date_not_the_word_yesterday():
    """Todoist reads "yesterday" in the account timezone when the request
    arrives, so a sweep near midnight could park a task on the wrong day.
    A literal date does not depend on when it is sent."""
    assert "yesterday" not in planner_mod.park_string(TODAY)


def test_a_task_parked_at_midnight_does_not_make_the_day_look_busy():
    """Tasks at midnight are parked, not booked, so forty of them leave
    08:00 to 17:00 completely free."""
    day = Day(TODAY)
    for i in range(40):
        day.add_task({
            "description": "⏱ Est: 45m",
            "due": {"date": datetime.combine(TODAY, time(0, 0)).isoformat()},
        })
    assert day.free_slot(45) == at(8, 0)


def test_a_task_with_only_a_date_and_no_time_does_not_block_anything():
    day = Day(TODAY)
    day.add_task({"due": {"date": TODAY.isoformat()}})
    assert day.free_slot(60) == at(8, 0)


# finding a gap
def test_an_empty_day_gives_the_start_of_the_working_day():
    assert Day(TODAY).free_slot(30) == at(8, 0)


def test_a_gap_is_found_after_what_is_already_booked():
    day = Day(TODAY)
    day.add(at(8, 0), 60)
    assert day.free_slot(30) == at(9, 15)


def test_the_buffer_is_dropped_rather_than_losing_the_gap():
    """A snug fit is still better than pushing the task to another day."""
    day = Day(TODAY)
    day.add(at(8, 0), 60)
    day.add(at(9, 30), 60)
    # 09:00 to 09:30 is exactly 30 minutes, so it only fits with no padding.
    assert day.free_slot(30) == at(9, 0)


def test_a_full_day_gives_nothing_rather_than_overlapping():
    day = Day(TODAY)
    day.add(at(8, 0), 9 * 60)
    assert day.free_slot(15) is None


def test_nothing_is_booked_past_the_end_of_the_working_day():
    day = Day(TODAY)
    day.add(at(8, 0), 8 * 60 + 30)
    assert day.free_slot(60) is None
    # 16:30 is free, and the buffer still fits before 17:00, so it takes it.
    assert day.free_slot(15) == at(16, 45)


def test_the_working_day_comes_from_settings():
    settings = FakeSettings({
        "planner.working_hours.start": 6,
        "planner.working_hours.end": 12,
    })
    slot = Planner(settings).find_slot(TODAY, 30)
    assert slot == at(6, 0)


# searching forward
def test_a_full_day_pushes_the_search_to_the_next_day():
    full = [booked(8, 0, minutes=9 * 60)]
    slot = Planner(None, full).find_slot(TODAY, 60)
    assert slot == at(8, 0, date(2026, 8, 28))


def test_the_search_steps_over_the_weekend():
    friday = date(2026, 8, 28)
    full = [booked(8, 0, minutes=9 * 60, day=friday)]
    slot = Planner(None, full).find_slot(friday, 60)
    assert slot == at(8, 0, date(2026, 8, 31))


def test_a_chase_asked_for_on_a_saturday_lands_on_monday():
    assert Planner().find_slot(date(2026, 8, 29), 30) == at(8, 0, date(2026, 8, 31))


def test_nothing_free_inside_the_window_returns_nothing():
    """With no free slot in the search window the result is None, and the
    caller reports it instead of double-booking."""
    settings = FakeSettings({"planner.search_days": 2})
    tasks = [
        booked(8, 0, minutes=9 * 60, day=TODAY),
        booked(8, 0, minutes=9 * 60, day=date(2026, 8, 28)),
    ]
    assert Planner(settings, tasks).find_slot(TODAY, 60) is None


def test_reserving_stops_two_tasks_in_one_sweep_sharing_a_slot():
    schedule = Planner()
    first = schedule.reserve(TODAY, 60)
    second = schedule.reserve(TODAY, 60)
    assert first == at(8, 0)
    # The first one runs to 09:00, so the second cannot start before that.
    assert second >= at(9, 0)


def test_a_reserved_slot_is_written_the_way_todoist_takes_it():
    assert Planner.as_due_string(at(9, 30)) == "2026-08-27 09:30"
    assert Planner.as_due_string(None) == ""


# reading a task's date
def test_a_date_only_task_has_no_time_of_day():
    assert planner_mod.task_datetime({"due": {"date": "2026-08-27"}}) is None
    assert planner_mod.task_date({"due": {"date": "2026-08-27"}}) == TODAY


def test_a_task_with_a_time_reads_back_as_that_time():
    task = {"due": {"date": "2026-08-27T09:30:00"}}
    assert planner_mod.task_datetime(task) == at(9, 30)


def test_an_undated_task_reads_as_no_date_rather_than_crashing():
    assert planner_mod.task_datetime({}) is None
    assert planner_mod.task_date({}) is None
    assert planner_mod.task_date({"due": {"date": "not a date"}}) is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
