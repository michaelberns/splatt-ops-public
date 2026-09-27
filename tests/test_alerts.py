"""
Tests for core/alerts.py, the wording of Telegram messages.

Telegram is how the operator follows the system when away from the
computer, so the tests pin down content, not just that a string comes
back: each message names the client and the change, changes nobody needs
to hear about produce no message, sensitive accounts get a distinct
heading, the daily summary carries its counts, and a pass with many
changes collapses into one roll-up message.
"""

from __future__ import annotations

from core import alerts


def task(**kw):
    base = {"id": "9001", "content": "Northstar, belts", "description": "",
            "labels": [], "priority": 1, "due": None}
    base.update(kw)
    return base


# what sort of work it was

def test_a_critical_client_makes_everything_on_it_sensitive():
    assert alerts.kind(task(), critical=True) == "dispute_action"


def test_a_label_decides_the_kind_before_the_wording_does():
    assert alerts.kind(task(labels=["quote"], content="call Raul")) == "quote_issued"


def test_the_wording_decides_when_there_is_no_label():
    assert alerts.kind(task(content="Site visit at Tui Valley")) == "site_visit"
    assert alerts.kind(task(content="Call Kyle back")) == "phone_call"


def test_anything_else_is_just_done():
    assert alerts.kind(task(content="Order the seals")) == "task_completed"


# single messages

def test_a_new_task_says_the_client_and_the_wording():
    text = alerts.new_task("Tui Valley", task(content="Probe cap requote"))

    assert "New task" in text
    assert "Tui Valley" in text
    assert "Probe cap requote" in text


def test_a_task_with_no_client_reads_as_general():
    assert "General" in alerts.new_task("", task())


def test_a_change_nobody_would_read_sends_nothing():
    """A change to project_name alone (bookkeeping, which a first sync
    pass rewrites on every record) produces no message."""
    assert alerts.changed("Tui Valley", "Probe cap", ["project_name"]) == ""


def test_a_change_worth_hearing_about_names_the_field_in_plain_words():
    text = alerts.changed("Tui Valley", "Probe cap", ["due_date", "project_name"])

    assert "due date" in text
    assert "project_name" not in text


def test_a_finished_task_on_a_sensitive_account_is_loud():
    text = alerts.completed("Kowhai Health NZ Ltd", "Send the response",
                            "dispute_action")

    assert "sensitive account" in text
    assert "Kowhai Health NZ Ltd" in text


def test_a_finished_task_says_what_kind_of_work_it_was():
    assert "quote issued" in alerts.completed("Northstar", "Belt quote", "quote_issued")


def test_the_archive_message_says_which_button_and_why():
    text = alerts.dashboard_event("delete", "Probe cap", "duplicate",
                                  at="2026-08-08 09:15:00.000Z")

    assert "Deleted from the dashboard" in text
    assert "duplicate" in text
    assert "2026-08-08 09:15" in text


def test_an_archive_with_no_todoist_task_says_so_rather_than_claiming_it_closed():
    text = alerts.dashboard_event("archive", "Probe cap", closed=False)

    assert "settled in the database only" in text
    assert "Todoist task closed" not in text


# the daily summary

def test_the_daily_summary_carries_the_numbers_that_decide_a_morning():
    text = alerts.daily(active=86, completed_today=4, overdue=11,
                        needing_decision=3, unlinked=64)

    assert "Open tasks: 86" in text
    assert "Finished today: 4" in text
    assert "Overdue: 11" in text
    assert "Waiting on a decision from you: 3" in text
    assert "With no client on them: 64" in text


def test_a_quiet_day_says_so_instead_of_listing_zeroes():
    text = alerts.daily(active=86, completed_today=4, overdue=0)

    assert "Nothing is late." in text
    assert "Waiting on a decision" not in text


# a whole pass

def test_a_forward_pass_speaks_for_creates_updates_and_completions():
    item = {
        "creates": [{"task": task(content="New one"), "payload": {"client": "c1"}}],
        "updates": [{"id": "a1", "task": task(), "payload": {"due_date": "x"}}],
        "completions": [{"id": "a2", "content": "Old one"}],
    }
    records = [{"id": "a1", "content": "Northstar, belts", "client": "c1"},
               {"id": "a2", "content": "Old one", "client": "c1"}]
    out = alerts.for_sync(item, records, {"c1": "Northstar"})

    assert len(out) == 3
    assert all("Northstar" in m for m in out)


def test_a_forward_pass_that_only_fixed_project_name_says_nothing():
    item = {"creates": [], "completions": [],
            "updates": [{"id": "a1", "task": task(), "payload": {"project_name": "x"}}]}

    assert alerts.for_sync(item, [{"id": "a1", "content": "x"}], {}) == []


def test_a_reverse_pass_speaks_for_edits_and_archives():
    item = {"edits": [{"content": "Northstar, belts"}],
            "orphans": [{"content": "No task here"}],
            "archives": [{"intent": "complete", "content": "Probe cap",
                          "reason": "done", "todoist_id": "9001"}]}
    out = alerts.for_reverse(item)

    assert len(out) == 3
    assert "no Todoist task" in out[1]
    assert "Completed from the dashboard" in out[2]


# the flood guard

def test_a_normal_pass_sends_one_message_per_thing():
    messages = ["a", "b", "c"]

    assert alerts.batched(messages) == messages


def test_a_pass_that_touched_everything_sends_one_message_instead_of_eighty_six():
    messages = [alerts.changed("Northstar", "task %d" % i, ["due_date"])
                for i in range(86)]
    out = alerts.batched(messages)

    assert len(out) == 1
    assert "86 changes in one pass" in out[0]
    assert "x86" in out[0]


def test_the_roll_up_says_what_the_changes_were_not_just_how_many():
    messages = ([alerts.new_task("Northstar", task())] * 10
                + [alerts.completed("Northstar", "done")] * 10)
    out = alerts.batched(messages)

    assert "New task" in out[0]
    assert "Completed" in out[0]
    assert "x10" in out[0]


def test_empty_messages_are_dropped_rather_than_sent_as_blanks():
    assert alerts.batched(["a", "", None, "b"]) == ["a", "b"]
