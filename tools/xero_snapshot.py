"""Turn raw Xero replies into one flat snapshot file.

Usage
    python -m tools.xero_snapshot RAW.json OUT.json [--expect-organisation NAME]

    RAW.json holds the replies from the Xero calls, untouched, under the keys
    receivables, payables, invoices, contacts, cash and organisation.

Why it is split this way
    The dashboard shows money figures that come from Xero, but no
    background process here can call Xero: the working Xero connection is
    the connector inside the agent's (Claude's) session. So the agent makes
    the calls and saves the raw replies, and this module turns them into a
    snapshot. Everything from that point on is ordinary code reading a
    file, which can be run and tested with Xero switched off
    (tests/test_xero_gaps.py does exactly that). tools/xero_gaps.py then
    reads the snapshot and lists what needs attention.

What the snapshot says about itself
    It records when it was taken (captured_at) and when Xero last refreshed
    the reports it came from, so a stale snapshot is visible as stale.

    Sales invoices are complete for the period asked for. Bills are not:
    the connector only reports bills that are still unpaid. The snapshot
    says so in its "completeness" block (bills_complete: false), so nothing
    downstream presents a total of money paid out that silently leaves out
    every settled bill.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional


SNAPSHOT_VERSION = 1


class DemoDataError(Exception):
    """Raised when a snapshot is being built from Xero's demo company."""


# A Xero account can be connected to more than one organisation, and one of
# them is often Xero's "Demo Company". Its data has exactly the same shape
# as a real company's, with plausible invoices and totals, so a capture
# taken from the wrong organisation would build without complaint and the
# dashboard would show money that does not exist.
#
# build_snapshot therefore refuses demo data outright rather than warning:
# there is no legitimate reason to build this snapshot from the demo
# company, and a warning written when nobody is watching would be missed.
DEMO_NAME_MARKERS = ("demo company",)

# The demo company's tax number. Xero uses the same one every time, so this
# still matches after the demo organisation has been renamed.
DEMO_TAX_NUMBERS = {"111-111-111"}


def _looks_like_demo(org: dict) -> Optional[str]:
    """Return why this organisation looks like the demo company, or None.

    Any one signal is enough: Xero's own is_demo flag, an org class of
    DEMO, "demo company" in the name, or the demo tax number.
    """
    if not org:
        return None

    # Xero's own flag is the most reliable signal, so it is checked first.
    flag = org.get("is_demo")
    if flag is True or str(flag).strip().lower() in {"yes", "true"}:
        return "Xero reports this organisation as a demo company"

    if str(org.get("org_class") or org.get("class") or "").strip().upper() == "DEMO":
        return "the organisation class is DEMO"

    name = str(org.get("name") or "").lower()
    for marker in DEMO_NAME_MARKERS:
        if marker in name:
            return "the organisation name contains %r" % marker

    tax = str(org.get("tax_number") or "").strip()
    if tax in DEMO_TAX_NUMBERS:
        return "the tax number %s belongs to Xero's demo company" % tax

    return None


def _num(value: Any) -> float:
    """Xero sends money as strings. Missing means zero."""
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _clean(value: Any) -> Optional[str]:
    """None for a missing, empty or whitespace-only value, else the stripped
    text. Xero uses both null and "" for "no value", and code that treated
    "" as an email address would send a reminder to nobody.

    >>> _clean("   ") is None
    True
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _invoice(raw: dict) -> dict:
    total = _num(raw.get("amount_total"))
    paid = _num(raw.get("amount_paid"))
    credited = _num(raw.get("amount_credited"))
    contact = raw.get("contact") or {}
    return {
        "invoice_number": _clean(raw.get("invoice_number")),
        "reference": _clean(raw.get("reference")),
        "status": _clean(raw.get("status")),
        "contact_name": _clean(contact.get("name")),
        "contact_id": _clean(contact.get("contact_id")),
        "total": total,
        "paid": paid,
        "credited": credited,
        "due": _num(raw.get("amount_due")),
        "tax": _num(raw.get("amount_tax")),
        "currency": _clean(raw.get("currency_code")),
        "issue_date": _clean(raw.get("invoice_date")),
        "due_date": _clean(raw.get("due_date")),
        # Xero marks an invoice PAID once its balance reaches zero, and a
        # credit note does that as well as a payment. An invoice cancelled
        # by a credit note therefore shows as PAID with no money received.
        # Worked out once, here, so every reader agrees on the meaning.
        "settled_by_credit": credited > 0 and paid == 0 and total > 0,
    }


def _bill(raw: dict) -> dict:
    contact = raw.get("contact") or {}
    return {
        "reference": _clean(raw.get("document_reference")),
        "supplier_name": _clean(contact.get("name")),
        "supplier_email": _clean(contact.get("email")),
        "total": _num(raw.get("total")),
        "issue_date": _clean(raw.get("document_date")),
        "due_date": _clean(raw.get("due_date")),
    }


def _receivable(raw: dict) -> dict:
    contact = raw.get("contact") or {}
    return {
        "invoice_number": _clean(raw.get("document_number")),
        "reference": _clean(raw.get("document_reference")),
        "contact_name": _clean(contact.get("name")),
        "contact_email": _clean(contact.get("email")),
        "total": _num(raw.get("total")),
        "issue_date": _clean(raw.get("document_date")),
        "due_date": _clean(raw.get("due_date")),
    }


def build_snapshot(
    *,
    receivables: dict,
    payables: dict,
    invoices: dict,
    contacts: dict,
    cash: Optional[dict] = None,
    organisation: Optional[dict] = None,
    captured_at: Optional[str] = None,
    expect_organisation: Optional[str] = None,
) -> dict:
    """Fold the raw Xero replies into one flat snapshot dict.

    Every argument is the reply from one Xero call, passed through
    untouched. Nothing here touches the network, so it can be tested with
    saved replies.

    Raises DemoDataError if the replies came from Xero's demo company, or,
    when `expect_organisation` is given, from any other organisation. A
    snapshot of the wrong ledger would look right and be wrong.
    """
    when = captured_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    org = organisation or {}

    reason = _looks_like_demo(org)
    if reason:
        raise DemoDataError(
            "Refusing to build a snapshot: %s. These figures are not real money. "
            "The capture used the wrong Xero connector." % reason
        )

    # The demo checks above miss a renamed demo organisation. Naming the
    # organisation expected closes that gap and also catches reading a real
    # but different company.
    if expect_organisation:
        got = _clean(org.get("name")) or _clean(receivables.get("organisation_name"))
        if (got or "").strip().lower() != expect_organisation.strip().lower():
            raise DemoDataError(
                "Refusing to build a snapshot: expected %r but the data came "
                "from %r." % (expect_organisation, got)
            )

    out_invoices = [_invoice(i) for i in (invoices.get("invoices") or [])]
    out_bills = [_bill(b) for b in (payables.get("aged_payables") or [])]
    out_receivables = [_receivable(r) for r in (receivables.get("aged_receivables") or [])]
    out_contacts = [
        {
            "contact_id": _clean(c.get("contact_id")),
            "name": _clean(c.get("name")),
            "email": _clean(c.get("email")),
        }
        for c in (contacts.get("contacts") or [])
    ]

    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "captured_at": when,
        "organisation": _clean(org.get("name"))
        or _clean(receivables.get("organisation_name")),
        "base_currency": _clean(org.get("base_currency"))
        or _clean(receivables.get("organisation_base_currency")),
        # Xero caches its reports. Keeping its refresh time shows when a
        # fresh snapshot was built from an older report.
        "xero_last_refreshed": _clean(receivables.get("last_refreshed")),
        "money": {
            "cash_balance": _num((cash or {}).get("cash_balance")),
            "owed_to_you": _num(receivables.get("total_outstanding")),
            "owed_by_you": _num(payables.get("total_outstanding")),
            "overdue_to_you": _num(receivables.get("overdue_total")),
            "overdue_by_you": _num(payables.get("overdue_total")),
            "invoiced_total": round(sum(i["total"] for i in out_invoices), 2),
            "received_total": round(sum(i["paid"] for i in out_invoices), 2),
            "credited_total": round(sum(i["credited"] for i in out_invoices), 2),
        },
        # Which halves are a complete picture. The connector lists every
        # sales invoice for the period but only the bills still owing, so a
        # "paid out" total built from this file would miss every settled
        # bill. Readers check this before adding anything up.
        "completeness": {
            "invoices_complete": True,
            "bills_complete": False,
            "bills_note": (
                "Only unpaid bills are available. Xero is not reporting "
                "bills that have already been paid, so money paid out "
                "cannot be totalled from this file."
            ),
        },
        "receivables": out_receivables,
        "payables": out_bills,
        "invoices": out_invoices,
        "contacts": out_contacts,
        "cash_available": bool(cash),
    }


def load_raw(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "raw",
        help="JSON file holding the raw Xero replies under the keys "
        "receivables, payables, invoices, contacts, cash, organisation",
    )
    parser.add_argument("out", help="where to write the snapshot")
    parser.add_argument(
        "--expect-organisation",
        help="refuse to write unless the data came from this organisation",
    )
    args = parser.parse_args()

    raw = load_raw(args.raw)
    try:
        snapshot = build_snapshot(
            receivables=raw.get("receivables") or {},
            payables=raw.get("payables") or {},
            invoices=raw.get("invoices") or {},
            contacts=raw.get("contacts") or {},
            cash=raw.get("cash"),
            organisation=raw.get("organisation"),
            expect_organisation=args.expect_organisation,
        )
    except DemoDataError as e:
        # Nothing is written: a file left behind by a refused run would be
        # found and trusted by the next reader.
        print("ERROR: %s" % e)
        return 1

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, indent=2)
        handle.write("\n")
    print("wrote %s, %d invoices, %d unpaid bills" % (
        args.out, len(snapshot["invoices"]), len(snapshot["payables"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
