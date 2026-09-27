"""
Tests for what the dashboard sends back to Todoist (core/reverse.py).

They pin down one rule for every field (a cleared field is pushed as
cleared, except the title, which is never cleared), how the archive,
complete and delete buttons are read from `archived_reason`, the comment
written before a close, and which records a pass picks up.
"""

from __future__ import annotations

from core import reverse


def record(**kw):
    base = {"id": "a1", "todoist_id": "9001", "content": "Northstar, belts",
            "description": "", "due_date": "", "priority": 0, "labels": "",
            "source": "sync", "archived": False, "archived_reason": "",
            "archived_at": "", "status": "open"}
    base.update(kw)
    return base


def edited(**kw):
    return record(source="dashboard", **kw)


# what goes out to Todoist on an edit

def test_a_description_cleared_in_the_dashboard_is_cleared_in_todoist():
    payload = reverse.edit_payload(edited(description=""))

    assert payload["description"] == ""


def test_a_title_cleared_in_the_dashboard_is_not_sent():
    """Todoist refuses an empty title, and a task with no title cannot be
    found again by anybody. So this one field is never cleared."""
    payload = reverse.edit_payload(edited(content="   "))

    assert "content" not in payload


def test_a_title_that_was_edited_is_sent():
    payload = reverse.edit_payload(edited(content="Northstar, belts and guides"))

    assert payload["content"] == "Northstar, belts and guides"


def test_a_due_date_taken_off_in_the_dashboard_is_taken_off_in_todoist():
    """A cleared due date is sent as due_string "no date", Todoist's way
    of removing it. If it were skipped, the date would stay in Todoist and
    come back on the next forward pass."""
    payload = reverse.edit_payload(edited(due_date=""))

    assert payload["due_string"] == "no date"
    assert "due_date" not in payload


def test_a_due_date_is_sent_as_a_plain_date():
    payload = reverse.edit_payload(edited(due_date="2026-08-14 09:30:00.000Z"))

    assert payload["due_date"] == "2026-08-14"
    assert "due_string" not in payload


def test_a_due_date_in_the_other_shape_is_still_sent_as_a_plain_date():
    payload = reverse.edit_payload(edited(due_date="2026-08-14T09:30:00Z"))

    assert payload["due_date"] == "2026-08-14"


def test_labels_are_sent_as_a_list_and_not_as_one_string():
    payload = reverse.edit_payload(edited(labels="quote, urgent"))

    assert payload["labels"] == ["quote", "urgent"]


def test_labels_cleared_in_the_dashboard_are_cleared_in_todoist():
    payload = reverse.edit_payload(edited(labels=""))

    assert payload["labels"] == []


def test_a_priority_todoist_would_reject_is_left_out_rather_than_guessed():
    """Zero is what PocketBase holds when no priority is set. Todoist
    would reject an update carrying it, so it is left out."""
    assert "priority" not in reverse.edit_payload(edited(priority=0))
    assert "priority" not in reverse.edit_payload(edited(priority="not a number"))
    assert reverse.edit_payload(edited(priority=3))["priority"] == 3


# which button was pressed

def test_the_delete_prefix_selects_delete_and_is_stripped_from_the_reason():
    assert reverse.intent("DELETE: duplicate of the Tui Valley one") == (
        "delete", "duplicate of the Tui Valley one")


def test_the_complete_prefix_selects_complete_and_is_stripped():
    assert reverse.intent("COMPLETE: parts arrived") == ("complete", "parts arrived")


def test_no_prefix_means_archive():
    assert reverse.intent("client went quiet") == ("archive", "client went quiet")


def test_no_reason_at_all_is_an_archive_with_no_reason():
    assert reverse.intent("") == ("archive", "")
    assert reverse.intent(None) == ("archive", "")


# what gets written on the task before it is closed

def test_the_comment_says_which_button_and_why():
    text = reverse.comment("complete", "parts arrived", "2026-08-08 09:15:00.000Z")

    assert "Completed from the dashboard" in text
    assert "Reason: parts arrived" in text
    assert "At: 2026-08-08 09:15" in text


def test_a_comment_with_no_reason_still_says_what_happened():
    text = reverse.comment("archive", "")

    assert text == "Archived from the dashboard"


# what a pass would pick up

def test_only_records_the_dashboard_touched_are_pushed():
    records = [record(), edited(id="a2")]

    assert [r["id"] for r in reverse.pending_edits(records)] == ["a2"]


def test_a_record_that_is_both_edited_and_archived_is_only_archived():
    """The task is about to be closed, so no edit is pushed; the archive
    path posts the wording in its comment."""
    records = [edited(archived=True, archived_reason="COMPLETE: done")]
    item = reverse.plan(records)

    assert item["edits"] == []
    assert len(item["archives"]) == 1


def test_a_record_already_marked_completed_is_not_closed_again():
    """A record already at status "completed" is not picked up again, so
    one close is not repeated every pass."""
    records = [record(archived=True, status="completed",
                      archived_reason="COMPLETE: done")]

    assert reverse.pending_archives(records) == []


def test_an_edited_record_with_no_todoist_task_is_an_orphan_not_a_send():
    item = reverse.plan([edited(todoist_id="")])

    assert item["edits"] == []
    assert len(item["orphans"]) == 1


def test_an_archived_record_with_no_todoist_task_is_still_in_the_plan():
    """There is nothing to close in Todoist, but the record still has to
    be settled locally, so it stays in the plan."""
    item = reverse.plan([record(archived=True, todoist_id="",
                                archived_reason="DELETE: never real")])

    assert len(item["archives"]) == 1
    assert item["archives"][0]["todoist_id"] == ""
    assert item["archives"][0]["intent"] == "delete"


def test_the_plan_carries_the_payload_so_it_can_be_read_before_it_is_sent():
    item = reverse.plan([edited(content="new wording")])

    assert item["edits"][0]["payload"]["content"] == "new wording"
    assert item["edits"][0]["todoist_id"] == "9001"


def test_nothing_staged_is_an_empty_plan():
    item = reverse.plan([record(), record(id="a2")])

    assert item == {"edits": [], "archives": [], "orphans": []}


# the printed version

def test_the_description_names_the_tasks_rather_than_counting_them():
    item = reverse.plan([edited(content="Northstar, belts"),
                         record(id="a2", content="Tui Valley, probe cap",
                                archived=True, archived_reason="DELETE: duplicate")])
    text = reverse.describe(item)

    assert "Northstar, belts" in text
    assert "Tui Valley, probe cap" in text
    assert "duplicate" in text
    assert "delete 1" in text
