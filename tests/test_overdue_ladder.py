"""
Tests for the overdue engine's ladder (daemon/overdue.py), one rung at a
time.

Every rung is measured from the real due date, not from the parked date
in Todoist. The first test pins that down: a task whose Todoist date is
yesterday but whose real due date is 49 days ago is 49 days late.

  day 1    move to Overdue, park on yesterday, date only
  day 3    tag @escalated
  day 7    Telegram to ops, once
  day 14   tag @stall-warning, say when it will stall, and do not move it
  day 28   move to Stalled and clear the due date

Waiting work skips the ladder and is chased instead. The suite runs on
TODAY = Thursday 2026-08-27 (tests/conftest_splatt.py).
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daemon.overdue import ALERT_MARKER, OverdueEngine
from tests.conftest_splatt import TODAY, FakeLedger, FakeNotifier, FakeSettings, task
from tests.fakes import SECTIONS, FakeTodoist


def engine(tasks, notifier=None, ledger=None, dry_run=False, settings=None):
    return OverdueEngine(
        FakeTodoist(tasks),
        settings or FakeSettings(),
        notifier=notifier,
        ledger=ledger,
        dry_run=dry_run,
    )


def run(tasks, **kwargs):
    eng = engine(tasks, **kwargs)
    eng.run(today=TODAY)
    return eng


def fields_for(eng, task_id="6aaaaaaaaaaaaaaa"):
    for written_id, fields in eng.todoist.rescheduled:
        if written_id == task_id:
            return fields
    return {}


# age comes from the real due date
def test_age_comes_from_the_real_date_not_the_parked_one():
    """A task parked on yesterday with a real due date 49 days ago is 49
    days late, which is past the stall threshold, so it moves to Stalled.
    """
    late = task(days_late=49, due=TODAY - timedelta(days=1))
    eng = run([late])
    assert eng.todoist.moved == [("6aaaaaaaaaaaaaaa", "stalled")]
    assert any("49 days" in str(a) for a in eng.actions)


def test_the_push_count_no_longer_caps_the_age():
    """Three "auto rescheduled" lines do not limit the age: a task 40
    days past its real due date still stalls."""
    history = "\n".join(["auto rescheduled"] * 3)
    eng = run([task(days_late=40, description=history)])
    assert eng.todoist.moved == [("6aaaaaaaaaaaaaaa", "stalled")]


# the rungs
def test_a_task_not_yet_late_is_left_alone():
    eng = run([task(days_late=0)])
    assert eng.actions == []
    assert eng.todoist.moved == []
    assert eng.todoist.rescheduled == []


def test_day_one_moves_it_to_overdue_and_parks_it_on_yesterday_date_only():
    eng = run([task(days_late=1)])
    assert eng.todoist.moved == [("6aaaaaaaaaaaaaaa", "overdue")]
    assert fields_for(eng)["due_string"] == "2026-08-26"


def test_day_one_does_not_escalate():
    eng = run([task(days_late=1)])
    assert "labels" not in fields_for(eng)


def test_day_three_tags_it_escalated():
    eng = run([task(days_late=3)])
    fields = fields_for(eng)
    assert fields["labels"] == ["escalated"]
    assert "ESCALATED" in fields["description"]


def test_the_escalated_tag_is_not_written_again_on_the_next_sweep():
    """A task that already has @escalated gets neither the label nor the
    ESCALATED note again on the next sweep."""
    eng = run([task(days_late=4, labels=["escalated"])])
    fields = fields_for(eng)
    assert "labels" not in fields
    assert "ESCALATED" not in fields["description"]


def test_day_seven_reaches_a_human():
    notifier = FakeNotifier()
    eng = run([task(days_late=7, content="draw up the CAP-100 bracket")],
              notifier=notifier)
    assert len(notifier.messages) == 1
    assert "draw up the CAP-100 bracket" in notifier.messages[0]


def test_day_six_does_not_reach_a_human_yet():
    notifier = FakeNotifier()
    run([task(days_late=6)], notifier=notifier)
    assert notifier.messages == []


def test_a_human_is_only_told_once_not_every_morning():
    """The first alert writes ALERT_MARKER into the description, and a
    task that already carries it is not alerted about again."""
    notifier = FakeNotifier()
    eng = run([task(days_late=9)], notifier=notifier)
    assert ALERT_MARKER in fields_for(eng)["description"]

    already = task(days_late=10, description="2026-08-20: %s at 9 days late."
                                             % ALERT_MARKER)
    second = FakeNotifier()
    run([already], notifier=second)
    assert second.messages == []


def test_day_fourteen_warns_and_does_not_move_the_task():
    """At day 14 the task is tagged @stall-warning but stays in Overdue,
    parked on yesterday."""
    eng = run([task(days_late=14, labels=["escalated"])])
    fields = fields_for(eng)
    assert eng.todoist.moved == [("6aaaaaaaaaaaaaaa", "overdue")]
    assert "stall-warning" in fields["labels"]
    assert fields["due_string"] == "2026-08-26"


def test_the_warning_says_the_exact_day_it_will_stall():
    """The day 14 note names the date the task will stall."""
    eng = run([task(days_late=14)])
    # Real due 2026-08-13 plus 28 days.
    assert "2026-09-10" in fields_for(eng)["description"]


def test_day_twenty_seven_is_still_only_a_warning():
    eng = run([task(days_late=27, labels=["escalated", "stall-warning"])])
    assert eng.todoist.moved == [("6aaaaaaaaaaaaaaa", "overdue")]
    assert fields_for(eng)["due_string"] == "2026-08-26"


def test_day_twenty_eight_moves_it_to_stalled_and_clears_the_date():
    """At day 28 the task moves to Stalled and its due date is cleared
    ("no date"), which takes it out of Today, Upcoming and overdue
    filters."""
    eng = run([task(days_late=28, labels=["escalated", "stall-warning"])])
    assert eng.todoist.moved == [("6aaaaaaaaaaaaaaa", "stalled")]
    assert fields_for(eng)["due_string"] == "no date"


def test_a_stalled_task_keeps_its_real_due_date_in_the_description():
    """A stalled task keeps its real due date on the pinned line, which
    is the only record of it once the Todoist date is cleared."""
    eng = run([task(days_late=30)])
    assert "📌 Real due: 2026-07-28" in fields_for(eng)["description"]


def test_a_stalled_task_becomes_a_decision_for_the_operator():
    eng = run([task(days_late=30)])
    assert len(eng.needs_decision) == 1
    _, age, reason = eng.needs_decision[0]
    assert age == 30
    assert "30 days overdue" in reason


def test_the_thresholds_come_from_settings():
    settings = FakeSettings({"overdue.stall_after_days": 10})
    eng = run([task(days_late=12)], settings=settings)
    assert eng.todoist.moved == [("6aaaaaaaaaaaaaaa", "stalled")]


# waiting work skips the ladder
def test_work_waiting_on_a_client_is_never_escalated_against_the_operator():
    """A task waiting on a client goes to Waiting on Client and is not
    tagged @escalated, however late it is."""
    waiting = task(days_late=20, content="QU-7994 quote, awaiting client approval")
    eng = run([waiting])
    assert eng.todoist.moved == [("6aaaaaaaaaaaaaaa", "waiting_client")]
    assert "escalated" not in fields_for(eng).get("labels", [])


def test_work_waiting_on_a_supplier_goes_to_its_own_column():
    waiting = task(days_late=20, content="awaiting Coastline freight ETA")
    eng = run([waiting])
    assert eng.todoist.moved == [("6aaaaaaaaaaaaaaa", "waiting_supplier")]


def test_a_waiting_task_is_given_a_real_chase_slot_in_working_hours():
    """A chase is booked at a real time of day in working hours, unlike
    parked overdue work, which gets a date only."""
    eng = run([task(days_late=20, content="awaiting Coastline freight ETA")])
    due = fields_for(eng)["due_string"]
    # Three business days after Thursday the 27th is Tuesday the 1st.
    assert due.startswith("2026-09-01 ")
    assert due.endswith(" 08:00")


def test_a_client_waits_longer_than_a_supplier():
    eng = run([task(days_late=20, content="awaiting client approval of the quote")])
    # Five business days after Thursday the 27th is Thursday the 3rd.
    assert fields_for(eng)["due_string"].startswith("2026-09-03 ")


def test_a_chase_already_booked_for_a_future_day_is_left_alone():
    """A waiting task whose due date (its chase date) is still in the
    future is not re-dated."""
    pending = task(
        days_late=20,
        content="awaiting client approval",
        due=TODAY + timedelta(days=3),
    )
    eng = run([pending])
    assert "due_string" not in fields_for(eng)


def test_two_chases_with_no_reply_stops_and_hands_it_over():
    """After two chases the task moves to Stalled, its date is cleared,
    and it becomes a decision."""
    chased = task(
        days_late=30,
        content="awaiting client approval",
        description="chase scheduled for x\nchase scheduled for y",
    )
    eng = run([chased])
    assert eng.todoist.moved == [("6aaaaaaaaaaaaaaa", "stalled")]
    assert fields_for(eng)["due_string"] == "no date"
    assert len(eng.needs_decision) == 1


# things that must not change
def test_the_engine_never_completes_or_deletes_a_task():
    tasks = [
        task("6aaaaaaaaaaaaaaa", 1),
        task("6bbbbbbbbbbbbbbb", 15),
        task("6cccccccccccccccc", 40),
    ]
    eng = run(tasks, notifier=FakeNotifier())
    assert eng.todoist.completed == []
    assert len(eng.todoist.tasks()) == 3


def test_a_task_already_in_the_right_column_is_not_moved_again():
    """No move call is made for a task already in the target column,
    but the task is still updated."""
    settled = task(days_late=2, section=SECTIONS["overdue"])
    eng = run([settled])
    assert eng.todoist.moved == []
    assert eng.todoist.rescheduled


def test_every_action_reaches_the_ledger():
    ledger = FakeLedger()
    eng = run([task(days_late=4)], ledger=ledger)
    assert len(ledger.entries) == len(eng.actions)
    assert {e["collection"] for e in ledger.entries} == {"todoist_task"}
    assert {e["source"] for e in ledger.entries} == {"overdue_engine"}


def test_a_dry_run_writes_nothing_anywhere():
    ledger = FakeLedger()
    notifier = FakeNotifier()
    tasks = [task("6aaaaaaaaaaaaaaa", 1), task("6bbbbbbbbbbbbbbb", 9),
             task("6cccccccccccccccc", 40)]
    eng = run(tasks, notifier=notifier, ledger=ledger, dry_run=True)

    assert eng.todoist.moved == []
    assert eng.todoist.rescheduled == []
    assert eng.todoist.completed == []
    assert notifier.messages == []
    assert ledger.entries == []
    assert all(a.kind.startswith("would") for a in eng.actions)


def test_the_engine_refuses_to_run_when_a_column_is_missing():
    """With a required column missing from settings, nothing is moved
    or updated and the failure names the missing column."""
    todoist = FakeTodoist([task(days_late=5)], sections={"overdue": "sec_overdue"})
    eng = OverdueEngine(todoist, FakeSettings())
    eng.run(today=TODAY)

    assert eng.actions == []
    assert todoist.moved == []
    assert todoist.rescheduled == []
    assert any("not configured" in f for f in eng.failures)
    assert any("stalled" in f for f in eng.failures)


def test_a_todoist_read_failure_is_recorded_rather_than_crashing():
    class Broken(FakeTodoist):
        def tasks(self, project_id=None, section_id=None):
            raise RuntimeError("410 Gone")

    eng = OverdueEngine(Broken(), FakeSettings())
    assert eng.run(today=TODAY) == []
    assert any("410 Gone" in f for f in eng.failures)


def test_one_bad_task_does_not_stop_the_rest_of_the_sweep():
    class HalfBroken(FakeTodoist):
        def move_to_section(self, task_id, section):
            if task_id == "6aaaaaaaaaaaaaaa":
                raise RuntimeError("section not found")
            return super().move_to_section(task_id, section)

    eng = OverdueEngine(
        HalfBroken([task("6aaaaaaaaaaaaaaa", 2), task("6bbbbbbbbbbbbbbb", 2)]),
        FakeSettings(),
    )
    eng.run(today=TODAY)
    assert any("could not move" in f for f in eng.failures)
    assert eng.todoist.moved == [("6bbbbbbbbbbbbbbb", "overdue")]


def test_the_labels_a_person_put_on_the_task_are_not_wiped():
    """Todoist replaces the whole label list on update, so the existing
    labels are sent back, in order, with the new one appended."""
    eng = run([task(days_late=3, labels=["coastline", "workshop"])])
    assert fields_for(eng)["labels"] == ["coastline", "workshop", "escalated"]


def test_the_summary_says_nothing_when_nothing_was_late():
    eng = run([task(days_late=0)])
    assert eng.summary() == "nothing was overdue"


def test_the_summary_flags_how_many_need_a_decision():
    eng = run([task("6aaaaaaaaaaaaaaa", 1), task("6bbbbbbbbbbbbbbb", 40)],
              notifier=FakeNotifier())
    assert "1 need a decision" in eng.summary()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


# the operator's own filing wins over the classifier
def test_a_manual_move_is_not_undone_the_next_morning():
    """A task the operator filed in Waiting on Supplier stays there, even
    though the classifier would read "Chase the gripper quote" as a
    client wait.
    """
    filed = task(days_late=6, content="Chase the gripper quote",
                 section=SECTIONS["waiting_supplier"])
    eng = run([filed])
    assert eng.todoist.moved == []


def test_the_chase_clock_follows_the_column_he_filed_it_in():
    """The chase interval follows the column the task was filed in:
    the same text gets a different chase date in Waiting on Supplier (3
    business days) than in Waiting on Client (5)."""
    supplier = task(days_late=6, task_id="6a",
                    content="Chase the gripper quote",
                    section=SECTIONS["waiting_supplier"])
    client = task(days_late=6, task_id="6b",
                  content="Chase the gripper quote",
                  section=SECTIONS["waiting_client"])
    eng = run([supplier, client])
    dates = {tid: f.get("due_string") or f.get("due_datetime")
             for tid, f in eng.todoist.rescheduled}
    assert dates["6a"] != dates["6b"]


def test_filing_in_overdue_does_not_stop_it_being_recognised_as_waiting():
    """Only the two waiting columns are read as an instruction. A task
    sitting in Overdue is still classified from its text."""
    parked = task(days_late=6, content="Chase Coastline on the shipment ETA",
                  section=SECTIONS["overdue"])
    eng = run([parked])
    assert eng.todoist.moved == [("6aaaaaaaaaaaaaaa", "waiting_supplier")]


# waiting work has a floor too
def test_a_wait_nobody_ever_chased_stalls_once_it_is_a_month_cold():
    """A wait that was never chased and is past the stall threshold goes
    straight to Stalled with its date cleared, instead of getting a first
    chase email on a thread that has been quiet for over a month.
    """
    cold = task(days_late=40, content="QU-7994, awaiting client approval")
    eng = run([cold])
    assert eng.todoist.moved == [("6aaaaaaaaaaaaaaa", "stalled")]
    assert fields_for(eng)["due_string"] == "no date"


def test_a_cold_wait_is_raised_as_a_decision_rather_than_filed_quietly():
    cold = task(days_late=40, content="QU-7994, awaiting client approval")
    eng = run([cold], notifier=FakeNotifier())
    assert eng.needs_decision
    assert "never chased" in eng.needs_decision[0][2]


def test_a_wait_already_being_chased_stays_on_the_chase_ladder_however_old():
    """A wait with at least one chase stays on the chase ladder however
    old it is; the stall floor only applies to threads never chased."""
    working = task(days_late=40, content="QU-7994, awaiting client approval",
                   description="2026-08-01: chase scheduled for 2026-08-05")
    eng = run([working])
    assert eng.todoist.moved == [("6aaaaaaaaaaaaaaa", "waiting_client")]


def test_a_wait_inside_the_month_is_chased_as_normal():
    fresh = task(days_late=6, content="QU-7994, awaiting client approval")
    eng = run([fresh])
    assert eng.todoist.moved == [("6aaaaaaaaaaaaaaa", "waiting_client")]


# placements made by the operator
def test_a_late_task_moved_to_a_future_day_is_left_entirely_alone():
    """A late task the operator moved to a future day is not touched
    until that day has passed, however late its real due date is."""
    eng = run([task(days_late=5, due=TODAY + timedelta(days=2))])
    assert eng.actions == []
    assert eng.todoist.moved == []
    assert eng.todoist.rescheduled == []


def test_a_late_task_with_a_time_of_day_keeps_its_slot():
    """A task with a due time of day keeps its slot: the ladder still
    runs (it moves to Overdue), but no park date is written."""
    from datetime import datetime, time
    slotted = task(days_late=3, due=datetime.combine(
        TODAY - timedelta(days=1), time(14, 0)))
    eng = run([slotted])
    assert eng.todoist.moved == [("6aaaaaaaaaaaaaaa", "overdue")]
    assert "due_string" not in fields_for(eng)
