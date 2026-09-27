"""
Tests for deciding whether an email proves a task is done (core/evidence.py).

A task wrongly left open costs the operator a few seconds of reading; a
task wrongly closed disappears from the board. So most of these tests
pin down refusals: no reference, a reference only in the notes, mail
older than the task, and money tasks. The rest pin down which email is
reported and how direction (sent or received) changes the verdict.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import evidence
from core.evidence import CHASED, DONE, MAYBE, UNKNOWN, Email
from daemon.waiting import CLIENT, SUPPLIER, WaitingRouter
from tests.conftest_splatt import FakeSettings

TODAY = date(2026, 8, 27)
router = WaitingRouter(FakeSettings())


def task(content="do the thing", description="", added=date(2026, 6, 1)):
    return {
        "id": "6aaaaaaaaaaaaaaa",
        "content": content,
        "description": description,
        "added_at": added.isoformat(),
    }


def mail(direction="sent", subject="", when=date(2026, 8, 21),
         who="kyle@cwb.example.co.nz", snippet=""):
    return Email(message_id="18f2a9", direction=direction, when=when,
                 subject=subject, snippet=snippet, who=who)


def look(the_task, emails, **kwargs):
    return evidence.look(the_task, emails, real_due=date(2026, 7, 1), **kwargs)


# reading a reference out of text
@pytest.mark.parametrize("text,expected", [
    ("Open quote QU-7994 (Taponera)", ["QU7994"]),
    ("qu 7994 approved", ["QU7994"]),
    ("qu7994", ["QU7994"]),
    ("Check Kauri paid INV-41352", ["INV41352"]),
    ("Their PO 845102 arrived", ["PO845102"]),
    ("QU-7994 and INV-41352", ["QU7994", "INV41352"]),
])
def test_a_reference_is_read_however_it_is_written(text, expected):
    assert evidence.references(text) == expected


def test_the_same_number_on_two_kinds_of_paper_is_two_references():
    """The prefix is part of the reference, so an email about PO 7994
    cannot close a task about quote 7994."""
    assert evidence.references("QU-7994") != evidence.references("PO-7994")


def test_a_small_number_in_a_sentence_is_not_a_reference():
    assert evidence.references("send 4 belts, PO 12") == []


def test_a_reference_is_only_counted_once_however_often_it_appears():
    assert evidence.references("QU-7994, re QU-7994, see QU 7994") == ["QU7994"]


# the refusals
def test_a_task_with_no_reference_number_is_never_closed():
    """Names are not evidence. One client can have many open tasks, and
    an email from that client would match all of them equally well."""
    found = look(task("Clearwater Bottling, follow up with Kyle"),
                 [mail(subject="Re: gripper assembly")])
    assert found.verdict == UNKNOWN
    assert not found.actionable


def test_a_reference_nobody_mentioned_is_not_evidence():
    found = look(task("Open quote QU-7994"), [mail(subject="Re: QU-8090")])
    assert found.verdict == UNKNOWN


def test_mail_from_before_the_task_existed_does_not_count():
    """The window starts at the task's creation date, so the email that
    caused the task cannot close it."""
    found = look(
        task("Chase QU-7994 with the client", added=date(2026, 8, 1)),
        [mail(direction="received", subject="QU-7994", when=date(2026, 7, 15))],
    )
    assert found.verdict == UNKNOWN


def test_money_owed_is_never_closed_by_email_however_good_the_match():
    """An email about INV-41352 could be the client querying it rather
    than paying it. Xero knows whether money arrived and the mailbox does
    not, so the verdict is MAYBE and the reason points at Xero."""
    money = task("Check if Kauri Springs paid INV-41352 (freight & duty)")
    found = look(
        money,
        [mail(direction="received", subject="Re: INV-41352 remittance")],
        waiting_kind=router.classify(money),
        money_owed=router.is_money_owed(money),
    )
    assert found.verdict == MAYBE
    assert "Xero" in found.why


def test_the_money_rule_is_the_one_the_router_already_uses():
    """`money_owed` comes from `WaitingRouter.is_money_owed`, the same
    pattern list the waiting router uses, so there is only one copy."""
    assert router.is_money_owed(
        task("Check if payment for INV-41369 has arrived")
    )
    assert not router.is_money_owed(task("Open quote QU-7994"))


# waiting on somebody else
def test_a_reply_arriving_ends_the_wait():
    waiting = task("QU-7994, awaiting client approval")
    found = look(
        waiting,
        [mail(direction="received", subject="Re: QU-7994 approved")],
        waiting_kind=CLIENT,
    )
    assert found.verdict == DONE
    assert found.actionable
    assert "came back" in found.why


def test_sending_another_email_does_not_end_a_wait():
    """On a waiting task an outgoing email is a chase (CHASED), not an
    answer."""
    found = look(
        task("QU-7994, awaiting client approval"),
        [mail(direction="sent", subject="Re: QU-7994 any news")],
        waiting_kind=CLIENT,
    )
    assert found.verdict == CHASED
    assert not found.actionable


def test_a_supplier_coming_back_ends_the_wait_the_same_way():
    found = look(
        task("Awaiting Coastline ETA on PO-845102"),
        [mail(direction="received", subject="PO 845102 shipping Tuesday",
              who="ops@coastline.example.co.nz")],
        waiting_kind=SUPPLIER,
    )
    assert found.verdict == DONE


# the operator's own work
def test_sending_the_thing_is_doing_the_thing():
    found = look(
        task("Send QU-7994 to Taponera"),
        [mail(direction="sent", subject="QU-7994 gripper assembly")],
    )
    assert found.verdict == DONE
    assert "you sent it" in found.why


def test_mail_arriving_about_his_own_work_is_only_worth_a_look():
    """On the operator's own task, an incoming email about QU-7994 does
    not show that the operator did the work, so it is only MAYBE."""
    found = look(
        task("Send QU-7994 to Taponera"),
        [mail(direction="received", subject="any news on QU-7994?")],
    )
    assert found.verdict == MAYBE
    assert not found.actionable


# picking which email to show
def test_the_newest_matching_email_is_the_one_reported():
    """The last message in a thread says where it ended up."""
    found = look(
        task("QU-7994, awaiting client approval"),
        [
            mail(direction="received", subject="QU-7994 question",
                 when=date(2026, 8, 10)),
            mail(direction="received", subject="QU-7994 approved, go ahead",
                 when=date(2026, 8, 25)),
        ],
        waiting_kind=CLIENT,
    )
    assert found.email.date == date(2026, 8, 25)


def test_a_reply_wins_over_a_chase_sent_the_same_week():
    found = look(
        task("QU-7994, awaiting client approval"),
        [
            mail(direction="sent", subject="QU-7994 any news",
                 when=date(2026, 8, 25)),
            mail(direction="received", subject="Re: QU-7994 approved",
                 when=date(2026, 8, 20)),
        ],
        waiting_kind=CLIENT,
    )
    assert found.verdict == DONE


def test_every_finding_carries_the_email_it_rests_on():
    """`proof()` describes the email (date, who, subject) so the report
    can show what each verdict rests on."""
    found = look(
        task("Send QU-7994 to Taponera"),
        [mail(direction="sent", subject="QU-7994 attached")],
    )
    assert found.proof()
    assert "2026-08-21" in found.proof()
    assert "QU-7994" in found.proof()


# the email shape
def test_an_email_reads_its_reference_out_of_the_body_as_well():
    found = mail(subject="that quote you wanted", snippet="attached is QU-7994")
    assert found.references == ["QU7994"]


def test_a_full_timestamp_and_a_bare_date_compare_the_same():
    """Emails carry full timestamps and tasks carry bare dates. Both are
    flattened to a plain date so they can be compared."""
    stamped = Email(direction="sent", when="2026-08-21T09:14:02Z",
                    subject="QU-7994")
    assert stamped.date == date(2026, 8, 21)


def test_an_email_with_no_date_cannot_be_evidence_of_anything():
    found = look(task("Send QU-7994"),
                 [Email(direction="sent", when="", subject="QU-7994")])
    assert found.verdict == UNKNOWN


# a number in the notes is not what the task is about
#
# Example shape: the task "Bright Fizz / Aaron, reach out re chain work"
# has a note "separate from the Wanda touch panel job (QU-8460/QU-8461),
# which is done". An email about the panel job must not close the chain
# job.
def test_a_number_only_in_the_notes_cannot_close_the_task():
    """The title says what the task is. A reference found only in the
    notes gives MAYBE, and the reason mentions the notes."""
    found = look(
        task("Reach out to Aaron about the chain work",
             description="separate from the panel job QU-8460, which is done"),
        [mail(direction="sent", subject="QU-8460 panel shipped")],
    )
    assert found.verdict == MAYBE
    assert not found.actionable
    assert "notes" in found.why


def test_a_number_in_the_title_still_closes_the_task():
    found = look(
        task("Send QU-7994 to Taponera", description="see also QU-8460"),
        [mail(direction="sent", subject="QU-7994 attached")],
    )
    assert found.verdict == DONE


def test_the_title_number_is_preferred_over_a_newer_one_from_the_notes():
    """Title references sort ahead of notes references, even when the
    notes match is newer, so the proof shown is about this task."""
    found = look(
        task("Send QU-7994 to Taponera", description="see also QU-8460"),
        [
            mail(direction="sent", subject="QU-7994 attached",
                 when=date(2026, 8, 10)),
            mail(direction="sent", subject="QU-8460 panel shipped",
                 when=date(2026, 8, 25)),
        ],
    )
    assert found.reference == "QU7994"
    assert found.verdict == DONE


def test_a_wait_ended_by_a_reply_about_a_number_from_the_notes_is_not_closed():
    """The same rule on the waiting side: a reply only ends the wait if
    its reference is in the title."""
    waiting = task("Chase Aaron on the chain work",
                   description="not the panel job QU-8460, that one is done")
    found = look(
        waiting,
        [mail(direction="received", subject="Re: QU-8460 panel arrived")],
        waiting_kind=CLIENT,
    )
    assert found.verdict == MAYBE
    assert not found.actionable


# the money wording has to be true in both directions
def test_a_bill_splatt_owes_is_refused_without_claiming_it_is_money_owed_in():
    """"Pay Greenlight INV-9184" is money going out. It is refused like
    money coming in, and the reason must not claim the money is owed to
    Splatt."""
    bill = task("Splatt / Accounts, pay Greenlight INV-9184")
    found = look(
        bill,
        [mail(direction="received", subject="Gentle Reminder | Invoice INV-9184")],
        money_owed=True,
    )
    assert found.verdict == MAYBE
    assert "Xero" in found.why
    assert "owed to Splatt" not in found.why


def test_a_money_task_says_so_when_the_only_match_came_from_the_notes():
    """When the only match came from the notes, the money reason adds a
    caveat that the email may be about a different job."""
    money = task("Check if Kauri Springs paid INV-41352",
                 description="chased alongside INV-41219")
    found = look(
        money,
        [mail(direction="received", subject="Re: INV-41219 rinser valve")],
        money_owed=True,
    )
    assert found.reference == "INV41219"
    assert "only in the notes" in found.why


def test_a_money_task_matched_on_its_own_number_gets_no_extra_caveat():
    money = task("Check if Kauri Springs paid INV-41352")
    found = look(money, [mail(direction="received", subject="Re: INV-41352")],
                 money_owed=True)
    assert "only in the notes" not in found.why


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
