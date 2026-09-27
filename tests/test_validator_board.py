"""
Tests for the validator checks that read the Todoist board.

"The board" is the Todoist project laid out as seven columns (today,
overdue, upcoming, waiting_client, waiting_supplier, stalled, backlog).
These checks exist because the board can be wrong even when the overdue
engine is right: the operator moves tasks by hand, other tools write to
the same project, and a sweep can stop part way.

Each check is tested failing first, then passing. None of them may read
a task's priority; tests/test_no_priority_logic.py enforces that by
scanning the validator source, so this file only tests behaviour.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.fakes import SECTIONS, FakePB, FakeTodoist
from validator.checks import standing
from validator.context import Context
from validator.rules import Rule

NOW = datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc)
TODAY = NOW.date()


class Settings:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get(self, dotted, default=None):
        return self.values.get(dotted, default)


def rule(rule_id="test_rule"):
    return Rule("standing", {
        "id": rule_id,
        "severity": "block",
        "bypassable": False,
        "check": rule_id,
        "last_reviewed": TODAY,
    })


def board(*tasks, **kwargs):
    """A context holding nothing but a Todoist board."""
    return Context(
        pb=None,
        todoist=FakeTodoist(list(tasks)),
        settings=Settings(kwargs.get("settings")),
        started_at=NOW,
        run_id="testrun",
    )


def card(column, days_late=None, task_id="6aaaaaaaaaaaaaaa", due="parked",
         content="do the thing", pinned=True):
    """One task sitting in a column.

    days_late is measured from the REAL due date, which is what goes on
    the pinned line. `due` is the date Todoist displays; the default
    "parked" means yesterday, which is where the overdue engine parks late
    tasks whatever their real age. The checks must use the first and
    ignore the second.
    """
    description = ""
    if pinned and days_late is not None:
        real = TODAY - timedelta(days=days_late)
        description = "📌 Real due: %s | %d days late" % (real.isoformat(),
                                                          days_late)
    task = {
        "id": task_id,
        "content": content,
        "description": description,
        "section_id": SECTIONS[column],
        "labels": [],
        "priority": 4,
    }
    if due == "parked":
        task["due"] = {"date": (TODAY - timedelta(days=1)).isoformat()}
    elif due is not None:
        task["due"] = {"date": due}
    return task


# tasks left in Overdue past the stall threshold
def test_a_task_past_the_stall_threshold_in_overdue_is_a_failure():
    """Past the 28 day threshold and still in Overdue means the engine did
    not run or could not classify the task, so it is reported."""
    res = standing.overdue_not_stalled(rule(), board(card("overdue", 40)))
    assert res.failed
    assert "40 days" in res.evidence[0]


def test_a_task_inside_the_threshold_in_overdue_is_fine():
    res = standing.overdue_not_stalled(rule(), board(card("overdue", 12)))
    assert not res.failed


def test_the_threshold_comes_from_settings():
    late = card("overdue", 20)
    strict = board(late, settings={"overdue.stall_after_days": 14})
    assert standing.overdue_not_stalled(rule(), strict).failed
    relaxed = board(late, settings={"overdue.stall_after_days": 60})
    assert not standing.overdue_not_stalled(rule(), relaxed).failed


def test_age_is_read_off_the_pinned_line_not_the_displayed_date():
    """The task shows as one day late in Todoist (the parked date) but is
    really 159 days old. The check must report the real age."""
    res = standing.overdue_not_stalled(rule(), board(card("overdue", 159)))
    assert res.failed
    assert "159 days" in res.evidence[0]


def test_a_long_dead_task_already_in_stalled_is_not_flagged():
    """Stalled is where an old task belongs, so it is not reported."""
    res = standing.overdue_not_stalled(rule(), board(card("stalled", 90,
                                                          due=None)))
    assert not res.failed


def test_a_long_wait_on_a_client_is_not_an_overdue_failure():
    """A waiting column has its own life cycle, ending in Stalled after
    the chases run out, so this check ignores it."""
    res = standing.overdue_not_stalled(
        rule(), board(card("waiting_client", 90))
    )
    assert not res.failed


# the real due date is pinned
def test_a_late_task_with_no_pinned_line_is_a_failure():
    """Without the pinned real due date, the next sweep can only see the
    parked date and would treat an old task as one day late."""
    res = standing.real_due_is_pinned(
        rule(), board(card("overdue", 40, pinned=False))
    )
    assert res.failed
    assert "no real due date" in res.evidence[0]


def test_a_task_the_operator_typed_in_this_morning_needs_no_pinned_line():
    """Today, Upcoming and Backlog are not watched. Nothing has moved
    their dates yet, so a task added by hand has no pin and needs none."""
    for column in ("today", "upcoming", "backlog"):
        res = standing.real_due_is_pinned(
            rule(), board(card(column, pinned=False))
        )
        assert not res.failed, column


@pytest.mark.parametrize("column", ["overdue", "waiting_client",
                                    "waiting_supplier", "stalled"])
def test_every_managed_column_is_watched(column):
    res = standing.real_due_is_pinned(
        rule(), board(card(column, pinned=False, due=None))
    )
    assert res.failed


def test_a_pinned_line_further_down_the_description_does_not_count():
    """Only the header of the description holds the record. A quoted email
    in the body that happens to contain the same words does not count."""
    task = card("overdue", pinned=False)
    task["description"] = "notes from the call\n📌 Real due: 2026-01-01"
    assert standing.real_due_is_pinned(rule(), board(task)).failed


def test_a_guessed_date_still_counts_as_pinned():
    """A date marked "(guessed)" still counts as pinned: the backfill
    recorded its best estimate and labelled it as one."""
    task = card("overdue", pinned=False)
    task["description"] = "📌 Real due: 2026-03-02 (guessed) | 178 days late"
    assert not standing.real_due_is_pinned(rule(), board(task)).failed


# the header, and the mirror behind it
PROJECT = "📁 Project: SS Folders/Amberleaf/QU-8090 Large Quote/PROJECT.md"


def pinned_card(column="overdue", task_id="6aaaaaaaaaaaaaaa", days_late=40,
                header=True):
    """A task whose header has a Project line first, then a blank line,
    then the pin."""
    task = card(column, days_late, task_id=task_id)
    if header:
        task["description"] = PROJECT + "\n\n" + task["description"]
    return task


def board_with_mirror(*tasks, **mirrors):
    """A board plus the PocketBase assignments behind it, keyed by id."""
    records = [
        {"id": "rec_%s" % task_id, "todoist_id": task_id, "real_due": value}
        for task_id, value in mirrors.items()
    ]
    return Context(
        pb=FakePB({"assignments": records}),
        todoist=FakeTodoist(list(tasks)),
        settings=Settings(),
        started_at=NOW,
        run_id="testrun",
    )


def test_a_pin_under_the_project_line_is_still_a_pin():
    """A pin below the Project line is still in the header, so it counts."""
    assert not standing.real_due_is_pinned(rule(), board(pinned_card())).failed


def test_one_pinned_line_is_not_a_duplicate():
    res = standing.real_due_not_duplicated(rule(), board(pinned_card()))
    assert not res.failed


def test_two_pinned_lines_are_reported():
    """A second pin written above the recorded one: a guessed date on top,
    the recorded date underneath."""
    task = pinned_card()
    task["description"] = (
        "📌 Real due: 2026-08-20 (guessed) | 10 days late\n\n"
        + task["description"]
    )
    res = standing.real_due_not_duplicated(rule(), board(task))
    assert res.failed
    assert "2 pinned lines" in res.evidence[0]


def test_a_second_pin_down_in_the_body_is_not_a_duplicate():
    """Only the header holds the record, so a quoted email carrying the
    same words is not a second pin and must not raise an alarm."""
    task = pinned_card()
    task["description"] += "\n\nthey wrote:\n📌 Real due: 2020-01-01 | ages late"
    assert not standing.real_due_not_duplicated(rule(), board(task)).failed


def test_a_mirror_holding_the_same_date_passes():
    task = pinned_card(task_id="6abc")
    real = (TODAY - timedelta(days=40)).isoformat()
    ctx = board_with_mirror(task, **{"6abc": real + " 00:00:00.000Z"})
    assert not standing.real_due_mirror_agrees(rule(), ctx).failed


def test_an_empty_mirror_is_reported():
    """The pin is fine but the PocketBase copy is empty, so the copy that
    should survive an overwritten description protects nothing."""
    task = pinned_card(task_id="6abc")
    ctx = board_with_mirror(task, **{"6abc": ""})
    res = standing.real_due_mirror_agrees(rule(), ctx)
    assert res.failed
    assert "empty mirror" in res.evidence[0]


def test_a_mirror_holding_a_different_date_is_reported_not_repaired():
    task = pinned_card(task_id="6abc")
    ctx = board_with_mirror(task, **{"6abc": "2026-06-01 00:00:00.000Z"})
    res = standing.real_due_mirror_agrees(rule(), ctx)
    assert res.failed
    assert "PocketBase holds 2026-06-01" in res.evidence[0]


def test_a_task_with_no_pin_is_not_the_mirror_check_business():
    task = card("today", pinned=False, task_id="6abc")
    ctx = board_with_mirror(task, **{"6abc": ""})
    assert not standing.real_due_mirror_agrees(rule(), ctx).failed


def test_no_pocketbase_is_a_skip_and_not_a_pass():
    assert standing.real_due_mirror_agrees(
        rule(), board(pinned_card())
    ).status == "skip"


# stalled tasks carry no date
def test_a_stalled_task_with_a_due_date_is_a_failure():
    """A due date would put the task back into the daily filters, which
    is what the Stalled column exists to prevent."""
    res = standing.stalled_has_no_date(
        rule(), board(card("stalled", 90, due="2026-08-26"))
    )
    assert res.failed
    assert "2026-08-26" in res.evidence[0]


def test_a_dateless_stalled_task_passes():
    res = standing.stalled_has_no_date(
        rule(), board(card("stalled", 90, due=None))
    )
    assert not res.failed


def test_a_due_date_anywhere_else_is_none_of_this_checks_business():
    """Every other column is supposed to have a date."""
    tasks = [card("overdue", 5, task_id="6a"),
             card("waiting_client", 5, task_id="6b"),
             card("today", 0, task_id="6c")]
    assert not standing.stalled_has_no_date(rule(), board(*tasks)).failed


# shared behaviour
@pytest.mark.parametrize("check", [
    standing.overdue_not_stalled,
    standing.real_due_is_pinned,
    standing.real_due_not_duplicated,
    standing.stalled_has_no_date,
])
def test_no_todoist_is_a_skip_and_not_a_pass(check):
    """Without Todoist nothing is known, so the result is a skip, which
    the report lists as "not checked" rather than as a pass."""
    ctx = Context(pb=None, todoist=None, settings=Settings(),
                  started_at=NOW, run_id="testrun")
    assert check(rule(), ctx).status == "skip"


@pytest.mark.parametrize("check", [
    standing.overdue_not_stalled,
    standing.real_due_is_pinned,
    standing.real_due_not_duplicated,
    standing.stalled_has_no_date,
])
def test_a_task_in_a_column_this_system_does_not_manage_is_left_alone(check):
    """The operator can add columns of their own. A section id that is not
    in the settings is not managed by this system, so it is ignored."""
    task = card("overdue", 90, pinned=False)
    task["section_id"] = "sec_added_by_the_operator"
    assert not check(rule(), board(task)).failed


@pytest.mark.parametrize("check", [
    standing.overdue_not_stalled,
    standing.real_due_is_pinned,
    standing.real_due_not_duplicated,
    standing.stalled_has_no_date,
])
def test_an_empty_board_passes_rather_than_erroring(check):
    assert check(rule(), board()).status == "pass"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
