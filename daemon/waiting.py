"""
Waiting work: telling the operator's own late work apart from work that
is late because someone else has not replied.

What it does
    WaitingRouter.classify reads a task's title and description and
    answers one question: is this task blocked on somebody else, and if
    so, on whom?

      CLIENT     waiting on a client (an approval, a PO, a payment, a reply)
      SUPPLIER   waiting on a supplier (a lead time, a dispatch, freight)
      None       the operator's own work

    The overdue engine (daemon/overdue.py) moves waiting work to the
    Waiting on Client or Waiting on Supplier column instead of Overdue.
    Waiting work never climbs the escalation ladder. It gets a chase
    instead, because the only useful action is to prompt the other side.

How classify decides
    1. Is it waiting at all? Yes if the text contains an explicit waiting
       phrase (WAITING_PATTERNS: "awaiting", "no reply", "follow up", ...)
       or is about money owed to Splatt (INCOMING_PAYMENT_PATTERNS).
       Otherwise the answer is None.
    2. Money owed to Splatt is CLIENT, whatever else the text says.
    3. Otherwise supplier words (SUPPLIER_PATTERNS) mean SUPPLIER.
    4. Otherwise client words (CLIENT_PATTERNS) mean CLIENT.
    5. Waiting with neither side named defaults to CLIENT.

    Each pattern list can be replaced from settings under `waiting.*`.

Chase timing
    A client chase waits 5 business days and a supplier chase 3
    (`waiting.client_chase_business_days`,
    `waiting.supplier_chase_business_days`). They are separate columns
    because they run on separate clocks: a client chase is part of a
    relationship and is often better as a phone call, while a supplier
    chase is logistics and usually a one-line email.

    Each chase writes a "chase scheduled" line into the description.
    After `waiting.max_chases` (2) chases with no reply the task goes to
    Stalled as a decision for the operator, because a third identical
    email rarely gets an answer either.

Conservative on purpose
    A task wrongly classed as waiting never escalates, so real work could
    sit unnoticed in a waiting column. The detection therefore needs a
    genuine signal (an explicit waiting phrase, or a payment question
    aimed outward), and anything ambiguous is treated as the operator's
    own work, where the ladder keeps it visible. Misreading a wait as own
    work only surfaces it early; misreading own work as a wait hides it.
"""

from __future__ import annotations

import re

CLIENT = "waiting_client"
SUPPLIER = "waiting_supplier"

#: Written into the description each time a chase is booked, and counted
#: to decide when to stop chasing. Used only to count chases, never to
#: work out a task's age.
CHASE_MARKER = "chase scheduled"

# Phrases that mean somebody else owes the next move. They describe the
# state of the conversation rather than its subject, because a word like
# "delivery" is as common in work not yet started as in work waiting on
# someone else.
WAITING_PATTERNS = [
    r"\bawait(?:ing)?\b",
    r"\bwaiting (?:on|for)\b",
    r"\bno reply\b",
    r"\bnot (?:yet )?repl(?:y|ied)\b",
    r"\bchase\b",
    r"\bfollow(?:ing)?[ -]up\b",
    r"\bhas(?:n't| not) (?:come back|responded|replied|confirmed)\b",
    r"\bcheck if\b",
    r"\bstill (?:waiting|outstanding|open)\b",
]

# Money owed TO Splatt. Checked before anything else, and it decides the
# answer.
#
# Splatt recharges freight and import duty to clients, so a task such as
# "Check Kauri Springs paid INV-41352 (freight & duty)" is full of
# supplier words while really being about collecting a client's payment.
# The rule is about direction, not subject: if the question is whether
# somebody has paid Splatt, the task is waiting on a client, whatever the
# money was for.
#
# The gaps are [^\n] rather than [^.\n] because these tasks often contain
# amounts, and "payment for INV-41363 ($1,250.40) has arrived" has a full
# stop inside the amount.
INCOMING_PAYMENT_PATTERNS = [
    # Splatt's own Xero invoices are numbered INV-NNNNN. One named next to
    # a payment word is money owed to Splatt.
    r"\bINV-\d[^\n]{0,70}\b(?:paid|payment|unpaid|outstanding|owing)\b",
    r"\b(?:paid|payment|unpaid|outstanding|owing)\b[^\n]{0,70}\bINV-\d",
    # "Check if Kauri Springs paid", "chase Kyle re payment"
    r"\b(?:check|chase|confirm|ask)\b[^\n]{0,60}\b(?:paid|payment)\b",
    # "Check if payment for INV-41369 has arrived"
    r"\bpayment\b[^\n]{0,70}\b(?:arrived|received|come (?:in|through)|landed|cleared)\b",
    r"\bremittance\b",
    r"\baccounts receivable\b",
]

# Which side of the desk. Only consulted once a task already reads as
# waiting, so these can be broad without causing false positives.
#
# A bare "PO" is deliberately not here. Splatt issues purchase orders to
# suppliers and receives them from clients, so the abbreviation points
# both ways, and a client task can be full of the client's own PO
# numbers. The spelled-out "purchase order" is kept, because in the
# operator's wording it almost always means one Splatt is placing.
#
# The names of the freight forwarder and courier the business uses
# (Coastline and QXP here) are supplier signals too.
SUPPLIER_PATTERNS = [
    r"\bsupplier\b", r"\bvendor\b", r"\bmanufacturer\b",
    r"\bpurchase order\b", r"\blead time\b", r"\bETA\b",
    r"\bdispatch\b", r"\bshipment\b", r"\bfreight\b", r"\bcustoms\b",
    r"\bimport\b", r"\bcoastline\b", r"\bqxp\b",
    r"\bspare part\b", r"\brestock\b", r"\bback ?order\b",
]

CLIENT_PATTERNS = [
    r"\bclient\b", r"\bcustomer\b",
    r"\bquote\b", r"\bQU-\d", r"\bquotation\b",
    r"\bapproval\b", r"\bsign ?off\b", r"\bpurchase\b",
    r"\binvoice\b", r"\bINV-\d", r"\bpayment\b", r"\bpaid\b",
    r"\bproposal\b", r"\border confirmation\b",
]


def _compile(patterns):
    return re.compile("|".join(patterns), re.IGNORECASE)


class WaitingRouter:
    """Decides whether a task is blocked on somebody else, and on whom.

    Also holds the chase settings: business days to wait for each side,
    and how many chases are allowed before a task is handed over.
    """

    def __init__(self, settings=None):
        get = (settings.get if settings else lambda key, default=None: default)
        self.waiting_re = _compile(get("waiting.patterns", None) or WAITING_PATTERNS)
        self.incoming_re = _compile(
            get("waiting.incoming_payment_patterns", None)
            or INCOMING_PAYMENT_PATTERNS
        )
        self.supplier_re = _compile(
            get("waiting.supplier_patterns", None) or SUPPLIER_PATTERNS
        )
        self.client_re = _compile(
            get("waiting.client_patterns", None) or CLIENT_PATTERNS
        )
        self.client_days = int(get("waiting.client_chase_business_days", 5) or 5)
        self.supplier_days = int(get("waiting.supplier_chase_business_days", 3) or 3)
        self.max_chases = int(get("waiting.max_chases", 2) or 2)

    @staticmethod
    def _text(task):
        return "%s\n%s" % (
            (task or {}).get("content") or "",
            (task or {}).get("description") or "",
        )

    def is_money_owed(self, task):
        """True when the task is about money coming in to Splatt.

        Public because tools/tick_off.py needs the same answer. An email
        that mentions an invoice number is not proof the invoice was paid,
        so a task asking about a payment is never ticked off from mail
        alone, and sharing this method keeps one pattern list.
        """
        return bool(self.incoming_re.search(self._text(task)))

    def is_waiting(self, task):
        """True when the next move is somebody else's.

        Money owed to Splatt counts on its own, without one of the general
        waiting words. "Check Clearwater Bottling paid INV-41370" never
        says wait, chase or follow up, but the only thing to do is wait
        for the payment.
        """
        return bool(self.waiting_re.search(self._text(task))
                    or self.is_money_owed(task))

    def classify(self, task):
        """CLIENT, SUPPLIER, or None when the task is the operator's own work.

        Money owed to Splatt is tested first and wins outright, because
        recharged freight and duty make a payment task read like a
        supplier task.

        Supplier is tested before client. A supplier thread about a
        client's machine mentions the client constantly, so testing client
        first would put most supplier chases in the wrong column. The
        reverse is rarer, because a client rarely has a lead time.
        """
        if not self.is_waiting(task):
            return None
        text = self._text(task)
        if self.incoming_re.search(text):
            return CLIENT
        if self.supplier_re.search(text):
            return SUPPLIER
        if self.client_re.search(text):
            return CLIENT
        # Reads as waiting but names nobody. Client is the safer default:
        # its chase interval is the longer of the two, and chasing a
        # client too early costs more than chasing a supplier too early.
        return CLIENT

    def chase_days(self, kind):
        """Business days to wait before a chase for this side."""
        return self.supplier_days if kind == SUPPLIER else self.client_days

    def chase_count(self, task):
        """How many chases have been booked, counted from the "chase
        scheduled" lines in the description."""
        description = (task or {}).get("description") or ""
        return len(re.findall(CHASE_MARKER, description, re.IGNORECASE))

    def exhausted(self, task):
        """True when every allowed chase has been used, so the task needs
        the operator rather than another email."""
        return self.chase_count(task) >= self.max_chases
