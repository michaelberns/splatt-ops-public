"""Tests for tools/xero_snapshot.py and tools/xero_gaps.py.

These run with no network and no credentials, which is why the Xero calls
are kept separate from the logic.

The fixture data is fictional, but it has exactly the shape Xero returns,
because the bugs that matter here are about shape: Xero sends money as
strings, sends empty strings where you would expect nulls, and calls an
invoice PAID when a credit note cancelled it.

The fixture's numbers, for reference (as of TODAY, 2026-08-10):
    receivables  INV-41365     460.00  Drivetrain New Zealand, no email, 5 days late
                 SE INV-41370  12000.00 Island Beverages (Fiji), 36 days late
    payables     HD 100000001  5200.00 RIALTO Filling Srl, 60 days late
    invoices     INV-41359, 41363, 41365 and SE INV-41370; INV-41359
                 (1900.00) was cancelled by a credit note
"""

from datetime import date, timedelta

import pytest

from tools.xero_gaps import find_gaps, OVERDUE_DAYS
from tools.xero_snapshot import build_snapshot
from tools import xero_snapshot


TODAY = date(2026, 8, 10)


@pytest.fixture
def raw():
    """The four Xero replies, in the shape Xero actually returns them."""
    return {
        "receivables": {
            "organisation_name": "splatt engineering ltd",
            "organisation_base_currency": "NZD",
            "last_refreshed": "2026-08-10T02:40:06Z",
            "total_outstanding": "12460.0",
            "overdue_total": "12460.0",
            "aged_receivables": [
                {
                    "contact": {"name": "Drivetrain New Zealand", "email": None},
                    "document_number": "INV-41365",
                    "document_reference": "Ravi 08/07/2026",
                    "document_date": "2026-07-08",
                    "due_date": "2026-08-05",
                    "total": "460.0",
                },
                {
                    "contact": {
                        "name": "Island Beverages (Fiji) PTE LTD",
                        "email": "arun.tiwari@islandbev.example.com",
                    },
                    "document_number": "SE INV-41370",
                    "document_reference": "PO000099966",
                    "document_date": "2026-06-09",
                    "due_date": "2026-07-05",
                    "total": "12000.0",
                },
            ],
        },
        "payables": {
            "total_outstanding": "5200.0",
            "overdue_total": "5200.0",
            "aged_payables": [
                {
                    "contact": {"name": "RIALTO Filling Srl", "email": "roberta@rialtofilling.example.it"},
                    "document_reference": "HD 100000001",
                    "document_date": "2026-05-11",
                    "due_date": "2026-06-11",
                    "total": "5200.0",
                },
            ],
        },
        "invoices": {
            "invoices": [
                {
                    "invoice_number": "INV-41363",
                    "reference": "Freight & duty recharge",
                    "status": "AUTHORISED",
                    "contact": {"contact_id": "c1", "name": "Bright Fizz Beverage Company"},
                    "amount_total": "1100.0",
                    "amount_paid": "0.0",
                    "amount_credited": "0.0",
                    "amount_due": "1100.0",
                    "amount_tax": "20.0",
                    "currency_code": "NZD",
                    "invoice_date": "2026-05-22",
                    "due_date": "2026-06-30",
                },
                {
                    "invoice_number": "INV-41359",
                    "reference": "Freight & duty recharge",
                    "status": "PAID",
                    "contact": {"contact_id": "c1", "name": "Bright Fizz Beverage Company"},
                    "amount_total": "1900.0",
                    "amount_paid": "0.0",
                    "amount_credited": "1900.0",
                    "amount_due": "0.0",
                    "amount_tax": "90.0",
                    "currency_code": "NZD",
                    "invoice_date": "2026-06-18",
                    "due_date": "2026-07-05",
                },
                {
                    "invoice_number": "INV-41365",
                    "reference": "Ravi",
                    "status": "AUTHORISED",
                    "contact": {"contact_id": "c2", "name": "Drivetrain New Zealand"},
                    "amount_total": "460.0",
                    "amount_paid": "0.0",
                    "amount_credited": "0.0",
                    "amount_due": "460.0",
                    "amount_tax": "60.0",
                    "currency_code": "NZD",
                    "invoice_date": "2026-07-08",
                    "due_date": "2026-08-05",
                },
                {
                    "invoice_number": "SE INV-41370",
                    "reference": "PO000099966",
                    "status": "AUTHORISED",
                    "contact": {"contact_id": "c3", "name": "Island Beverages (Fiji) PTE LTD"},
                    "amount_total": "12000.0",
                    "amount_paid": "0.0",
                    "amount_credited": "0.0",
                    "amount_due": "12000.0",
                    "amount_tax": "0.0",
                    "currency_code": "NZD",
                    "invoice_date": "2026-06-09",
                    "due_date": "2026-07-05",
                },
            ]
        },
        "contacts": {
            "contacts": [
                {"contact_id": "c2", "name": "Drivetrain New Zealand", "email": None},
                {"contact_id": "c1", "name": "Bright Fizz Beverage Company", "email": "aaron@brightfizz.example.nz"},
            ]
        },
        "cash": {"cash_balance": "8450.259999999998"},
    }


@pytest.fixture
def snapshot(raw):
    return build_snapshot(
        receivables=raw["receivables"],
        payables=raw["payables"],
        invoices=raw["invoices"],
        contacts=raw["contacts"],
        cash=raw["cash"],
        captured_at="2026-08-10T02:40:06+00:00",
    )


# ---------------------------------------------------------------------------
# Building the snapshot
# ---------------------------------------------------------------------------

def test_money_arrives_as_numbers_not_strings(snapshot):
    """Xero sends every amount as a string. If any of them reach the
    dashboard still a string, adding two together concatenates them and the
    total silently becomes nonsense instead of raising."""
    assert snapshot["money"]["owed_to_you"] == 12460.0
    assert snapshot["money"]["cash_balance"] == pytest.approx(8450.26, abs=0.01)
    for invoice in snapshot["invoices"]:
        for field in ("total", "paid", "credited", "due", "tax"):
            assert isinstance(invoice[field], float), field


def test_the_file_admits_bills_are_incomplete(snapshot):
    """The connector cannot list paid bills. Anything totalling money paid
    out from this file would be wrong, so the file has to say so itself
    rather than leave a reader to find out."""
    assert snapshot["completeness"]["invoices_complete"] is True
    assert snapshot["completeness"]["bills_complete"] is False
    assert "paid" in snapshot["completeness"]["bills_note"]


def test_a_snapshot_carries_the_time_it_was_taken(snapshot):
    assert snapshot["captured_at"] == "2026-08-10T02:40:06+00:00"
    assert snapshot["xero_last_refreshed"] == "2026-08-10T02:40:06Z"


def test_an_empty_email_counts_as_no_email():
    """Xero uses null in some places and an empty string in others. Code
    that treats "" as an address sends the chaser to nobody and reports
    success."""
    built = build_snapshot(
        receivables={"aged_receivables": [
            {"contact": {"name": "Blank", "email": "   "}, "document_number": "INV-1",
             "due_date": "2026-01-01", "total": "10"}]},
        payables={}, invoices={}, contacts={},
    )
    assert built["receivables"][0]["contact_email"] is None


def test_a_credit_settled_invoice_is_marked_at_build_time(snapshot):
    by_number = {i["invoice_number"]: i for i in snapshot["invoices"]}
    assert by_number["INV-41359"]["settled_by_credit"] is True
    assert by_number["INV-41363"]["settled_by_credit"] is False


def test_a_normal_paid_invoice_is_not_called_credit_settled():
    """Guards the obvious way to get the credit check wrong, which is to
    look at the credited amount alone and flag every part-credited invoice
    that was then genuinely paid."""
    built = build_snapshot(
        receivables={}, payables={}, contacts={},
        invoices={"invoices": [{
            "invoice_number": "INV-9", "amount_total": "100",
            "amount_paid": "80", "amount_credited": "20", "amount_due": "0",
        }]},
    )
    assert built["invoices"][0]["settled_by_credit"] is False


# ---------------------------------------------------------------------------
# Finding the gaps
# ---------------------------------------------------------------------------

def kinds(result):
    return [f["kind"] for f in result["findings"]]


def only(result, kind):
    return [f for f in result["findings"] if f["kind"] == kind]


def test_a_contact_with_no_email_and_money_owing_is_flagged(snapshot):
    result = find_gaps(snapshot, as_of=TODAY)
    found = only(result, "missing_contact_email")
    assert len(found) == 1
    assert "Drivetrain New Zealand" in found[0]["title"]
    assert found[0]["amount"] == 460.0
    assert found[0]["confidence"] == "certain"


def test_a_contact_with_an_email_is_left_alone(snapshot):
    result = find_gaps(snapshot, as_of=TODAY)
    titles = " ".join(f["title"] for f in only(result, "missing_contact_email"))
    assert "Island Beverages" not in titles


def test_overdue_is_counted_from_the_due_date(snapshot):
    result = find_gaps(snapshot, as_of=TODAY)
    ibf = [f for f in only(result, "overdue_receivable")
            if f["evidence"]["invoice_number"] == "SE INV-41370"]
    assert len(ibf) == 1
    assert ibf[0]["evidence"]["days_overdue"] == 36


def test_an_invoice_inside_the_threshold_is_not_flagged(snapshot):
    """INV-41365 fell due on 5 August and it is the 10th. Five days late is
    a slow payer, not a collection problem, and flagging it would bury the
    findings that matter."""
    result = find_gaps(snapshot, as_of=TODAY)
    numbers = [f["evidence"]["invoice_number"] for f in only(result, "overdue_receivable")]
    assert "INV-41365" not in numbers


def test_the_threshold_is_the_boundary_not_a_suggestion():
    """Exactly OVERDUE_DAYS late is flagged; one day short is not."""
    due = date(2026, 7, 11)
    built = build_snapshot(
        receivables={"aged_receivables": [
            {"contact": {"name": "X", "email": "x@x.com"}, "document_number": "INV-1",
             "due_date": due.isoformat(), "total": "100"}]},
        payables={}, invoices={}, contacts={},
    )
    fires_on = due + timedelta(days=OVERDUE_DAYS)
    assert only(find_gaps(built, as_of=fires_on), "overdue_receivable")
    assert not only(find_gaps(built, as_of=fires_on - timedelta(days=1)), "overdue_receivable")


def test_overdue_bills_are_flagged_as_well_as_overdue_invoices(snapshot):
    result = find_gaps(snapshot, as_of=TODAY)
    payable = only(result, "overdue_payable")
    assert len(payable) == 1
    assert payable[0]["amount"] == 5200.0
    assert "RIALTO Filling" in payable[0]["title"]


def test_the_credited_invoice_is_reported_as_likely_not_certain(snapshot):
    """A full credit usually means a reissue, but not always, so this is
    not stated as fact."""
    result = find_gaps(snapshot, as_of=TODAY)
    found = only(result, "settled_by_credit")
    assert len(found) == 1
    assert found[0]["evidence"]["invoice_number"] == "INV-41359"
    assert found[0]["confidence"] == "likely"


def test_missing_invoice_numbers_are_reported_as_a_lead(snapshot):
    """This check cannot see drafts or voided invoices, so it must not
    claim to know what happened to the missing numbers: the confidence
    must be "lead", never "certain"."""
    result = find_gaps(snapshot, as_of=TODAY)
    found = only(result, "invoice_number_gap")
    assert len(found) == 1
    assert found[0]["confidence"] == "lead"
    missing = found[0]["evidence"]["missing"]
    assert 41364 in missing
    assert 41363 not in missing
    assert 41359 not in missing


def test_a_complete_run_of_numbers_produces_no_gap_finding():
    built = build_snapshot(
        receivables={}, payables={}, contacts={},
        invoices={"invoices": [
            {"invoice_number": "INV-1", "amount_total": "1"},
            {"invoice_number": "INV-2", "amount_total": "1"},
            {"invoice_number": "INV-3", "amount_total": "1"},
        ]},
    )
    assert not only(find_gaps(built, as_of=TODAY), "invoice_number_gap")


def test_the_oddly_numbered_invoice_is_flagged(snapshot):
    """SE INV-41370 is the largest debt in the fixture and the only one
    that does not start with "INV-". Anything matching on invoice number
    would skip it."""
    result = find_gaps(snapshot, as_of=TODAY)
    found = only(result, "odd_invoice_number")
    assert len(found) == 1
    assert found[0]["evidence"]["invoice_number"] == "SE INV-41370"
    assert found[0]["evidence"]["expected_prefix"] == "INV-"


def test_findings_are_sorted_with_the_most_money_first(snapshot):
    result = find_gaps(snapshot, as_of=TODAY)
    amounts = [f["amount"] for f in result["findings"]]
    assert amounts == sorted(amounts, reverse=True)
    assert result["findings"][0]["amount"] == 12000.0


def test_the_headline_total_counts_each_document_once(snapshot):
    """SE INV-41370 trips two checks, overdue and odd numbering. A plain
    sum would count its $12,000 twice and report more money at stake than
    is owed in total.
    """
    result = find_gaps(snapshot, as_of=TODAY)
    ibf = [f for f in result["findings"] if f["document"] == "SE INV-41370"]
    assert len(ibf) == 2, "this test is pointless unless one document trips two checks"

    naive = sum(f["amount"] for f in result["findings"])
    assert result["total_amount_flagged"] < naive
    assert result["total_amount_flagged"] == pytest.approx(12000.0 + 5200.0 + 1900.0 + 460.0)


def test_an_empty_snapshot_produces_no_findings_and_does_not_raise():
    """An empty snapshot produces no findings rather than an error. Noticing
    that the snapshot itself is empty is the caller's job."""
    built = build_snapshot(receivables={}, payables={}, invoices={}, contacts={})
    result = find_gaps(built, as_of=TODAY)
    assert result["count"] == 0
    assert result["total_amount_flagged"] == 0


def test_a_missing_due_date_does_not_crash_the_overdue_check():
    built = build_snapshot(
        receivables={"aged_receivables": [
            {"contact": {"name": "X", "email": "x@x.com"}, "document_number": "INV-1",
             "due_date": None, "total": "100"}]},
        payables={}, invoices={}, contacts={},
    )
    result = find_gaps(built, as_of=TODAY)
    assert not only(result, "overdue_receivable")


def test_the_threshold_is_reported_so_the_reader_knows_what_late_means(snapshot):
    result = find_gaps(snapshot, as_of=TODAY)
    assert result["overdue_days_threshold"] == OVERDUE_DAYS
    assert result["snapshot_captured_at"] == snapshot["captured_at"]


# ------------------------------------------------------- the demo company
#
# A Xero login can reach more than one organisation, and Xero's Demo Company
# produces data of exactly the same shape with fake money in it. Nothing in
# the figures gives it away, so the check happens when the snapshot is
# built, before anything downstream can use it.


DEMO_ORG = {
    "name": "Demo Company (NZ)",
    "base_currency": "NZD",
    "tax_number": "111-111-111",
    "is_demo": True,
    "org_class": "DEMO",
}


def _build(raw, org, **kw):
    return xero_snapshot.build_snapshot(
        receivables=raw.get("receivables") or {},
        payables=raw.get("payables") or {},
        invoices=raw.get("invoices") or {},
        contacts=raw.get("contacts") or {},
        cash=raw.get("cash"),
        organisation=org,
        **kw
    )


def test_the_demo_company_is_refused_outright(raw):
    with pytest.raises(xero_snapshot.DemoDataError):
        _build(raw, DEMO_ORG)


def test_each_demo_signal_stands_on_its_own(raw):
    """Four separate signals, and any one of them is enough, so the check
    still holds if Xero stops setting the flag or the organisation is
    renamed."""
    for key, value in (
        ("is_demo", True),
        ("is_demo", "Yes"),
        ("org_class", "DEMO"),
        ("name", "Demo Company (NZ)"),
        ("tax_number", "111-111-111"),
    ):
        org = {"name": "splatt engineering ltd", "base_currency": "NZD"}
        org[key] = value
        with pytest.raises(xero_snapshot.DemoDataError):
            _build(raw, org)


def test_the_real_company_builds_fine(raw):
    """The guard must let the real organisation through."""
    snap = _build(raw, {"name": "splatt engineering ltd", "base_currency": "NZD"})
    assert snap["organisation"] == "splatt engineering ltd"
    assert snap["money"]["owed_to_you"] == 12460.0


def test_naming_the_org_you_wanted_catches_the_wrong_one(raw):
    """A renamed demo organisation would pass the name check. Naming the
    company expected closes that gap and also catches reading a real but
    different company."""
    org = {"name": "Some Other Company Ltd", "base_currency": "NZD"}
    with pytest.raises(xero_snapshot.DemoDataError) as e:
        _build(raw, org, expect_organisation="splatt engineering ltd")
    assert "Some Other Company Ltd" in str(e.value)


def test_naming_the_right_org_lets_it_through(raw):
    snap = _build(
        raw,
        {"name": "splatt engineering ltd", "base_currency": "NZD"},
        expect_organisation="Splatt Engineering Ltd",
    )
    assert snap["organisation"] == "splatt engineering ltd"


def test_a_missing_org_name_still_has_to_match_when_one_is_expected(raw):
    """The organisation block can come back empty. The name on the
    receivables report is used instead, and the check must still fail when
    that does not match either."""
    with pytest.raises(xero_snapshot.DemoDataError):
        _build(raw, {}, expect_organisation="Some Company That Is Not In The Data")
