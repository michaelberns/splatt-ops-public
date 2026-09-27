"""Read a Xero snapshot and list what is missing or worth a second look.

Usage
    python -m tools.xero_gaps SNAPSHOT.json [--out FINDINGS.json]

It never touches Xero. It reads the file tools/xero_snapshot.py wrote and
returns a list of findings, so it runs anywhere (in a test, a scheduled job
or by hand) with no session and no credentials. The files server serves
the findings to the dashboard's Xero panel.

The checks
    missing_contact_email  an unpaid invoice whose contact has no email
    overdue_receivable     a sales invoice at least OVERDUE_DAYS past due
    overdue_payable        a bill at least OVERDUE_DAYS past due
    settled_by_credit      an invoice Xero calls paid that a credit note
                           cancelled, with no money received
    invoice_number_gap     numbers missing from the invoice sequence
    odd_invoice_number     an invoice numbered unlike the others

Every finding carries a confidence, because some findings are facts read
straight off the data and one is only a lead. The dashboard shows them
differently.

  certain  the data says this outright
  likely   the data strongly implies it, with a small chance of a harmless cause
  lead     worth opening Xero to check; this file cannot settle it

Findings are sorted by the money attached, largest first, and the headline
total counts each document once (see _total_distinct). A finding looks like:

    {"kind": "overdue_receivable", "confidence": "certain", "amount": 12000.0,
     "title": "SE INV-41370 is 36 days overdue", "detail": "...",
     "evidence": {...}, "document": "SE INV-41370"}
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any, Optional


# An unpaid invoice this many days past due is reported. Thirty days is one
# statement cycle: a few days late is a slow payer, a whole cycle late is a
# collection problem.
OVERDUE_DAYS = 30

# Splits an invoice number into a prefix and the trailing number, so
# "INV-41363" gives ("INV-", 41363) and "SE INV-41370" gives
# ("SE INV-", 41370). A number with no trailing digits cannot be sequenced
# and is skipped by the two numbering checks.
INVOICE_NUMBER_PATTERN = re.compile(r"^(?P<prefix>.*?)(?P<number>\d+)$")


def _parse_date(value: Any) -> Optional[date]:
    if not value:
        return None
    text = str(value)[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _days_overdue(due_date: Any, as_of: date) -> Optional[int]:
    parsed = _parse_date(due_date)
    if parsed is None:
        return None
    return (as_of - parsed).days


def _finding(kind, confidence, amount, title, detail, evidence, document=None):
    return {
        "kind": kind,
        "confidence": confidence,
        "amount": round(float(amount or 0), 2),
        "title": title,
        "detail": detail,
        "evidence": evidence,
        # Which document this is about, so a total can count each one once.
        # One invoice can trip several checks.
        "document": document,
    }


def _total_distinct(findings: list) -> float:
    """Add up the money at stake, counting each document once.

    A plain sum would count an invoice flagged by two checks (say overdue
    and oddly numbered) twice, and could report more money at stake than
    is owed in total. Findings with no document (the numbering gap) are
    added as they are.
    """
    seen = {}
    unkeyed = 0.0
    for f in findings:
        key = f.get("document")
        if key is None:
            unkeyed += f["amount"]
            continue
        seen[key] = max(seen.get(key, 0.0), f["amount"])
    return round(sum(seen.values()) + unkeyed, 2)


def _missing_contact_email(snapshot: dict) -> list:
    """Unpaid invoices whose contact has no email address in Xero, so the
    reminder cannot be sent until one is added. Only contacts that owe
    money are reported."""
    findings = []
    for row in snapshot.get("receivables") or []:
        if row.get("contact_email"):
            continue
        findings.append(_finding(
            kind="missing_contact_email",
            confidence="certain",
            amount=row.get("total"),
            title="%s has no email address in Xero" % (row.get("contact_name") or "A contact"),
            detail=(
                "Invoice %s is unpaid and there is no address on the contact "
                "record, so it cannot be chased by email until one is added."
                % (row.get("invoice_number") or "with no number")
            ),
            evidence={
                "invoice_number": row.get("invoice_number"),
                "contact_name": row.get("contact_name"),
                "due_date": row.get("due_date"),
            },
            document=row.get("invoice_number"),
        ))
    return findings


def _badly_overdue(snapshot: dict, as_of: date) -> list:
    """Receivables and bills at least OVERDUE_DAYS past their due date."""
    findings = []
    for row in snapshot.get("receivables") or []:
        days = _days_overdue(row.get("due_date"), as_of)
        if days is None or days < OVERDUE_DAYS:
            continue
        findings.append(_finding(
            kind="overdue_receivable",
            confidence="certain",
            amount=row.get("total"),
            title="%s is %d days overdue" % (row.get("invoice_number") or "An invoice", days),
            detail="%s owes this and the due date has passed by more than %d days."
                   % (row.get("contact_name") or "The customer", OVERDUE_DAYS),
            evidence={
                "invoice_number": row.get("invoice_number"),
                "contact_name": row.get("contact_name"),
                "due_date": row.get("due_date"),
                "days_overdue": days,
            },
            document=row.get("invoice_number"),
        ))
    for row in snapshot.get("payables") or []:
        days = _days_overdue(row.get("due_date"), as_of)
        if days is None or days < OVERDUE_DAYS:
            continue
        findings.append(_finding(
            kind="overdue_payable",
            confidence="certain",
            amount=row.get("total"),
            title="Owed to %s, %d days overdue" % (row.get("supplier_name") or "a supplier", days),
            detail="Bill %s has been due for more than %d days."
                   % (row.get("reference") or "with no reference", OVERDUE_DAYS),
            evidence={
                "reference": row.get("reference"),
                "supplier_name": row.get("supplier_name"),
                "due_date": row.get("due_date"),
                "days_overdue": days,
            },
            document="bill:%s" % (row.get("reference") or row.get("supplier_name")),
        ))
    return findings


def _settled_by_credit(snapshot: dict) -> list:
    """Invoices Xero calls paid that a credit note cancelled, with no money
    received. Usually the invoice was wrong and was reissued; if the
    reissue never happened, the work was never charged for."""
    findings = []
    for inv in snapshot.get("invoices") or []:
        if not inv.get("settled_by_credit"):
            continue
        findings.append(_finding(
            kind="settled_by_credit",
            confidence="likely",
            amount=inv.get("credited"),
            title="%s was credited in full, not paid" % (inv.get("invoice_number") or "An invoice"),
            detail=(
                "Xero shows this as paid, but no money came in. A credit note "
                "cancelled the whole amount. If it was reissued there should be "
                "a replacement invoice. If not, this work has not been charged for."
            ),
            evidence={
                "invoice_number": inv.get("invoice_number"),
                "contact_name": inv.get("contact_name"),
                "reference": inv.get("reference"),
                "issue_date": inv.get("issue_date"),
            },
            document=inv.get("invoice_number"),
        ))
    return findings


def _sequence_gaps(snapshot: dict) -> list:
    """Numbers missing between the lowest and highest invoice number.

    Reported as a lead, not a fact: a gap is normal when the number went to
    a draft or a voided invoice, and the snapshot does not include either.
    It is still worth a look, because a draft that was never sent is work
    that was never billed.
    """
    numbers = []
    prefixes = {}
    for inv in snapshot.get("invoices") or []:
        raw = inv.get("invoice_number")
        if not raw:
            continue
        match = INVOICE_NUMBER_PATTERN.match(raw.strip())
        if not match:
            continue
        value = int(match.group("number"))
        numbers.append(value)
        prefixes.setdefault(value, match.group("prefix").strip())

    if len(numbers) < 2:
        return []

    missing = sorted(set(range(min(numbers), max(numbers) + 1)) - set(numbers))
    if not missing:
        return []

    return [_finding(
        kind="invoice_number_gap",
        confidence="lead",
        amount=0,
        title="%d invoice number%s unaccounted for" % (
            len(missing), "" if len(missing) == 1 else "s"),
        detail=(
            "The numbers %s do not appear between %d and %d. Most likely they "
            "are drafts or voided invoices, which do not show up here. Worth a "
            "look, because a draft that was never sent is money never asked for."
            % (", ".join(str(m) for m in missing), min(numbers), max(numbers))
        ),
        evidence={"missing": missing, "first": min(numbers), "last": max(numbers)},
    )]


def _inconsistent_numbering(snapshot: dict) -> list:
    """Invoices whose prefix differs from the most common one (for example
    "SE INV-41370" among "INV-" invoices). Harmless in itself, but anything
    that matches invoices by number will miss it, so a real debt can drop
    out of a report."""
    seen = {}
    for inv in snapshot.get("invoices") or []:
        raw = inv.get("invoice_number")
        if not raw:
            continue
        match = INVOICE_NUMBER_PATTERN.match(raw.strip())
        if not match:
            continue
        seen.setdefault(match.group("prefix").strip(), []).append(inv)

    if len(seen) < 2:
        return []

    majority = max(seen, key=lambda p: len(seen[p]))
    findings = []
    for prefix, invs in seen.items():
        if prefix == majority:
            continue
        for inv in invs:
            findings.append(_finding(
                kind="odd_invoice_number",
                confidence="likely",
                amount=inv.get("due"),
                title="%s is numbered unlike the others" % inv.get("invoice_number"),
                detail=(
                    "Every other invoice starts with \"%s\". Anything that matches "
                    "invoices by number will skip this one, so it can quietly drop "
                    "out of a report while the money is still owed."
                    % (majority or "no prefix")
                ),
                evidence={
                    "invoice_number": inv.get("invoice_number"),
                    "contact_name": inv.get("contact_name"),
                    "expected_prefix": majority,
                },
                document=inv.get("invoice_number"),
            ))
    return findings


def find_gaps(snapshot: dict, as_of: Optional[date] = None) -> dict:
    """Run every check over a snapshot and return the findings."""
    today = as_of or date.today()

    findings = []
    findings += _missing_contact_email(snapshot)
    findings += _badly_overdue(snapshot, today)
    findings += _settled_by_credit(snapshot)
    findings += _sequence_gaps(snapshot)
    findings += _inconsistent_numbering(snapshot)

    # Largest amount first, so the finding that matters most is at the top.
    findings.sort(key=lambda f: f["amount"], reverse=True)

    return {
        "checked_at": today.isoformat(),
        "snapshot_captured_at": snapshot.get("captured_at"),
        "overdue_days_threshold": OVERDUE_DAYS,
        "count": len(findings),
        "total_amount_flagged": _total_distinct(findings),
        "findings": findings,
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", help="path to the snapshot written by xero_snapshot.py")
    parser.add_argument("--out", help="write the findings here as JSON")
    args = parser.parse_args()

    with open(args.snapshot, "r", encoding="utf-8") as handle:
        snapshot = json.load(handle)

    result = find_gaps(snapshot)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2)
            handle.write("\n")

    for f in result["findings"]:
        money = "${:,.2f}".format(f["amount"])
        print("[{:<7}] {:>12}  {}".format(f["confidence"], money, f["title"]))
    print("\n{} finding(s), {} attached".format(
        result["count"], "${:,.2f}".format(result["total_amount_flagged"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
