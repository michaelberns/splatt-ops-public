"""
Tests for tools/tick_off.py, the cleanup that ticks off tasks the mail
shows are done.

It is the only tool in the repo that takes work off the board, so the tests
concentrate on what it refuses to do:

  it closes nothing without a reference number tying an email to the task
  a stale evidence file stops the run rather than shortening it
  the reason is written into the task before the task is closed
  a dry run closes nothing at all
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.evidence import CHASED, DONE, MAYBE, UNKNOWN
from daemon.waiting import WaitingRouter
from tests.conftest_splatt import TODAY, FakeSettings
from tests.fakes import FakeTodoist
from tools import tick_off

router = WaitingRouter(FakeSettings())


def task(task_id="6aaaaaaaaaaaaaaa", content="Send QU-7994 to Taponera",
         description="📌 Real due: 2026-07-12 | 46 days late",
         added=date(2026, 6, 1)):
    return {
        "id": task_id,
        "content": content,
        "description": description,
        "priority": 4,
        "labels": [],
        "due": {"date": "2026-08-26"},
        "added_at": added.isoformat(),
    }


def email(direction="sent", subject="QU-7994 gripper assembly",
          when="2026-08-21", who="kyle@cwb.example.co.nz"):
    return {"id": "18f2a9", "direction": direction, "date": when,
            "subject": subject, "from": who, "snippet": ""}


def evidence_file(tmp_path, rows, fetched_at=None):
    path = tmp_path / "evidence.json"
    body = {"emails": rows}
    if fetched_at:
        body["fetched_at"] = fetched_at
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def plan(tasks, rows):
    emails = tick_off.load_evidence_rows(rows)
    return tick_off.plan(tasks, emails, TODAY, router)


# reading the evidence file
def test_a_missing_evidence_file_stops_the_run_and_says_how_to_fix_it(tmp_path):
    with pytest.raises(tick_off.EvidenceError) as caught:
        tick_off.load_evidence(tmp_path / "nope.json")
    assert "SS agent" in str(caught.value)


def test_a_stale_evidence_file_is_refused(tmp_path):
    """A run against an old mailbox would miss exactly the tasks finished
    since it was fetched."""
    old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    path = evidence_file(tmp_path, [email()], fetched_at=old)
    with pytest.raises(tick_off.EvidenceError) as caught:
        tick_off.load_evidence(path)
    assert "too old" in str(caught.value)


def test_a_fresh_evidence_file_is_read(tmp_path):
    now = datetime.now(timezone.utc).isoformat()
    path = evidence_file(tmp_path, [email()], fetched_at=now)
    assert len(tick_off.load_evidence(path)) == 1


def test_a_file_with_no_fetched_at_is_read_rather_than_refused(tmp_path):
    """An unstamped file is a hand written one, and refusing it would make
    the tool untestable by hand."""
    path = evidence_file(tmp_path, [email()])
    assert len(tick_off.load_evidence(path)) == 1


def test_a_bare_list_of_emails_is_accepted(tmp_path):
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps([email()]), encoding="utf-8")
    assert len(tick_off.load_evidence(path)) == 1


def test_something_that_is_not_emails_at_all_is_refused(tmp_path):
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps({"emails": "lots"}), encoding="utf-8")
    with pytest.raises(tick_off.EvidenceError):
        tick_off.load_evidence(path)


# planning
def test_a_sent_email_quoting_the_quote_number_marks_the_operators_own_work_done():
    rows = plan([task()], [email()])
    assert rows[0].verdict == DONE
    assert rows[0].age == 46


def test_a_task_naming_no_reference_is_left_alone():
    rows = plan([task(content="Clearwater Bottling, follow up with Kyle")], [email()])
    assert rows[0].verdict == UNKNOWN
    assert not rows[0].finding.actionable


def test_a_waiting_task_needs_a_reply_not_a_send():
    rows = plan(
        [task(content="QU-7994, awaiting client approval")],
        [email(direction="sent", subject="QU-7994 any news")],
    )
    assert rows[0].verdict == CHASED


def test_a_payment_question_is_never_marked_done():
    rows = plan(
        [task(content="Check if Kauri Springs paid INV-41352")],
        [email(direction="received", subject="Re: INV-41352 remittance")],
    )
    assert rows[0].verdict == MAYBE


def test_the_age_comes_from_the_pinned_real_due_date_not_the_shown_one():
    """Same rule as everywhere else: the date Todoist shows says one day
    late, the pinned real due date says 46."""
    rows = plan([task()], [email()])
    assert rows[0].age == 46


# writing
def test_a_dry_run_closes_nothing():
    tasks = [task()]
    todoist = FakeTodoist(tasks)
    rows = plan(tasks, [email()])

    assert rows[0].verdict == DONE
    assert todoist.completed == []
    assert todoist.rescheduled == []


def test_the_reason_is_written_into_the_task_before_it_is_closed():
    """A completed task cannot be updated afterwards, so the proof has to
    go in first or the completion has no recorded reason."""
    tasks = [task()]
    todoist = FakeTodoist(tasks)
    rows = plan(tasks, [email()])
    tick_off.apply_row(todoist, tasks[0], rows[0], TODAY)

    task_id, fields = todoist.rescheduled[0]
    assert "ticked off by the cleanup" in fields["description"]
    assert "2026-08-21" in fields["description"]
    assert todoist.completed == [rows[0].task_id]


def test_the_pinned_real_due_line_survives_the_note():
    tasks = [task()]
    todoist = FakeTodoist(tasks)
    rows = plan(tasks, [email()])
    tick_off.apply_row(todoist, tasks[0], rows[0], TODAY)

    _, fields = todoist.rescheduled[0]
    assert fields["description"].splitlines()[0].startswith("📌 Real due:")


def test_closing_a_task_is_written_to_the_ledger():
    class FakeLedger:
        def __init__(self):
            self.entries = []

        def record(self, action, collection, record_id, fields, source, note=""):
            self.entries.append((collection, record_id, source, note))

    tasks = [task()]
    todoist = FakeTodoist(tasks)
    ledger = FakeLedger()
    rows = plan(tasks, [email()])
    tick_off.apply_row(todoist, tasks[0], rows[0], TODAY, ledger)

    assert ledger.entries[0][0] == "todoist_task"
    assert ledger.entries[0][2] == "tick_off"


# the report
def test_the_report_separates_the_four_verdicts():
    tasks = [
        task("6a", content="Send QU-7994 to Taponera"),
        task("6b", content="QU-8090, awaiting client approval"),
        task("6c", content="Check if Kauri Springs paid INV-41352"),
        task("6d", content="Clearwater Bottling, follow up with Kyle"),
    ]
    rows = plan(tasks, [
        email(subject="QU-7994 attached"),
        email(direction="sent", subject="QU-8090 any news"),
        email(direction="received", subject="Re: INV-41352"),
    ])
    text = tick_off.report(rows, TODAY, write=False)

    assert "DRY RUN" in text
    assert "1 proven done" in text
    assert "1 chased" in text
    assert "1 worth a look" in text
    assert "1 nothing found" in text


def test_the_report_shows_the_proof_next_to_every_completion():
    rows = plan([task()], [email()])
    text = tick_off.report(rows, TODAY, write=False)
    assert "proof:" in text
    assert "QU-7994" in text


def test_the_tasks_with_nothing_to_match_on_are_counted_not_listed():
    rows = plan([task(content="Clearwater Bottling, follow up with Kyle")], [email()])
    text = tick_off.report(rows, TODAY, write=False)
    assert "--all" in text
    assert "Clearwater Bottling" not in text

    listed = tick_off.report(rows, TODAY, write=False, show_all=True)
    assert "Clearwater Bottling" in listed


def test_a_write_run_and_a_dry_run_do_not_share_a_report_file():
    from core.config import Settings

    conf = Settings()
    assert (tick_off.report_path(conf, TODAY, write=False)
            != tick_off.report_path(conf, TODAY, write=True))


def test_the_report_path_resolves_against_the_real_settings_file():
    from core.config import Settings

    path = tick_off.report_path(Settings(), TODAY, write=False)
    assert path.name == "cleanup-2026-08-27-dryrun.txt"
    assert path.parent.name == "reports"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


# one reference on several tasks
def test_a_reference_shared_by_several_tasks_is_flagged_in_the_report():
    """One quote has several tasks: send it, chase it, invoice it. A reply
    quoting QU-7994 may close one and leave the others open, and nothing
    here can tell which. Not a refusal, but the report must show the doubt
    so the operator can overrule it."""
    tasks = [
        task("6a", content="Send QU-7994 to Taponera"),
        task("6b", content="Invoice QU-7994 once approved"),
    ]
    rows = plan(tasks, [email(subject="QU-7994 attached")])
    assert rows[0].shared == 1
    assert "careful" in "\n".join(rows[0].to_lines())


def test_a_reference_on_one_task_only_is_not_flagged():
    rows = plan([task()], [email()])
    assert rows[0].shared == 0
    assert "careful" not in "\n".join(rows[0].to_lines())
