"""
Tests for the trigger engine (core/triggers.py, applied by daemon/triggers.py).

Most tests pin down a case where a job must NOT move: backwards, onto a
sensitive account, to "invoiced" without an invoice record, twice for the
same completion, or from a task with no project link. Others cover the
trigger and auto lines in the description header, R0 to R3 one by one,
applying a plan through fake Todoist and PocketBase clients, and that the
shipped settings match the code's defaults.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from core import triggers
from core.ledger import RecordingClient, WriteLedger
from daemon import triggers as daemon_triggers
from tests.conftest_splatt import FakeLedger, FakeSettings
from tests.fakes import SECTIONS, FakePB, FakeTodoist

#: A Thursday, so the planner has a working day to book a chase into and
#: nothing here reads strangely because of a weekend.
TODAY = date(2026, 9, 10)

PIN = "📌 Real due: 2026-09-01 | 9 days late"
PROJECT = "📁 Project: SS Folders/Kauri Springs/QU-8090 Filler/PROJECT.md"


def stamp(day):
    return "%s 00:00:00.000Z" % day.isoformat()


def job(job_id="j1", status="quoting", changed=None, client="c1",
        title="Filler upgrade", archived=False):
    return {
        "id": job_id,
        "title": title,
        "status": status,
        "client": client,
        "archived": archived,
        triggers.JOB_CHANGED_AT: stamp(changed) if changed else "",
        triggers.JOB_CHANGED_BY: "manual" if changed else "",
    }


def assignment(record_id="a1", todoist_id="6aaaaaaaaaaaaaaa",
               content="Send the quote", description=None, job_id="j1",
               client="c1", status="open", on_complete="", auto_kind="",
               archived=False):
    if description is None:
        description = "⏱ Est: 15m\n%s\n\n%s" % (PROJECT, PIN)
    return {
        "id": record_id,
        "todoist_id": todoist_id,
        "content": content,
        "description": description,
        "job": job_id,
        "client": client,
        "status": status,
        "archived": archived,
        triggers.PB_STATUS_FIELD: on_complete,
        triggers.PB_AUTO_FIELD: auto_kind,
    }


def task(todoist_id="6aaaaaaaaaaaaaaa", content="Send the quote",
         description=None):
    return {
        "id": todoist_id,
        "content": content,
        "description": description if description is not None
        else "⏱ Est: 15m\n%s\n\n%s" % (PROJECT, PIN),
    }


def board(assignments=(), jobs=(), clients=None, tasks=(), quotes=(),
          invoices=(), contacts=(), task_groups=(), keys=()):
    return {
        "assignments": list(assignments),
        "jobs": list(jobs),
        "clients": list(clients if clients is not None
                        else [{"id": "c1", "name": "Kauri Springs"}]),
        "contacts": list(contacts),
        "quotes": list(quotes),
        "invoices": list(invoices),
        "task_groups": list(task_groups),
        "tasks": list(tasks),
        "interaction_keys": set(keys),
    }


def rules(**overrides):
    values = {
        "triggers.quoted_chase_after_days": 7,
        "triggers.forward_order": list(triggers.FORWARD_ORDER),
        "triggers.title_patterns": triggers.DEFAULT_TITLE_PATTERNS,
    }
    values.update(overrides)
    return triggers.Rules(FakeSettings(values))


def run(seen, **overrides):
    return triggers.plan(seen, rules(**overrides), today=TODAY)


# ==================================================================
# The lines on a task
# ==================================================================

def test_the_line_is_read_back_out_of_a_description():
    text = "⏱ Est: 15m\n%s\n%s\n\n%s" % (
        PROJECT, triggers.render_on_complete("quoted"), PIN)
    assert triggers.parse_on_complete(text) == "quoted"


def test_a_status_nobody_agreed_on_is_not_a_trigger():
    """Only the four TARGETS can be named in a trigger line. A line naming
    any other status does not parse as a trigger at all."""
    assert triggers.parse_on_complete("🔁 On complete: job to on_hold") == ""
    assert triggers.parse_on_complete("🔁 On complete: job to paid") == ""


def test_a_line_further_down_the_body_is_not_an_instruction():
    """A quoted email can say anything. Only the header is the record."""
    text = "%s\n\nThey wrote:\n%s" % (PIN, triggers.render_on_complete("won"))
    assert triggers.parse_on_complete(text) == ""


def test_two_different_lines_is_no_answer_rather_than_a_guess():
    text = "%s\n%s\n%s" % (PIN, triggers.render_on_complete("quoted"),
                          triggers.render_on_complete("won"))
    assert triggers.parse_on_complete(text) == ""
    assert triggers.on_complete_count(text) == 2


def test_the_line_goes_below_the_pin_and_never_above_it():
    """New header lines go at the bottom of the header, below the pin,
    so the pin never moves."""
    before = "⏱ Est: 15m\n%s\n\n%s\n\nSome body text." % (PROJECT, PIN)
    after = triggers.place(before, triggers.render_on_complete("quoted"))
    lines = [line for line in after.splitlines() if line.strip()]
    assert lines.index(PIN) < lines.index(triggers.render_on_complete("quoted"))
    assert "Some body text." in after


def test_writing_the_same_line_twice_does_not_stack_it_up():
    line = triggers.render_on_complete("quoted")
    once = triggers.place("%s\n%s" % (PROJECT, PIN), line)
    twice = triggers.place(once, line)
    assert once == twice
    assert triggers.on_complete_count(twice) == 1


def test_the_line_and_the_field_disagreeing_is_reported_with_pocketbase_winning():
    text = "%s\n%s" % (PIN, triggers.render_on_complete("quoted"))
    clash = triggers.mismatch(text, "won")
    assert clash == {"line": "quoted", "pocketbase": "won", "winner": "won"}
    assert triggers.mismatch(text, "quoted") is None


def test_an_auto_line_names_its_kind_and_its_job():
    text = "%s\n%s" % (PROJECT, triggers.render_auto(triggers.QUOTED_CHASE, "j7"))
    assert triggers.parse_auto(text) == (triggers.QUOTED_CHASE, "j7")


# ==================================================================
# R1, a finished task moves its job
# ==================================================================

def test_completing_a_send_quote_task_moves_the_job_to_quoted():
    item = run(board(
        assignments=[assignment(status="completed", on_complete="quoted")],
        jobs=[job(status="quoting")],
    ))
    assert len(item["r1"]) == 1
    move = item["r1"][0]
    assert (move["from"], move["to"]) == ("quoting", "quoted")
    assert move["changed_by"] == "task:6aaaaaaaaaaaaaaa"
    assert move["subject"] == "Project quoted: Filler upgrade"


@pytest.mark.parametrize("current,target", [
    ("quoted", "won"),
    ("won", "invoicing"),
    ("invoicing", "invoiced"),
])
def test_the_other_three_moves_work_the_same_way(current, target):
    item = run(board(
        assignments=[assignment(status="completed", on_complete=target)],
        jobs=[job(status=current)],
        invoices=[{"id": "i1", "job": "j1", "invoice_number": "SE INV-41374"}],
    ))
    assert [(a["from"], a["to"]) for a in item["r1"]] == [(current, target)]


def test_a_finished_task_with_no_project_link_does_nothing_and_is_reported():
    """No project link means nothing moves, and the case is listed under
    needs_decision instead of a link being guessed."""
    item = run(board(
        assignments=[assignment(job_id="", status="completed", on_complete="quoted")],
        jobs=[job(status="quoting")],
    ))
    assert item["r1"] == []
    assert [row["rule"] for row in item["needs_decision"]] == ["r1"]
    assert "not linked to a project" in item["needs_decision"][0]["why"]


def test_a_backwards_move_is_refused():
    item = run(board(
        assignments=[assignment(status="completed", on_complete="quoted")],
        jobs=[job(status="invoicing")],
    ))
    assert item["r1"] == []
    assert any("already invoicing" in row["why"] for row in item["skipped"])


def test_a_job_already_at_the_target_is_left_alone():
    item = run(board(
        assignments=[assignment(status="completed", on_complete="quoted")],
        jobs=[job(status="quoted", changed=TODAY)],
    ))
    assert item["r1"] == []
    assert item["needs_decision"] == []


def test_a_job_that_is_already_paid_is_past_everything():
    item = run(board(
        assignments=[assignment(status="completed", on_complete="invoiced")],
        jobs=[job(status="paid")],
    ))
    assert item["r1"] == []
    assert any("past invoiced" in row["why"] for row in item["skipped"])


def test_a_status_off_the_forward_line_is_a_decision_not_a_move():
    """on_hold is not on the forward line, so a completed task does not
    move the job out of it; the case goes to needs_decision."""
    item = run(board(
        assignments=[assignment(status="completed", on_complete="won")],
        jobs=[job(status="on_hold")],
    ))
    assert item["r1"] == []
    assert "not on the forward line" in item["needs_decision"][0]["why"]


def test_a_sensitive_account_is_reported_and_never_acted_on():
    item = run(board(
        assignments=[assignment(status="completed", on_complete="quoted")],
        jobs=[job(status="quoting", client="c_kh")],
        clients=[{"id": "c_kh", "name": "Kowhai Health NZ Ltd", "critical": True}],
    ))
    assert item["r1"] == []
    assert "sensitive account" in item["needs_decision"][0]["why"]


def test_moving_to_quoted_does_not_need_a_quote_record():
    """Unlike invoiced, the move to quoted needs no quote record. A quoted
    job with no quote record is reported by the validator and shown on
    the dashboard, but it does not block the move."""
    item = run(board(
        assignments=[assignment(status="completed", on_complete="quoted")],
        jobs=[job(status="quoting")],
        quotes=[],
    ))
    assert len(item["r1"]) == 1


def test_moving_to_invoiced_without_an_invoice_record_is_refused():
    """The validator's gate_invoiced would block this move, so the engine
    refuses it up front and lists it under needs_decision."""
    item = run(board(
        assignments=[assignment(status="completed", on_complete="invoiced")],
        jobs=[job(status="invoicing")],
        invoices=[],
    ))
    assert item["r1"] == []
    assert "needs an invoice record" in item["needs_decision"][0]["why"]


def test_an_invoice_record_with_no_number_on_it_does_not_count():
    item = run(board(
        assignments=[assignment(status="completed", on_complete="invoiced")],
        jobs=[job(status="invoicing")],
        invoices=[{"id": "i1", "job": "j1", "invoice_number": ""}],
    ))
    assert item["r1"] == []


def test_an_open_task_with_a_trigger_does_nothing_until_it_is_finished():
    item = run(board(
        assignments=[assignment(status="open", on_complete="quoted")],
        jobs=[job(status="quoting")],
    ))
    assert item["r1"] == []


def test_a_move_that_already_happened_is_not_made_twice():
    """The interaction written for a move is its receipt. When the receipt
    key is already present, the move is skipped, even if the job has since
    been moved back by hand."""
    key = triggers.source_id("r1", "a1", "quoted")
    item = run(board(
        assignments=[assignment(status="completed", on_complete="quoted")],
        jobs=[job(status="quoting")],
        keys=[key],
    ))
    assert item["r1"] == []
    assert any("already fired" in row["why"] for row in item["skipped"])


# ==================================================================
# R0, giving a task the trigger it should have had
# ==================================================================

def test_a_line_already_on_the_task_is_mirrored_into_the_record():
    """The normal path: the agent wrote the line when it created the task,
    and R0 mirrors it into the PocketBase field without rewriting the
    line."""
    description = "⏱ Est: 15m\n%s\n%s\n\n%s" % (
        PROJECT, triggers.render_on_complete("quoted"), PIN)
    item = run(board(
        assignments=[assignment(description=description)],
        jobs=[job(status="quoting")],
        tasks=[task(description=description)],
    ))
    assert len(item["r0"]) == 1
    assert item["r0"][0]["status"] == "quoted"
    assert item["r0"][0]["write_line"] is False


def test_a_strict_title_match_adds_the_line_and_the_field():
    item = run(board(
        assignments=[assignment(content="Kauri / Shane — Send the quote")],
        jobs=[job(status="quoting")],
        tasks=[task(content="Kauri / Shane — Send the quote")],
    ))
    assert len(item["r0"]) == 1
    assert item["r0"][0]["write_line"] is True
    assert triggers.render_on_complete("quoted") in item["r0"][0]["description_after"]


def test_the_title_only_counts_when_the_job_is_in_the_right_status():
    """A title pattern only applies when the job is in the matching
    status. "Send the quote" on a job that is already won is about
    something else."""
    item = run(board(
        assignments=[assignment(content="Send the quote")],
        jobs=[job(status="won")],
        tasks=[task(content="Send the quote")],
    ))
    assert item["r0"] == []


def test_a_task_with_no_project_link_never_gets_a_trigger():
    """A task with no project link gets no trigger, and no link is
    invented for it."""
    item = run(board(
        assignments=[assignment(job_id="", content="Send the quote")],
        jobs=[job(status="quoting")],
        tasks=[task()],
    ))
    assert item["r0"] == []
    assert item["needs_decision"] == []


def test_a_task_that_already_has_the_field_is_left_alone():
    item = run(board(
        assignments=[assignment(on_complete="quoted")],
        jobs=[job(status="quoting")],
        tasks=[task()],
    ))
    assert item["r0"] == []


def test_a_completed_task_is_not_given_a_trigger_after_the_fact():
    """R0 only considers open tasks. Adding a trigger to a completed task
    would move its job on the next pass off a title pattern nobody had
    reviewed."""
    item = run(board(
        assignments=[assignment(status="completed", content="Send the quote")],
        jobs=[job(status="quoting")],
    ))
    assert item["r0"] == []


def test_a_trigger_added_this_pass_does_not_also_fire_this_pass():
    """All rules read one snapshot taken before any write, so R1 cannot
    act on a trigger R0 is adding in the same pass."""
    item = run(board(
        assignments=[assignment(content="Send the quote", status="completed")],
        jobs=[job(status="quoting")],
        tasks=[task()],
    ))
    assert item["r1"] == []


# ==================================================================
# R2, one chase, seven days after the quote
# ==================================================================

def quoted_board(days_ago, **kwargs):
    changed = TODAY - timedelta(days=days_ago)
    return board(
        jobs=[job(status="quoted", changed=changed)],
        assignments=kwargs.pop("assignments", [
            assignment(record_id="a_sent", status="completed",
                       content="Send the quote", on_complete="quoted"),
        ]),
        quotes=kwargs.pop("quotes", [
            {"id": "q1", "job": "j1", "title": "QU-8090 Filler upgrade",
             "amount": 12500, "sent_date": stamp(changed)},
        ]),
        contacts=[{"id": "ct1", "client": "c1", "name": "Shane"}],
        **kwargs
    )


def test_the_chase_appears_on_day_seven():
    item = run(quoted_board(7))
    assert len(item["r2"]) == 1
    chase = item["r2"][0]
    assert chase["title"] == "Kauri Springs / Shane — Chase quote QU-8090"
    assert chase["due"].startswith(TODAY.isoformat())
    assert chase["section"] == "today"


def test_the_chase_does_not_appear_on_day_six():
    assert run(quoted_board(6))["r2"] == []


def test_the_chase_carries_the_header_lines_and_its_own_auto_line():
    chase = run(quoted_board(7))["r2"][0]
    assert chase["description"].splitlines()[0] == "⏱ Est: 15m"
    assert PROJECT in chase["description"]
    assert triggers.render_auto(triggers.QUOTED_CHASE, "j1") in chase["description"]
    assert "QU-8090" in chase["description"]


def test_only_one_chase_per_job_ever_even_after_it_is_completed():
    """A completed chase shows the chase happened, so no second one is
    created."""
    done_chase = assignment(record_id="a_chase", todoist_id="6bbbbbbbbbbbbbbb",
                            content="Kauri Springs / Shane — Chase quote QU-8090",
                            status="completed", auto_kind=triggers.QUOTED_CHASE)
    item = run(quoted_board(30, assignments=[done_chase]))
    assert item["r2"] == []
    assert any("already exists" in row["why"] for row in item["skipped"])


def test_an_archived_chase_still_counts_as_one_having_happened():
    gone = assignment(record_id="a_chase", content="Chase quote",
                      archived=True, auto_kind=triggers.QUOTED_CHASE)
    assert run(quoted_board(30, assignments=[gone]))["r2"] == []


def test_a_quoted_job_with_no_recorded_change_date_asks_rather_than_guesses():
    item = run(board(jobs=[job(status="quoted", changed=None)]))
    assert item["r2"] == []
    assert "backfill_status_changed" in item["needs_decision"][0]["why"]


def test_no_project_path_anywhere_means_no_chase_task():
    """With no project path on a task or task group, no chase is created,
    rather than one pointing at a guessed folder."""
    bare = assignment(record_id="a_sent", status="completed",
                      description="%s" % PIN)
    item = run(quoted_board(7, assignments=[bare]))
    assert item["r2"] == []
    assert "no project path" in item["needs_decision"][0]["why"]


def test_the_task_group_path_is_used_when_the_task_has_none():
    bare = assignment(record_id="a_sent", status="completed", description=PIN)
    item = run(quoted_board(
        7, assignments=[bare],
        task_groups=[{"id": "g1", "job": "j1",
                      "project_path": "SS Folders/Kauri Springs/PROJECT.md"}]))
    assert len(item["r2"]) == 1
    assert "SS Folders/Kauri Springs/PROJECT.md" in item["r2"][0]["description"]


def test_a_quote_with_no_number_is_chased_without_inventing_one():
    """With no quote number known, the title has none and the description
    says so; no number is made up."""
    item = run(quoted_board(7, quotes=[{"id": "q1", "job": "j1",
                                        "title": "Filler upgrade"}]))
    assert item["r2"][0]["title"] == "Kauri Springs / Shane — Chase quote"
    assert "with no number on it" in item["r2"][0]["description"]


def test_a_sensitive_account_is_never_chased_automatically():
    item = run(quoted_board(
        7, clients=[{"id": "c1", "name": "Kowhai Health NZ Ltd", "critical": True}]))
    assert item["r2"] == []
    assert any(row["rule"] == "r2" and "sensitive account" in row["why"]
               for row in item["needs_decision"])


def test_a_chase_long_overdue_is_booked_today_not_in_the_past():
    """A chase whose day has passed is booked from today, not in the past
    among parked overdue work."""
    chase = run(quoted_board(30))["r2"][0]
    assert chase["due"].startswith(TODAY.isoformat())


# ==================================================================
# R3, closing the chase once the job has moved on
# ==================================================================

def open_chase(**kwargs):
    values = dict(record_id="a_chase", todoist_id="6bbbbbbbbbbbbbbb",
                  content="Kauri Springs / Shane — Chase quote QU-8090",
                  auto_kind=triggers.QUOTED_CHASE)
    values.update(kwargs)
    return assignment(**values)


def test_the_chase_is_closed_when_the_job_reaches_invoicing():
    item = run(board(assignments=[open_chase()], jobs=[job(status="invoicing")]))
    assert len(item["r3"]) == 1
    closed = item["r3"][0]
    assert closed["archived_reason"] == "trigger: job moved to invoicing"
    assert closed["comment"].startswith("Closed by trigger: job moved to invoicing")


@pytest.mark.parametrize("status", ["won", "invoiced", "paid", "lost",
                                    "cancelled", "on_hold"])
def test_any_status_other_than_quoted_closes_the_chase(status):
    item = run(board(assignments=[open_chase()], jobs=[job(status=status)]))
    assert len(item["r3"]) == 1


def test_a_job_still_in_quoted_keeps_its_chase():
    item = run(board(assignments=[open_chase()],
                     jobs=[job(status="quoted", changed=TODAY)]))
    assert item["r3"] == []


def test_a_task_the_operator_wrote_with_the_same_title_is_never_closed():
    """R3 only closes tasks carrying auto_kind. Without it the engine does
    not touch a task, whatever the title says."""
    human = open_chase(auto_kind="")
    item = run(board(assignments=[human], jobs=[job(status="invoicing")]))
    assert item["r3"] == []


def test_a_chase_already_closed_is_not_closed_again():
    item = run(board(assignments=[open_chase(status="completed")],
                     jobs=[job(status="invoicing")]))
    assert item["r3"] == []


def test_a_sensitive_account_chase_is_reported_not_closed():
    item = run(board(
        assignments=[open_chase()], jobs=[job(status="invoicing", client="c_kh")],
        clients=[{"id": "c_kh", "name": "Kowhai Health NZ Ltd", "critical": True}]))
    assert item["r3"] == []
    assert any(row["rule"] == "r3" for row in item["needs_decision"])


# ==================================================================
# Applying the plan
# ==================================================================

def fakes_for(seen):
    pb = FakePB({
        "assignments": [dict(r) for r in seen["assignments"]],
        "jobs": [dict(r) for r in seen["jobs"]],
        "clients": [dict(r) for r in seen["clients"]],
        "contacts": [dict(r) for r in seen["contacts"]],
        "quotes": [dict(r) for r in seen["quotes"]],
        "invoices": [dict(r) for r in seen["invoices"]],
        "task_groups": [dict(r) for r in seen["task_groups"]],
        "interactions": [],
    })
    todoist = FakeTodoist([dict(t) for t in seen["tasks"]], sections=dict(SECTIONS))
    return pb, todoist


def apply_once(seen, tmp_path):
    pb, todoist = fakes_for(seen)
    ledger = WriteLedger(tmp_path / "state" / "writes.jsonl", run_id="testrun")
    client = RecordingClient(pb, ledger, source="daemon.triggers")
    item = triggers.plan(daemon_triggers.gather(todoist, pb), rules(), today=TODAY)
    done, failures = daemon_triggers.apply(client, todoist, item, ledger=ledger)
    return pb, todoist, ledger, item, done, failures


def test_applying_a_move_writes_the_status_the_date_and_who_did_it(tmp_path):
    seen = board(
        assignments=[assignment(status="completed", on_complete="quoted")],
        jobs=[job(status="quoting")],
    )
    pb, _, _, _, done, failures = apply_once(seen, tmp_path)

    assert failures == []
    assert done["jobs_moved"] == 1
    moved = pb.get("jobs", "j1")
    assert moved["status"] == "quoted"
    assert moved[triggers.JOB_CHANGED_BY] == "task:6aaaaaaaaaaaaaaa"
    assert moved[triggers.JOB_CHANGED_AT]


def test_every_action_writes_exactly_one_interaction(tmp_path):
    seen = board(
        assignments=[assignment(status="completed", on_complete="quoted")],
        jobs=[job(status="quoting")],
    )
    pb, _, _, _, _, _ = apply_once(seen, tmp_path)

    logged = pb.list_all("interactions")
    assert len(logged) == 1
    assert logged[0]["source_key"] == "trigger:" + triggers.source_id(
        "r1", "a1", "quoted")
    assert logged[0]["subject"] == "Project quoted: Filler upgrade"
    assert "was completed" in logged[0]["summary"]


def test_a_second_run_over_the_same_board_writes_nothing(tmp_path):
    """Idempotency end to end: the engine runs on every loop pass, so a
    second run over the same board must write nothing."""
    seen = board(
        assignments=[assignment(status="completed", on_complete="quoted")],
        jobs=[job(status="quoting")],
    )
    pb, todoist, ledger, _, _, _ = apply_once(seen, tmp_path)
    before = len(pb.writes)

    client = RecordingClient(pb, ledger, source="daemon.triggers")
    again = triggers.plan(daemon_triggers.gather(todoist, pb), rules(), today=TODAY)
    done, failures = daemon_triggers.apply(client, todoist, again, ledger=ledger)

    assert failures == []
    assert done == {"triggers_added": 0, "jobs_moved": 0, "chases_created": 0,
                    "chases_closed": 0}
    assert len(pb.writes) == before
    assert len(pb.list_all("interactions")) == 1


def test_every_write_appears_in_the_ledger(tmp_path):
    seen = board(
        assignments=[assignment(status="completed", on_complete="quoted")],
        jobs=[job(status="quoting")],
    )
    _, _, ledger, _, _, _ = apply_once(seen, tmp_path)

    entries = ledger.entries("testrun")
    collections = [e["collection"] for e in entries]
    assert "jobs" in collections
    assert "interactions" in collections


def test_creating_a_chase_writes_the_task_the_record_and_the_history(tmp_path):
    seen = quoted_board(7)
    pb, todoist, ledger, _, done, failures = apply_once(seen, tmp_path)

    assert failures == []
    assert done["chases_created"] == 1
    assert len(todoist.added) == 1
    assert todoist.added[0]["content"].endswith("Chase quote QU-8090")

    created = [r for r in pb.list_all("assignments")
               if r.get(triggers.PB_AUTO_FIELD) == triggers.QUOTED_CHASE]
    assert len(created) == 1
    assert created[0]["source"] == "ai_suggested"
    assert created[0]["job"] == "j1"
    assert created[0]["todoist_id"] == todoist.added[0]["id"]

    logged = pb.list_all("interactions")
    assert logged[0]["subject"].startswith("Chase task created:")
    assert todoist.added[0]["id"] in logged[0]["summary"]


def test_closing_a_chase_comments_first_then_completes_and_archives(tmp_path):
    seen = board(assignments=[open_chase()], jobs=[job(status="invoicing")])
    pb, todoist, _, _, done, failures = apply_once(seen, tmp_path)

    assert failures == []
    assert done["chases_closed"] == 1
    assert todoist.comments and "moved to invoicing" in todoist.comments[0][1]
    assert todoist.completed == ["6bbbbbbbbbbbbbbb"]

    record = pb.get("assignments", "a_chase")
    assert record["archived"] is True
    assert record["archived_reason"] == "trigger: job moved to invoicing"
    assert record["status"] == "completed"


def test_adding_a_trigger_writes_the_line_to_todoist_and_the_field_to_pocketbase(tmp_path):
    seen = board(
        assignments=[assignment(content="Send the quote")],
        jobs=[job(status="quoting")],
        tasks=[task(content="Send the quote")],
    )
    pb, todoist, _, _, done, failures = apply_once(seen, tmp_path)

    assert failures == []
    assert done["triggers_added"] == 1
    assert pb.get("assignments", "a1")[triggers.PB_STATUS_FIELD] == "quoted"
    sent = dict(todoist.rescheduled)["6aaaaaaaaaaaaaaa"]
    assert triggers.render_on_complete("quoted") in sent["description"]


def test_working_out_the_plan_writes_nothing_at_all(tmp_path):
    """plan() is a pure function, so building a plan writes nothing to
    either fake client."""
    seen = quoted_board(7)
    pb, todoist = fakes_for(seen)
    item = triggers.plan(daemon_triggers.gather(todoist, pb), rules(), today=TODAY)

    assert triggers.actions(item), "this board should have had something to do"
    assert pb.writes == []
    assert todoist.added == []
    assert todoist.completed == []
    assert todoist.comments == []


def test_the_telegram_message_is_empty_when_nothing_happened():
    """The pass runs on every loop, so it sends no message when nothing
    happened."""
    empty = {"triggers_added": 0, "jobs_moved": 0, "chases_created": 0,
             "chases_closed": 0}
    item = run(board())
    assert daemon_triggers.message(item, empty) == ""


def test_the_telegram_message_names_what_moved():
    item = run(board(
        assignments=[assignment(status="completed", on_complete="quoted")],
        jobs=[job(status="quoting")],
    ))
    text = daemon_triggers.message(item, {
        "triggers_added": 0, "jobs_moved": 1, "chases_created": 0,
        "chases_closed": 0})
    assert "1 project moved on" in text
    assert "Filler upgrade" in text


# ==================================================================
# The settings file and the code agree
# ==================================================================

def test_the_shipped_settings_match_the_defaults_in_the_code():
    """The code carries defaults so tests need no settings file. This test
    checks config/settings.yaml still agrees with them."""
    import yaml
    from pathlib import Path

    shipped = yaml.safe_load(
        (Path(__file__).resolve().parent.parent / "config" / "settings.yaml").read_text()
    )["triggers"]

    assert shipped["quoted_chase_after_days"] == 7
    assert tuple(shipped["forward_order"]) == triggers.FORWARD_ORDER
    for target, spec in triggers.DEFAULT_TITLE_PATTERNS.items():
        assert shipped["title_patterns"][target]["when_status"] == spec["when_status"]
        assert shipped["title_patterns"][target]["patterns"] == spec["patterns"]
