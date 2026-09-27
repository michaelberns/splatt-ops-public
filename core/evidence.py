"""
Email evidence: decide whether an email proves that a Todoist task is finished.

Inputs
    One Todoist task (`content`, `description`, `added_at`), a list of
    `Email` objects fetched by something that has mailbox access, and two
    answers from `daemon.waiting.WaitingRouter`: whether the task is
    waiting on somebody else (`waiting_kind`) and whether it is about
    money (`money_owed`).

Output
    A `Finding` carrying one of four verdicts, a sentence saying why, and
    the single email the verdict rests on, so a report can show the proof
    instead of asking the operator to trust it:

      DONE     the email proves the task is finished. This is the only
               verdict `tools/tick_off.py --write` acts on.
      CHASED   the operator has followed up and nobody has replied yet.
      MAYBE    an email looks related, and the operator decides.
      UNKNOWN  nothing in the mail says anything about this task.

Rules
    1. Only a reference ties an email to a task. A reference is a quote,
       invoice or PO number (QU-7994, INV-41352, PO-845102). Names are
       not used: one client can have many open tasks, and an email naming
       the client would match all of them equally well.
    2. The reference must be in the task title. The notes carry history
       and other jobs, sometimes precisely to say what the task is not
       ("separate from the panel job QU-8460, which is done"). A match
       found only in the notes is reported as MAYBE and never closes the
       task.
    3. Only mail dated on or after the task's creation counts, so the
       email that caused a task cannot also close it.
    4. Direction matters. For a task waiting on somebody else, the proof
       is an email arriving; an email going out is only a chase. For the
       operator's own work, the proof is an email going out, because for
       most of these tasks sending the email is the work.
    5. Money is never closed by email. An email mentioning INV-41352 is as
       likely to be the client querying the invoice as paying it. Xero
       knows whether money moved and the mailbox does not, so money tasks
       are always MAYBE, however strong the match.

Why so strict
    A task wrongly left open costs the operator a few seconds of reading.
    A task wrongly closed disappears from the board, and the first sign
    is a client asking why nothing happened. Each rule gives up some real
    completions in exchange for never making a wrong one.
"""

from __future__ import annotations

import re
from datetime import date, datetime

#: The task is finished, and `tools/tick_off.py --write` may tick it off.
DONE = "done"
#: The operator has chased and nobody has replied. Reported, never ticked off.
CHASED = "chased"
#: Something looks related. The operator decides. Never ticked off.
MAYBE = "maybe"
#: Nothing in the mail says anything about this task.
UNKNOWN = "unknown"

#: The only verdict `--write` acts on.
ACTIONABLE = (DONE,)

# What counts as a reference. All three are numbers printed on a
# document, so a task and an email about the same job carry the same one.
#
#   QU   Splatt quote
#   INV  Splatt invoice
#   PO   purchase order, in either direction (the number alone is enough)
#
# The prefix is kept in the normalised form. Without it, quote 7994 and
# PO 7994 would be one reference, and an email about the purchase order
# would tick off the task about the quote.
REFERENCE_KINDS = ("QU", "INV", "PO")

# The separator is optional and case is ignored, because "QU-7994",
# "QU 7994" and "qu7994" are one number written three ways. Three digits
# is the minimum: a shorter number in a sentence is a quantity, not a
# reference.
_REFERENCE_RE = re.compile(
    "|".join(r"\b(%s)[-\s]?(\d{3,8})\b" % kind for kind in REFERENCE_KINDS),
    re.IGNORECASE,
)

SENT = "sent"
RECEIVED = "received"


def references(text):
    """Every quote, invoice and PO number in a piece of text.

    Returned in one normalised spelling, upper case with no separator,
    in order of first appearance and without repeats, so a task and an
    email can be compared directly.

    >>> references("Chase qu 7994 and PO-845102, re QU-7994")
    ['QU7994', 'PO845102']
    """
    found = []
    for match in _REFERENCE_RE.finditer(str(text or "")):
        kind, number = [g for g in match.groups() if g][:2]
        ref = "%s%s" % (kind.upper(), number)
        if ref not in found:
            found.append(ref)
    return found


def _as_date(value):
    """A date out of whatever the fetcher put in the field.

    Emails arrive with full timestamps and tasks with bare dates, and
    Python refuses to compare a datetime with a date, so everything is
    flattened to a plain date here. Returns None when nothing parses.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    for cut in (len(text), 19, 10):
        try:
            return datetime.fromisoformat(text[:cut]).date()
        except ValueError:
            continue
    return None


class Email:
    """One message, in the only shape this module reads.

    Plain data, not a Gmail API object. This repo holds no mail
    credentials: the agent, which does have mailbox access, fetches the
    messages and writes them to a JSON file (state/evidence.json by
    default) that tools/tick_off.py reads. `from_dict` accepts one row of
    that file. Keeping it plain also means every rule here can be tested
    with a few dicts.

    `direction` is "sent" or "received". `references` is computed from
    the subject and snippet when the object is created.
    """

    def __init__(self, message_id="", direction="", when=None, subject="",
                 snippet="", who="", link=""):
        self.message_id = str(message_id or "")
        self.direction = str(direction or "").strip().lower()
        self.date = _as_date(when)
        self.subject = str(subject or "")
        self.snippet = str(snippet or "")
        self.who = str(who or "")
        self.link = str(link or "")
        self.references = references("%s\n%s" % (self.subject, self.snippet))

    @classmethod
    def from_dict(cls, row):
        row = dict(row or {})
        return cls(
            message_id=row.get("id") or row.get("message_id") or "",
            direction=row.get("direction") or "",
            when=row.get("date") or row.get("when") or "",
            subject=row.get("subject") or "",
            snippet=row.get("snippet") or row.get("body") or "",
            who=row.get("from") or row.get("who") or row.get("to") or "",
            link=row.get("link") or row.get("url") or "",
        )

    def describe(self):
        parts = [self.date.isoformat() if self.date else "no date"]
        parts.append("to %s" % self.who if self.direction == SENT else
                     "from %s" % self.who)
        parts.append('"%s"' % self.subject[:70])
        return " ".join(p for p in parts if p)

    def __repr__(self):
        return "Email(%s, %s)" % (self.direction, self.subject[:40])


class Finding:
    """The answer for one task: a verdict, the reason, and the proof.

    `email` is the single email the verdict rests on (None for UNKNOWN)
    and `reference` is the normalised number that tied it to the task.
    """

    def __init__(self, verdict, why, email=None, reference=""):
        self.verdict = verdict
        self.why = why
        self.email = email
        self.reference = reference

    @property
    def actionable(self):
        return self.verdict in ACTIONABLE

    def proof(self):
        return self.email.describe() if self.email else ""

    def __repr__(self):
        return "Finding(%s, %s)" % (self.verdict, self.why)


def _window_start(task, real_due=None):
    """The earliest an email can be and still be about this task.

    The task's creation date, falling back to the real due date only if
    the task has no creation date. Creation rather than due date, because
    much of this work is done before it is due, and the point of the run
    is to find work that is already finished.

    Starting at creation means a newly created task has a short window
    and little mail qualifies. That is the intended trade-off. Starting
    earlier would let the email that caused the task (for example the
    client's request) close it on the day it was created.
    """
    added = _as_date((task or {}).get("added_at"))
    if added:
        return added
    return real_due


def _in_window(email, since):
    if not email.date:
        return False
    return since is None or email.date >= since


def look(task, emails, waiting_kind=None, money_owed=False, real_due=None):
    """Decide whether one task is finished. A pure function: no network, no I/O.

    `waiting_kind` is what `daemon.waiting.WaitingRouter.classify` said
    (a client or supplier wait), and None means the task is the
    operator's own work. `money_owed` is that router's money test, and
    when it is true nothing here can return DONE. `real_due` is used as
    the window start only for a task with no creation date.

    Order of checks: no reference -> UNKNOWN; no matching email in the
    window -> UNKNOWN; money -> MAYBE; waiting and a reply arrived ->
    DONE; waiting and only a chase went out -> CHASED; own work and an
    email went out -> DONE; own work and only mail arrived -> MAYBE.
    A DONE is downgraded to MAYBE when its reference is only in the notes.
    """
    title = (task or {}).get("content") or ""
    notes = (task or {}).get("description") or ""
    titled = references(title)
    wanted = references("%s\n%s" % (title, notes))
    if not wanted:
        return Finding(
            UNKNOWN,
            "the task names no quote, invoice or PO number, so no email can "
            "be tied to it with any confidence",
        )

    since = _window_start(task, real_due)
    matches = [
        (email, ref)
        for email in emails
        for ref in wanted
        if ref in email.references and _in_window(email, since)
    ]
    if not matches:
        return Finding(
            UNKNOWN,
            "nothing in the mail mentions %s since %s"
            % (", ".join(wanted), since.isoformat() if since else "ever"),
        )

    # Title references sort ahead of notes references (see rule 2 in the
    # module docstring and `_from_notes`). Within each group the newest
    # email wins, because the last message in a thread says where it
    # ended up.
    matches.sort(key=lambda pair: (pair[1] in titled, pair[0].date),
                 reverse=True)
    inbound = [pair for pair in matches if pair[0].direction == RECEIVED]
    outbound = [pair for pair in matches if pair[0].direction == SENT]

    if money_owed:
        email, ref = matches[0]
        # The sentence does not say which way the money goes. The router's
        # money test matches both "check if they paid INV-41352" (money
        # in) and "pay Greenlight INV-9184" (money out). The refusal is
        # right for both, and a direction-neutral sentence is true for
        # both.
        why = ("this task is about money. An email mentioning %s is not proof "
               "the money moved, and Xero is. Check there, not here." % ref)
        if ref not in titled:
            why += (" On top of that, %s is only in the notes here, not the "
                    "title, so the email below may be a different job." % ref)
        return Finding(MAYBE, why, email, ref)

    if waiting_kind:
        if inbound:
            email, ref = inbound[0]
            if ref not in titled:
                return _from_notes(email, ref, title)
            return Finding(
                DONE,
                "they came back on %s about %s, so the wait is over"
                % (email.date.isoformat(), ref),
                email, ref,
            )
        email, ref = outbound[0]
        return Finding(
            CHASED,
            "you chased on %s about %s and nobody has replied yet"
            % (email.date.isoformat(), ref),
            email, ref,
        )

    if outbound:
        email, ref = outbound[0]
        if ref not in titled:
            return _from_notes(email, ref, title)
        return Finding(
            DONE,
            "you sent it on %s, quoting %s" % (email.date.isoformat(), ref),
            email, ref,
        )

    email, ref = inbound[0]
    return Finding(
        MAYBE,
        "%s came up in mail from %s on %s, but nothing went out, so this may "
        "not be done" % (ref, email.who or "them", email.date.isoformat()),
        email, ref,
    )


def _from_notes(email, ref, title):
    """The only number that matched is one the title never mentions.

    Example: the task "Bright Fizz / Aaron, reach out re chain work" has
    a note reading "separate from the Wanda touch panel job
    (QU-8460/QU-8461), which is done and paid". An email about the panel
    job matches QU-8460, but the chain job has not started, so closing it
    would be wrong.

    The result is MAYBE rather than nothing, because sometimes the notes
    really are where the job's number lives, and the operator can see
    that at a glance.
    """
    return Finding(
        MAYBE,
        "%s is only in the notes on this task, not in its title, so this "
        "email is probably about a different job. Your call." % ref,
        email, ref,
    )
