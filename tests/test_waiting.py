"""
Tests for daemon/waiting.py: deciding whether a late task is waiting on a
client, waiting on a supplier, or the operator's own work.

The task titles are in the style of a real board (client, supplier,
quote and invoice references), with fictional names and numbers.

The detection is conservative on purpose. A task classed as waiting never
escalates, so anything ambiguous must stay with the operator's own work.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daemon.waiting import CLIENT, SUPPLIER, WaitingRouter
from tests.conftest_splatt import FakeSettings

router = WaitingRouter(FakeSettings())


def classify(content, description=""):
    return router.classify({"content": content, "description": description})


# work that is somebody else's move
@pytest.mark.parametrize("content", [
    "QU-7994 Taponera, awaiting client approval",
    "waiting on Clearwater Bottling to confirm the spec",
    "Bright Fizz INV-41363, no reply",
    "follow-up with the client on the deposit",
    "chase the quote",
    "still waiting on sign off",
    "client has not come back on the drawing",
])
def test_a_task_blocked_on_a_client_is_recognised(content):
    assert classify(content) == CLIENT


@pytest.mark.parametrize("content", [
    "awaiting Coastline freight ETA",
    "waiting for the QXP dispatch date",
    "supplier has not confirmed the lead time",
    "chase the purchase order with Rialto Filling",
    "follow up the back order on the seal kit",
    "awaiting customs clearance on the import",
])
def test_a_task_blocked_on_a_supplier_is_recognised(content):
    assert classify(content) == SUPPLIER


def test_a_supplier_thread_about_a_client_machine_is_still_a_supplier_task():
    """Supplier words are tested before client words, so a supplier
    thread that names the client's machine is still SUPPLIER."""
    assert classify("awaiting Coastline ETA for the Clearwater Bottling filler") == SUPPLIER


def test_waiting_on_nobody_in_particular_defaults_to_client():
    """A waiting task that names neither side is CLIENT, the side with
    the longer chase interval."""
    assert classify("still waiting on an answer") == CLIENT


# the operator's own work
@pytest.mark.parametrize("content", [
    "draw up the CAP-100 bracket",
    "order the seal kit",
    "site visit Thursday",
    "write the commissioning report",
    "book the freight",
])
def test_the_operators_own_work_is_not_marked_as_waiting(content):
    """Tasks with no waiting phrase are None (own work), even when they
    contain supplier words such as "freight"."""
    assert classify(content) is None


def test_the_word_delivery_alone_does_not_mean_waiting():
    """The waiting patterns describe the state of the conversation, not
    the subject, so "delivery" on its own is own work."""
    assert classify("arrange delivery of the capper") is None


def test_the_description_counts_as_well_as_the_title():
    assert classify("QU-7994 Taponera", "sent 12 July, awaiting a reply") == CLIENT


# chase timing
def test_a_client_waits_longer_than_a_supplier():
    assert router.chase_days(CLIENT) == 5
    assert router.chase_days(SUPPLIER) == 3


def test_chases_are_counted_off_the_description():
    task = {"description": "chase scheduled for Monday\nchase scheduled for Friday"}
    assert router.chase_count(task) == 2
    assert router.chase_count({"description": ""}) == 0


def test_two_chases_with_no_reply_is_the_end_of_it():
    """With the default max_chases of 2, one chase is not exhausted and
    two are."""
    assert not router.exhausted({"description": "chase scheduled"})
    assert router.exhausted(
        {"description": "chase scheduled\nchase scheduled"}
    )


def test_the_limits_come_from_settings():
    custom = WaitingRouter(FakeSettings({
        "waiting.client_chase_business_days": 10,
        "waiting.max_chases": 1,
    }))
    assert custom.chase_days(CLIENT) == 10
    assert custom.exhausted({"description": "chase scheduled"})


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


# Money owed to Splatt is always a client wait
#
# Splatt recharges freight and import duty to clients, so a task about
# collecting a payment is often full of supplier words. These must still
# be classed as waiting on a client.
@pytest.mark.parametrize("content", [
    "Check Clearwater Bottling paid INV-41370 (freight & duty) + INV-41229",
    "Check Kauri Springs paid INV-41352 (NZD 90.00, RIALTO Inv 162 freight)",
    "Bright Fizz / Sam — Check if payment for INV-41363 ($1,250.40) has arrived",
    "Hilltop / Mark Chen — Check INV-41350 freight share payment",
    "Northstar / Sam — Check if payment for INV-41369 ($1,480.20) has arrived",
])
def test_chasing_money_owed_to_splatt_is_a_client_wait(content):
    router = WaitingRouter(FakeSettings())
    assert router.classify({"content": content, "description": ""}) == CLIENT


def test_a_full_stop_inside_an_amount_does_not_break_the_match():
    """The gaps in the payment patterns are [^\\n], not [^.\\n], so the
    full stop inside a dollar amount does not stop the match."""
    router = WaitingRouter(FakeSettings())
    task = {"content": "Check if payment for INV-41363 ($1,250.40) has arrived",
            "description": ""}
    assert router.classify(task) == CLIENT


@pytest.mark.parametrize("content", [
    "Splatt / Accounts — Pay Greenlight INV-9184 ($40.00, overdue since 20 Jul)",
    "QXP / Partnerco — Pay import duty on waybill 1234567890",
    "Splatt Engineering / Sam — Send IBF Fiji $12,000 payment from Partnerco",
])
def test_money_splatt_owes_is_the_operators_own_work_not_a_wait(content):
    """Paying somebody is an action, not a wait. The incoming payment
    patterns do not match outgoing payments just because they contain
    the word payment."""
    router = WaitingRouter(FakeSettings())
    assert router.classify({"content": content, "description": ""}) is None


def test_a_purchase_order_no_longer_decides_the_side_of_the_desk():
    """A bare "PO" is not a supplier signal, because Splatt both issues
    and receives purchase orders. A client task full of the client's PO
    numbers is CLIENT."""
    router = WaitingRouter(FakeSettings())
    task = {"content": "Tui Valley — Chase 3 wrong PO references on paid invoices",
            "description": "Kyle's actual POs: PO-6504, PO-6514, PO6528"}
    assert router.classify(task) == CLIENT


def test_a_real_supplier_chase_still_goes_to_the_supplier_column():
    """Supplier chases without "PO" are still SUPPLIER."""
    router = WaitingRouter(FakeSettings())
    for content in [
        "Coastline — Chase ETA on the shipment",
        "Rialto Filling — chase the purchase order",
        "Chase the supplier on lead time for the gripper belt",
        "Chase QXP on the dispatch date",
    ]:
        assert router.classify({"content": content, "description": ""}) == SUPPLIER, content


def test_the_word_quote_alone_cannot_tell_the_two_sides_apart():
    """Known limitation: "Chase gripper quote" is classed as CLIENT even
    when a supplier owes the quote.

    Splatt sends quotes to clients and asks suppliers for them, so the
    word "quote" points both ways. Routing on the company name would fix
    this and is not built. Meanwhile the engine believes the column the
    operator filed a task in over the text, so a manual correction
    sticks; see test_a_manual_move_is_not_undone_the_next_morning in
    tests/test_overdue_ladder.py.
    """
    router = WaitingRouter(FakeSettings())
    task = {"content": "Polymer Developments / Joel — Chase gripper quote",
            "description": ""}
    assert router.classify(task) == CLIENT
