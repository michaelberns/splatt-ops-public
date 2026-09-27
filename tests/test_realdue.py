"""
Tests for the real due date (core/realdue.py).

The two central properties are:

  a date already recorded cannot be overwritten (write once)
  a date that had to be estimated is labelled "(guessed)"

The rest cover the resolve order, recovery from legacy escalation notes,
the pin line format, the PocketBase mirror, and the pin's position under
other header lines such as the project line.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import realdue

TODAY = date(2026, 8, 30)


# reading the pinned line
def test_a_pinned_line_is_read_back():
    found = realdue.parse("📌 Real due: 2026-07-12 | 49 days late")
    assert found.date == date(2026, 7, 12)
    assert found.source == realdue.RealDue.PINNED
    assert not found.guessed


def test_a_guessed_pin_stays_guessed_when_it_is_read_back():
    """The "(guessed)" label is read back from the line, not recomputed,
    so a guess stays a guess after a round trip."""
    found = realdue.parse("📌 Real due: 2026-07-12 (guessed) | 49 days late")
    assert found.guessed


def test_only_the_first_line_counts_as_the_record():
    """A pin after the first body line is not the record, because a
    quoted email in the body can contain the same words."""
    description = "chase the client\n📌 Real due: 2020-01-01 | ages late"
    assert realdue.parse(description) is None


def test_a_description_with_no_pin_reads_as_no_record():
    assert realdue.parse("") is None
    assert realdue.parse(None) is None
    assert realdue.parse("just some notes") is None


# recovering a date from legacy escalation notes
def test_the_old_engine_notes_give_back_an_exact_date():
    """2026-08-19 minus 38 days is 2026-07-12."""
    note = "ESCALATED 2026-08-19: auto rescheduled to today, 38 days overdue"
    assert realdue.recover(note) == date(2026, 7, 12)


def test_the_oldest_recovered_date_wins():
    """Each later note was computed from a due date that had already been
    moved, so the earliest implied date wins."""
    notes = (
        "ESCALATED 2026-08-19: auto rescheduled, 38 days overdue\n"
        "ESCALATED 2026-08-25: auto rescheduled, 3 days overdue"
    )
    assert realdue.recover(notes) == date(2026, 7, 12)


def test_a_nonsense_day_count_is_ignored_rather_than_believed():
    assert realdue.recover("2026-08-19: 99999 days overdue") is None


def test_nothing_to_recover_reads_as_nothing():
    assert realdue.recover("no history here") is None
    assert realdue.recover("") is None


# working out which date to trust
def test_the_pinned_line_beats_everything_else():
    task = {
        "description": "📌 Real due: 2026-07-12 | 49 days late\n"
                       "ESCALATED 2026-08-19: 38 days overdue",
        "added_at": "2026-01-01",
        "due": {"date": "2026-08-29"},
    }
    found = realdue.resolve(task, today=TODAY, pb_value="2026-06-01")
    assert found.source == realdue.RealDue.PINNED
    assert found.date == date(2026, 7, 12)


def test_pocketbase_beats_the_notes_and_the_creation_date():
    task = {"description": "2026-08-19: 38 days overdue", "added_at": "2026-01-01"}
    found = realdue.resolve(task, today=TODAY, pb_value="2026-06-01 00:00:00.000Z")
    assert found.source == realdue.RealDue.POCKETBASE
    assert found.date == date(2026, 6, 1)
    assert not found.guessed


def test_a_recovered_date_is_trusted_and_not_marked_as_a_guess():
    task = {"description": "ESCALATED 2026-08-19: 38 days overdue"}
    found = realdue.resolve(task, today=TODAY)
    assert found.source == realdue.RealDue.RECOVERED
    assert not found.guessed


def test_falling_back_to_the_creation_date_is_marked_as_a_guess():
    """The creation date is only an estimate, so it is marked as a guess."""
    task = {"added_at": "2026-05-04T09:00:00Z"}
    found = realdue.resolve(task, today=TODAY)
    assert found.date == date(2026, 5, 4)
    assert found.source == realdue.RealDue.CREATED
    assert found.guessed


def test_a_task_with_nothing_usable_still_gets_a_date():
    """resolve() never returns None: with no usable date it falls back to
    today, marked as a guess, so the task still gets an age."""
    found = realdue.resolve({}, today=TODAY)
    assert found.date == TODAY
    assert found.guessed


def test_the_age_is_measured_from_the_real_date():
    found = realdue.RealDue(date(2026, 7, 12), realdue.RealDue.PINNED)
    assert found.age(TODAY) == 49


def test_a_task_not_due_yet_has_a_negative_age():
    found = realdue.RealDue(date(2026, 9, 5), realdue.RealDue.PINNED)
    assert found.age(TODAY) == -6


# writing the pinned line
def test_the_line_says_the_date_the_age_and_the_pushes():
    found = realdue.RealDue(date(2026, 7, 12), realdue.RealDue.PINNED)
    line = realdue.render(found, today=TODAY, pushes=3)
    assert line == "📌 Real due: 2026-07-12 | 49 days late | 3 pushes"


def test_a_guessed_date_is_labelled_on_the_line():
    found = realdue.RealDue(date(2026, 7, 12), realdue.RealDue.CREATED)
    assert "(guessed)" in realdue.render(found, today=TODAY)


def test_the_line_goes_first_and_the_description_is_kept():
    body = "Open quote QU-7994 (Taponera CAP-100-TR)\nawaiting client response"
    found = realdue.RealDue(date(2026, 7, 12), realdue.RealDue.PINNED)
    out = realdue.apply(body, found, today=TODAY)
    assert out.splitlines()[0].startswith("📌 Real due: 2026-07-12")
    assert "Open quote QU-7994" in out
    assert "awaiting client response" in out


def test_a_recorded_date_cannot_be_overwritten():
    """Write once: a second apply() carrying a different date keeps the
    recorded one."""
    body = realdue.apply(
        "notes", realdue.RealDue(date(2026, 7, 12), realdue.RealDue.PINNED), today=TODAY
    )
    again = realdue.apply(
        body, realdue.RealDue(date(2026, 8, 29), realdue.RealDue.DUE), today=TODAY
    )
    assert "2026-07-12" in again
    assert "2026-08-29" not in again


def test_the_age_on_the_line_is_refreshed_even_though_the_date_is_not():
    found = realdue.RealDue(date(2026, 7, 12), realdue.RealDue.PINNED)
    body = realdue.apply("notes", found, today=date(2026, 8, 1))
    assert "20 days late" in body
    later = realdue.apply(body, found, today=TODAY)
    assert "49 days late" in later
    assert "20 days late" not in later


def test_applying_twice_does_not_stack_two_pinned_lines():
    found = realdue.RealDue(date(2026, 7, 12), realdue.RealDue.PINNED)
    once = realdue.apply("notes", found, today=TODAY)
    twice = realdue.apply(once, found, today=TODAY)
    assert twice.count("📌") == 1


def test_an_attempt_to_change_a_recorded_date_can_be_reported():
    """apply() keeps the stored date quietly; would_change_recorded_date()
    lets a caller detect and report the attempt."""
    body = "📌 Real due: 2026-07-12 | 49 days late"
    assert realdue.would_change_recorded_date(
        body, realdue.RealDue(date(2026, 8, 1), realdue.RealDue.DUE)
    )
    assert not realdue.would_change_recorded_date(
        body, realdue.RealDue(date(2026, 7, 12), realdue.RealDue.DUE)
    )


# the two copies disagreeing
def test_pocketbase_wins_a_disagreement_and_the_difference_is_reported():
    clash = realdue.mismatch("📌 Real due: 2026-07-12 | late", "2026-06-01")
    assert clash["pinned"] == "2026-07-12"
    assert clash["pocketbase"] == "2026-06-01"
    assert clash["winner"] == "2026-06-01"


def test_agreement_is_not_reported_as_a_problem():
    assert realdue.mismatch("📌 Real due: 2026-07-12 | late", "2026-07-12") is None
    assert realdue.mismatch("📌 Real due: 2026-07-12 | late", None) is None
    assert realdue.mismatch("no pin", "2026-07-12") is None


# the mirror, without a server
def test_an_unreachable_pocketbase_reads_as_no_mirror_rather_than_crashing():
    class Broken:
        def find_one(self, *a, **k):
            raise RuntimeError("connection refused")

    assert realdue.read_mirror(Broken(), "6abc") is None
    assert realdue.read_mirror(None, "6abc") is None


def test_a_missing_field_is_reported_as_such_and_not_as_a_failure():
    """write_mirror() returns "no field" when the collection lacks the
    field, which a report can tell apart from "failed"."""
    class NoField:
        def field_names(self, collection):
            return ["todoist_id", "content"]

    found = realdue.RealDue(date(2026, 7, 12), realdue.RealDue.PINNED)
    assert realdue.write_mirror(NoField(), "6abc", found) == "no field"


def test_a_mirror_already_holding_the_right_date_is_left_alone():
    class Ready:
        def __init__(self):
            self.updates = []

        def field_names(self, collection):
            return ["todoist_id", "real_due"]

        def find_one(self, collection, filter_str):
            return {"id": "rec1", "real_due": "2026-07-12 00:00:00.000Z"}

        def update(self, collection, record_id, payload):
            self.updates.append(payload)

    pb = Ready()
    found = realdue.RealDue(date(2026, 7, 12), realdue.RealDue.PINNED)
    assert realdue.write_mirror(pb, "6abc", found) == "already set"
    assert pb.updates == []


# the Project line above the pin
#
# A description can start with a "📁 Project:" line and other header lines,
# with the pin below them. The pin must still be found, rewritten where it
# stands, and never duplicated.
PROJECT = "📁 Project: SS Folders/Amberleaf/QU-8090 Large Quote/PROJECT.md"
LIVE = PROJECT + "\n\n📌 Real due: 2026-07-12 | 49 days late | 3 pushes\n\nchase the client"


def test_a_pin_under_the_project_line_is_still_the_record():
    """A pin below the project line is still read as the record."""
    found = realdue.parse(LIVE)
    assert found.date == date(2026, 7, 12)
    assert found.source == realdue.RealDue.PINNED


def test_a_pin_under_a_whole_header_block_is_still_the_record():
    description = "⏱ Est: 45m\n" + PROJECT + "\n\n📌 Real due: 2026-07-12 | late\n\nnotes"
    assert realdue.parse(description).date == date(2026, 7, 12)


def test_the_body_still_ends_the_search():
    """A quoted email can contain these exact words, so a pin only counts
    in the header, and the first body line ends the header."""
    description = PROJECT + "\n\nchase the client\n\n📌 Real due: 2020-01-01 | ages late"
    assert realdue.parse(description) is None


def test_the_pin_is_rewritten_where_it_stands():
    """The pin stays on its own line under the project line, keeping the
    recorded date, instead of moving to the top."""
    out = realdue.apply(LIVE, realdue.RealDue(date(2026, 8, 29), realdue.RealDue.DUE),
                        today=TODAY)
    lines = out.splitlines()
    assert lines[0] == PROJECT
    assert lines[2].startswith("📌 Real due: 2026-07-12")
    assert "chase the client" in out


def test_a_first_pin_goes_below_the_project_line_not_above_it():
    found = realdue.RealDue(date(2026, 7, 12), realdue.RealDue.PINNED)
    out = realdue.apply(PROJECT + "\n\nchase the client", found, today=TODAY)
    lines = out.splitlines()
    assert lines[0] == PROJECT
    assert lines[2].startswith("📌 Real due: 2026-07-12")
    assert lines[4] == "chase the client"


def test_a_sweep_over_a_project_headed_task_does_not_stack_pins():
    found = realdue.RealDue(date(2026, 7, 12), realdue.RealDue.PINNED)
    once = realdue.apply(LIVE, found, today=TODAY)
    twice = realdue.apply(once, found, today=TODAY)
    assert twice.count("📌") == 1
    assert twice.count(PROJECT) == 1


def test_stripping_the_pin_leaves_the_project_line_where_it_was():
    body = realdue.strip_pin(LIVE)
    assert body.splitlines()[0] == PROJECT
    assert "📌" not in body
    assert "chase the client" in body


def test_two_pins_are_repaired_to_one_and_the_record_wins():
    """A guessed pin above the project line and a recorded pin below it.
    A recorded date beats a guess, and an older date beats a newer one,
    so the guess is removed and one pin remains."""
    damaged = (
        "📌 Real due: 2026-08-20 (guessed) | 10 days late\n\n"
        + PROJECT
        + "\n\n📌 Real due: 2026-07-12 | 49 days late\n\nchase the client"
    )
    assert realdue.parse(damaged).date == date(2026, 7, 12)
    repaired = realdue.apply(damaged, realdue.RealDue(date(2026, 8, 29), realdue.RealDue.DUE),
                             today=TODAY)
    assert repaired.count("📌") == 1
    assert "2026-07-12" in repaired
    assert "2026-08-20" not in repaired
    assert repaired.splitlines()[0] == PROJECT


def test_a_task_with_no_header_at_all_is_unchanged_in_shape():
    """With no header lines, the pin becomes line one."""
    found = realdue.RealDue(date(2026, 7, 12), realdue.RealDue.PINNED)
    out = realdue.apply("chase the client", found, today=TODAY)
    assert out.splitlines()[0].startswith("📌 Real due: 2026-07-12")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
