"""
Tests for the sync planner (core/sync.py).

Groups: the "Todoist owns these fields" rule and fill-only links;
duplicate handling; completion and the 50% floor; new records; field
conversion (due date, deadline, priority, labels); the plan as a whole;
and project links.
"""

from __future__ import annotations

import pytest

from core import jobmatch, sync
from core.jobmatch import JobIndex
from core.matching import Index

CLIENTS = [
    {"id": "c_northstar", "name": "Northstar Beverage & Food New Zealand Limited",
     "aliases": "Northstar"},
    {"id": "c_internal", "name": "Splatt Engineering (internal)",
     "aliases": "Splatt Engineering, Splatt"},
]
SUPPLIERS = [{"id": "s_courier", "name": "QXP Express NZ", "aliases": "QXP"}]
INDEX = Index(CLIENTS, SUPPLIERS)

SECTIONS = {"sec_1": "This Week", "sec_2": "Waiting"}


def task(tid="t1", content="Northstar / Raul \u2014 Send 4 quoted belts", **kw):
    base = {"id": tid, "content": content, "description": "", "priority": 1,
            "labels": [], "due": None, "section_id": "sec_1"}
    base.update(kw)
    return base


def record(rid="a1", tid="t1", **kw):
    base = {"id": rid, "todoist_id": tid,
            "content": "Northstar / Raul \u2014 Send 4 quoted belts",
            "description": "", "priority": 1, "labels": "", "due_date": "",
            "section_name": "This Week", "project_name": "Splatt Ops",
            "status": "open", "source": "sync", "client": "", "supplier": "",
            "created": "2026-01-01 00:00:00.000Z"}
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# Todoist owns only its own fields, and links are fill-only
# ---------------------------------------------------------------------------

def test_a_client_link_is_never_blanked_by_a_pass_over_todoist():
    """Todoist holds no client, supplier or project link, so an update
    payload never contains those fields and a link set by hand survives
    every pass."""
    diff = sync.changes(record(client="c_northstar"),
                        sync.derive(task(), SECTIONS, "Splatt Ops"))

    assert "client" not in diff
    assert "supplier" not in diff
    assert "job" not in diff


def test_a_client_already_set_is_left_alone_even_when_the_title_says_otherwise():
    """A link someone set is kept, even when the title names another
    client."""
    out = sync.link("Northstar / Raul \u2014 belts", INDEX, "c_internal",
                    record(client="c_someone_else"))

    assert out == {}


def test_a_name_it_cannot_place_changes_nothing():
    out = sync.link("Kiwipak / Johnny Green \u2014 jar drawings", INDEX,
                    "c_internal", record())

    assert out == {}


def test_a_blank_client_is_filled_when_the_name_is_certain():
    out = sync.link("Northstar / Raul \u2014 belts", INDEX, "c_internal", record())

    assert out == {"client": "c_northstar"}


def test_a_supplier_task_gets_the_supplier_and_splatt_as_the_client():
    out = sync.link("QXP / Partnerco \u2014 Pay import duty", INDEX, "c_internal",
                    record())

    assert out == {"supplier": "s_courier", "client": "c_internal"}


def test_a_supplier_already_set_is_not_written_over():
    out = sync.link("QXP / Partnerco \u2014 Pay import duty", INDEX, "c_internal",
                    record(supplier="s_coastline"))

    assert out == {"client": "c_internal"}


def test_an_unchanged_task_produces_an_empty_payload():
    """A payload with nothing in it is never sent, so an unchanged task
    costs no write and leaves no trace in the ledger."""
    diff = sync.changes(record(), sync.derive(task(), SECTIONS, "Splatt Ops"))

    assert diff == {}


def test_only_the_field_that_moved_is_in_the_payload():
    diff = sync.changes(record(),
                        sync.derive(task(content="Northstar / Raul \u2014 Send 6 belts"),
                                    SECTIONS, "Splatt Ops"))

    assert diff == {"content": "Northstar / Raul \u2014 Send 6 belts"}


def test_an_unknown_section_id_does_not_erase_the_section_name():
    """An unknown section id means the section map is out of date, not
    that the task left its section, so the stored name is kept."""
    diff = sync.changes(record(),
                        sync.derive(task(section_id="sec_new"), SECTIONS,
                                    "Splatt Ops"))

    assert "section_name" not in diff


def test_a_real_section_move_does_come_through():
    diff = sync.changes(record(),
                        sync.derive(task(section_id="sec_2"), SECTIONS,
                                    "Splatt Ops"))

    assert diff == {"section_name": "Waiting"}


def test_a_description_cleared_in_todoist_is_cleared_here():
    """Unlike a section name, an empty description is a real edit: the
    operator cleared it in Todoist, so the pass clears it here too."""
    diff = sync.changes(record(description="old notes"),
                        sync.derive(task(), SECTIONS, "Splatt Ops"))

    assert diff == {"description": ""}


def test_an_edit_waiting_to_go_out_to_todoist_is_not_overwritten():
    item = sync.plan([task(content="Northstar / Raul \u2014 the old wording")],
                     [record(source="dashboard", content="what the operator typed")],
                     SECTIONS, "Splatt Ops", INDEX, "c_internal")

    assert item["updates"] == []
    assert item["links"] == []


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------

def test_the_oldest_record_for_a_todoist_id_is_the_one_that_is_kept():
    """The oldest record is kept because it carries anything set by hand
    and is the one other records refer to."""
    old = record("a_old", created="2026-01-01 00:00:00.000Z")
    new = record("a_new", created="2026-06-01 00:00:00.000Z")
    best, extra = sync.canonical([new, old])

    assert best["t1"]["id"] == "a_old"
    assert [r["id"] for r in extra] == ["a_new"]


def test_the_answer_does_not_depend_on_the_order_records_arrived_in():
    old = record("a_old", created="2026-01-01 00:00:00.000Z")
    new = record("a_new", created="2026-06-01 00:00:00.000Z")

    assert sync.canonical([old, new])[0]["t1"]["id"] == \
        sync.canonical([new, old])[0]["t1"]["id"]


def test_a_duplicate_is_reported_and_not_deleted():
    """The plan has no deletes. A loop that deletes is one bad read away
    from clearing the board, so duplicates are only reported."""
    item = sync.plan([task()],
                     [record("a_old", created="2026-01-01 00:00:00.000Z"),
                      record("a_new", created="2026-06-01 00:00:00.000Z")],
                     SECTIONS, "Splatt Ops", INDEX, "c_internal")

    assert [d["id"] for d in item["duplicates"]] == ["a_new"]
    assert "deletes" not in item


def test_a_record_with_no_todoist_id_is_skipped_rather_than_grouped():
    best, extra = sync.canonical([record("a1", tid=""), record("a2", tid=None)])

    assert best == {} and extra == []


# ---------------------------------------------------------------------------
# Completion, and the floor under it
# ---------------------------------------------------------------------------

def test_a_task_gone_from_todoist_marks_its_record_completed():
    item = sync.plan([task("t1")],
                     [record("a1", "t1"), record("a2", "t2")],
                     SECTIONS, "Splatt Ops", INDEX, "c_internal")

    assert [c["id"] for c in item["completions"]] == ["a2"]


def test_an_already_completed_record_is_not_touched_again():
    item = sync.plan([], [record("a1", "t1", status="completed")],
                     SECTIONS, "Splatt Ops", INDEX, "c_internal")

    assert item["completions"] == []


def test_a_read_that_lost_most_of_the_board_refuses_to_complete_anything():
    """The 50% floor: one task back against twenty open records raises
    Refused instead of completing nineteen of them."""
    records = [record("a%d" % i, "t%d" % i) for i in range(20)]

    with pytest.raises(sync.Refused) as exc:
        sync.plan([task("t0")], records, SECTIONS, "Splatt Ops",
                  INDEX, "c_internal")

    assert "will not mark anything completed" in str(exc.value)


def test_a_real_run_of_completions_is_still_allowed_through():
    records = [record("a%d" % i, "t%d" % i) for i in range(20)]
    tasks = [task("t%d" % i) for i in range(15)]
    item = sync.plan(tasks, records, SECTIONS, "Splatt Ops",
                     INDEX, "c_internal")

    assert len(item["completions"]) == 5


def test_an_empty_board_on_both_sides_is_not_an_error():
    item = sync.plan([], [], SECTIONS, "Splatt Ops", INDEX, "c_internal")

    assert item["completions"] == [] and item["creates"] == []


# ---------------------------------------------------------------------------
# New records
# ---------------------------------------------------------------------------

def test_a_new_task_carries_its_todoist_id_and_its_client():
    payload = sync.new_record(task(), SECTIONS, "Splatt Ops", INDEX,
                              "c_internal")

    assert payload["todoist_id"] == "t1"
    assert payload["client"] == "c_northstar"
    assert payload["status"] == "open"
    assert payload["source"] == "sync"


def test_a_new_task_it_cannot_place_still_gets_created():
    """A task whose client is unknown is still created, just without a
    client link."""
    payload = sync.new_record(task(content="Kiwipak / Johnny \u2014 drawings"),
                              SECTIONS, "Splatt Ops", INDEX, "c_internal")

    assert payload["content"].startswith("Kiwipak")
    assert "client" not in payload


def test_the_project_name_field_holds_a_name_and_not_an_id():
    """project_name comes from the caller, never from the task's
    project_id."""
    payload = sync.new_record(task(project_id="6Xproject0000001"), SECTIONS,
                              "Splatt Ops")

    assert payload["project_name"] == "Splatt Ops"


# ---------------------------------------------------------------------------
# Field conversion
# ---------------------------------------------------------------------------

def test_an_all_day_due_date_becomes_a_datetime():
    assert sync.due_date({"due": {"date": "2026-08-14"}}) == "2026-08-14 00:00:00.000Z"


def test_a_timed_due_date_keeps_its_time():
    assert sync.due_date({"due": {"date": "2026-08-14T09:30:00"}}) == \
        "2026-08-14 09:30:00.000Z"


def test_a_zone_offset_is_converted_to_utc_and_not_just_cut_off():
    """09:30 in Auckland (+12:00) is 21:30 UTC the day before. Cutting the
    offset off instead would file the task twelve hours out."""
    assert sync.due_date({"due": {"date": "2026-08-14T09:30:00+12:00"}}) == \
        "2026-08-13 21:30:00.000Z"


def test_a_time_already_in_utc_is_left_where_it_is():
    assert sync.due_date({"due": {"date": "2026-08-14T21:30:00Z"}}) == \
        "2026-08-14 21:30:00.000Z"


def test_no_due_date_is_an_empty_string_not_a_crash():
    assert sync.due_date({}) == ""
    assert sync.due_date({"due": None}) == ""


def test_a_due_date_todoist_could_not_have_sent_is_refused_not_guessed():
    assert sync.due_date({"due": {"date": "next Tuesdayish"}}) == ""


def test_a_due_date_is_stable_when_it_has_not_changed():
    """An unstable conversion would make every pass see a change and
    write every task again."""
    once = sync.due_date({"due": {"date": "2026-08-14"}})

    assert sync.changes({"due_date": once},
                        {"due_date": sync.due_date({"due": {"date": "2026-08-14"}})}) == {}


def test_a_priority_todoist_never_sends_falls_back_rather_than_crashing():
    assert sync.priority({"priority": None}) == 1
    assert sync.priority({"priority": "not a number"}) == 1
    assert sync.priority({"priority": 9}) == 1
    assert sync.priority({"priority": 4}) == 4


def test_labels_are_stored_the_way_the_field_holds_them():
    assert sync.labels({"labels": ["quote", "urgent"]}) == "quote,urgent"
    assert sync.labels({"labels": []}) == ""


# ---------------------------------------------------------------------------
# The deadline, which is not the due date
#
# Todoist holds two dates on a task. The due date is when the operator plans
# to work on it, and the overdue engine may move it. The deadline is when the
# work stops being worth doing, and nothing may move it. Both are mirrored.
# ---------------------------------------------------------------------------

def test_a_deadline_is_read_off_the_task():
    assert sync.deadline({"deadline": {"date": "2026-09-03"}}) == \
        "2026-09-03 00:00:00.000Z"


def test_a_deadline_is_always_midnight_because_todoist_has_no_clock_on_it():
    """Todoist's deadline is a plain day with no time part, so it is
    stored at midnight and no timezone conversion applies."""
    assert sync.deadline({"deadline": {"date": "2026-09-03"}}).endswith(
        "00:00:00.000Z")


def test_no_deadline_is_an_empty_string_not_a_crash():
    assert sync.deadline({}) == ""
    assert sync.deadline({"deadline": None}) == ""
    assert sync.deadline(None) == ""


def test_a_deadline_todoist_could_not_have_sent_is_refused_not_guessed():
    assert sync.deadline({"deadline": {"date": "sometime in spring"}}) == ""
    assert sync.deadline({"deadline": {"date": ""}}) == ""


def test_a_deadline_that_is_not_a_dict_does_not_crash_the_pass():
    """A malformed deadline gives "" instead of raising, so one bad task
    does not stop the rest of the pass."""
    assert sync.deadline({"deadline": "2026-09-03"}) == ""


def test_a_deadline_is_stable_when_it_has_not_changed():
    once = sync.deadline({"deadline": {"date": "2026-09-03"}})

    assert sync.changes(
        {"deadline": once},
        {"deadline": sync.deadline({"deadline": {"date": "2026-09-03"}})}) == {}


def test_the_derived_record_carries_the_deadline():
    got = sync.derive(task(deadline={"date": "2026-09-03"}), SECTIONS,
                      "Splatt Ops")

    assert got["deadline"] == "2026-09-03 00:00:00.000Z"


def test_todoist_owns_the_deadline_so_a_change_there_wins():
    """The deadline is in TODOIST_OWNS, so clearing it in Todoist clears
    it in PocketBase."""
    assert "deadline" in sync.TODOIST_OWNS

    diff = sync.changes(record(deadline="2026-09-03 00:00:00.000Z"),
                        sync.derive(task(), SECTIONS, "Splatt Ops"))

    assert diff.get("deadline") == ""


# ---------------------------------------------------------------------------
# The plan as a whole
# ---------------------------------------------------------------------------

def test_the_plan_can_be_read_before_anything_is_written():
    item = sync.plan([task("t1"), task("t2", content="QXP / Partnerco \u2014 duty")],
                     [record("a1", "t1", content="stale wording")],
                     SECTIONS, "Splatt Ops", INDEX, "c_internal")
    text = sync.describe(item)

    assert "New tasks to add: 1" in text
    assert "Tasks whose details changed: 1" in text
    assert "QXP" in text


def test_nothing_in_the_plan_writes_a_client_onto_a_completed_record():
    item = sync.plan([task()], [record(status="completed")], SECTIONS,
                     "Splatt Ops", INDEX, "c_internal")

    assert item["links"] == []


# ---------------------------------------------------------------------------
# Project links
#
# Todoist holds no project, so `job` is filled by the link step (link_job)
# on every pass. These tests check that it happens and that it follows the
# same fill-only rules as the client link.
# ---------------------------------------------------------------------------

JOBS = JobIndex(
    [{"id": "j_belts", "title": "Northstar / conveyor belt replacement programme",
      "client": "c_northstar", "status": "active", "archived": False}],
    CLIENTS,
)


def test_a_pass_now_files_a_task_under_a_project():
    item = sync.plan([task(content="Northstar / Raul \u2014 conveyor belt replacement")],
                     [record(client="c_northstar")],
                     SECTIONS, "Splatt Ops", INDEX, "c_internal", JOBS)

    assert [e["payload"] for e in item["job_links"]] == [{"job": "j_belts"}]
    assert item["unplaced_jobs"] == []


def test_a_project_already_set_is_left_alone():
    """Same rule as the client link: a project someone set is kept."""
    out = sync.link_job("Northstar / conveyor belt replacement", "c_northstar",
                        JOBS, record(job="j_something_else"))

    assert out == ({}, None)


def test_a_task_that_names_no_project_is_reported_rather_than_filed():
    item = sync.plan([task(content="Northstar / Raul \u2014 call about the invoice")],
                     [record(client="c_northstar")],
                     SECTIONS, "Splatt Ops", INDEX, "c_internal", JOBS)

    assert item["job_links"] == []
    assert item["unplaced_jobs"][0]["reason"] == jobmatch.NO_EVIDENCE


def test_a_client_gained_this_pass_can_gain_its_project_in_the_same_pass():
    """The client link runs first and its result is used to choose the
    project, so a record with no links comes out of one pass with both."""
    item = sync.plan([task(content="Northstar / Raul \u2014 conveyor belt replacement")],
                     [record(client="",
                             content="Northstar / Raul \u2014 conveyor belt replacement")],
                     SECTIONS, "Splatt Ops", INDEX, "c_internal", JOBS)

    assert item["links"][0]["payload"] == {"client": "c_northstar"}
    assert item["job_links"][0]["payload"] == {"job": "j_belts"}


def test_a_new_record_arrives_with_its_project_already_on_it():
    item = sync.plan([task(content="Northstar / Raul \u2014 conveyor belt replacement")],
                     [], SECTIONS, "Splatt Ops", INDEX, "c_internal", JOBS)

    assert item["creates"][0]["payload"]["job"] == "j_belts"


def test_nothing_in_the_plan_files_a_completed_record_under_a_project():
    item = sync.plan([task(content="Northstar / Raul \u2014 conveyor belt replacement")],
                     [record(status="completed", client="c_northstar")],
                     SECTIONS, "Splatt Ops", INDEX, "c_internal", JOBS)

    assert item["job_links"] == []


def test_a_pass_with_no_project_index_behaves_exactly_as_before():
    """With no job index, project linking is skipped entirely."""
    item = sync.plan([task()], [record()], SECTIONS, "Splatt Ops",
                     INDEX, "c_internal")

    assert item["job_links"] == []
    assert item["unplaced_jobs"] == []


def test_a_todoist_pass_still_never_sends_the_job_field_itself():
    """`job` is written only by the link step. If it were in
    TODOIST_OWNS, every pass would blank it."""
    assert "job" not in sync.TODOIST_OWNS


def test_the_plan_says_what_it_filed_and_what_it_could_not():
    item = sync.plan([task("t1", content="Northstar / Raul \u2014 conveyor belt replacement"),
                      task("t2", content="Northstar / Raul \u2014 call about the invoice")],
                     [record("a1", "t1", client="c_northstar"),
                      record("a2", "t2", client="c_northstar",
                             content="Northstar / Raul \u2014 call about the invoice")],
                     SECTIONS, "Splatt Ops", INDEX, "c_internal", JOBS)
    text = sync.describe(item)

    assert "Tasks that would gain a project link" in text
    assert "conveyor belt replacement programme" in text
    assert "Tasks left with no project link: 1" in text
    assert "The validator lists these, they are not lost." in text
