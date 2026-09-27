"""
Tests for tools/fill_real_due_mirror.py.

The mirror (assignments.real_due) is the copy of a task's real due date
that a person cannot overwrite by accident. The tests concentrate on what
the tool refuses to do: it never writes over a date already stored, and it
never touches anything but that one field.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import fill_real_due_mirror as mirror

PROJECT = "📁 Project: SS Folders/Amberleaf/QU-8090 Large Quote/PROJECT.md"
PIN = "📌 Real due: 2026-07-12 | 49 days late | 3 pushes"
LIVE = PROJECT + "\n\n" + PIN + "\n\nchase the client"


def record(record_id="rec1", description=LIVE, real_due=None,
           content="chase Amberleaf"):
    return {
        "id": record_id,
        "todoist_id": "6aaaaaaaaaaaaaaa",
        "content": content,
        "description": description,
        "real_due": real_due,
    }


class FakePB:
    """A PocketBase that remembers what it was asked to write."""

    def __init__(self):
        self.updates = []

    def update(self, collection, record_id, payload):
        self.updates.append((collection, record_id, payload))


# reading the board
def test_a_pin_under_the_project_line_is_found():
    """The pin sits under the project line, not on the first line, and
    must still be found."""
    rows = mirror.plan([record()])
    assert rows[0].verdict == "fill"
    assert rows[0].pinned.date == date(2026, 7, 12)


def test_an_empty_mirror_is_a_fill():
    for value in (None, "", "   "):
        assert mirror.plan([record(real_due=value)])[0].verdict == "fill"


def test_a_mirror_already_holding_the_date_is_left_alone():
    rows = mirror.plan([record(real_due="2026-07-12 00:00:00.000Z")])
    assert rows[0].verdict == "agrees"


def test_a_disagreement_is_reported_and_not_a_fill():
    """The stored date wins when the two are read. Changing it here
    silently would hide the mismatch core/realdue.py exists to detect."""
    rows = mirror.plan([record(real_due="2026-06-01 00:00:00.000Z")])
    assert rows[0].verdict == "disagrees"
    assert "PocketBase wins" in rows[0].detail


def test_a_task_with_no_pin_is_not_a_problem_on_its_own():
    rows = mirror.plan([record(description="just some notes")])
    assert rows[0].verdict == "no pin"


def test_a_pin_down_in_the_body_is_not_the_record():
    rows = mirror.plan([record(
        description=PROJECT + "\n\nthey wrote:\n📌 Real due: 2020-01-01 | late"
    )])
    assert rows[0].verdict == "no pin"


# writing
def test_only_the_one_field_is_written_and_only_on_that_record():
    pb = FakePB()
    row = mirror.plan([record(record_id="rec42")])[0]
    assert mirror.apply_row(pb, row) == "written"
    assert pb.updates == [
        ("assignments", "rec42", {"real_due": "2026-07-12"})
    ]


def test_a_failed_write_says_so_rather_than_passing_quietly():
    class Broken:
        def update(self, *a, **k):
            raise RuntimeError("connection refused")

    row = mirror.plan([record()])[0]
    assert mirror.apply_row(Broken(), row).startswith("FAILED")


def test_a_dry_run_report_names_every_verdict():
    rows = mirror.plan([
        record(record_id="a"),
        record(record_id="b", real_due="2026-07-12"),
        record(record_id="c", real_due="2026-06-01"),
        record(record_id="d", description="no pin here"),
    ])
    text = mirror.report(rows, date(2026, 9, 5), write=False)
    assert "DRY RUN" in text
    assert "fill       1" in text
    assert "agrees     1" in text
    assert "disagrees  1" in text
    assert "no pin     1" in text


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
