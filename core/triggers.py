"""
Trigger engine: completing a task moves its job (project) to the next status.

Terms
    Trigger line   a line in a task's description header, for example
                       🔁 On complete: job to quoted
                   naming the status the task's job moves to when the task
                   is completed. It is mirrored into the assignment field
                   `on_complete_job_status`, and PocketBase wins if the two
                   disagree (see `mismatch`).
    Auto line      "🔁 Auto: quoted_chase for job <id>", written on a task
                   this engine created. The assignment field `auto_kind`
                   carries the same fact.
    Header         the lines at the top of a description that this system
                   owns (pin line, project line, estimate, trigger lines),
                   as defined by core/realdue.py.
    Forward line   the order a job's status moves in:
                   quoting -> quoted -> won -> invoicing -> invoiced.

Inputs
    One snapshot (`seen`) of assignments, jobs, clients, contacts, quotes,
    invoices, task groups, the live Todoist tasks, and the source keys of
    interactions this engine has already written. Settings under
    `triggers.*` override the defaults in `Rules`.

Output
    `plan()` returns the actions for each rule ("r0" to "r3"), the cases
    that need a human decision ("needs_decision"), and the cases left
    alone with a reason ("skipped"). daemon/triggers.py applies the plan
    and writes one interaction per action, keyed by `source_id`, so a
    second run over the same board does nothing.

The four rules, applied in this order on every pass
    R0  Assign missing triggers. A task whose header already has a trigger
        line gets it mirrored into PocketBase (the normal case: the agent
        writes the line when it creates the task). A task whose title
        matches a strict pattern for its job's current status gets both
        the line and the field. Every addition is reported, so a wrong
        pattern is visible and can be removed.
    R1  Fire on completion. A completed task with a trigger moves its job.
        Forward only, never onto a job already at or past the target,
        never on a sensitive account, and never to "invoiced" without an
        invoice record carrying a number (the validator's gate_invoiced
        would block it).
    R2  Generate the chase, once. A job that has been "quoted" for seven
        days (`triggers.quoted_chase_after_days`) gets exactly one chase
        task, booked into a free slot by core/planner.py. From then on it
        is an ordinary task owned by the waiting engine (daemon/waiting.py).
        This module never moves it and never creates a second one.
    R3  Clean up. When a job leaves "quoted" while its chase task is still
        open, the chase is completed and archived with a comment saying
        why. It is never deleted.

Two deliberate limits
    Only tasks carrying `auto_kind` are ever closed by the engine. The
    overdue engine never completes or deletes a task, and R3 is a single,
    named exception to that rule. A task the operator wrote with the same
    title is never touched.

    A job link is never invented to make a trigger fire. A completed task
    with a trigger but no project goes to needs_decision, because a wrong
    guess would silently move a real job.

Sensitive accounts
    A client with `critical` set in PocketBase is reported under
    needs_decision by every rule and never acted on.

Everything here is a pure function: no network, no database. The same
split as core/sync.py and core/reverse.py.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

from core import planner as planner_mod
from core import realdue

#: The marker that starts every trigger and auto line. Distinct from the
#: pin (📌), the estimate (⏱) and the project line (📁).
MARK = "\U0001f501"

#: The statuses a trigger is allowed to move a job to. Nothing else is
#: reachable from a task, however anybody spells the line.
TARGETS = ("quoted", "won", "invoicing", "invoiced")

#: The forward line, in order. A move is allowed only when the target
#: ranks higher on this list than the job's current status.
FORWARD_ORDER = ("quoting", "quoted", "won", "invoicing", "invoiced")

#: Statuses past the end of the forward line. A job in one of these is
#: beyond every target, so a trigger has nothing left to do.
PAST_THE_LINE = ("paid", "completed")

QUOTED_CHASE = "quoted_chase"
AUTO_KINDS = (QUOTED_CHASE,)

PB_STATUS_FIELD = "on_complete_job_status"
PB_AUTO_FIELD = "auto_kind"
JOB_CHANGED_AT = "status_changed_at"
JOB_CHANGED_BY = "status_changed_by"

#: The source every interaction this engine writes is keyed on. It is
#: listed in core.interactions.KNOWN_SOURCES, so a re-run finds the
#: record it wrote last time instead of writing a second one.
SOURCE = "trigger"

#: The estimate written on a chase task. A chase is a one-line email.
CHASE_MINUTES = 15

ON_COMPLETE_RE = re.compile(
    r"^\s*%s\s*On complete:\s*job to\s*(quoted|won|invoicing|invoiced)\s*$"
    % re.escape(MARK),
    re.IGNORECASE,
)

AUTO_RE = re.compile(
    r"^\s*%s\s*Auto:\s*([a-z_]+)\s+for job\s+(\S+)\s*$" % re.escape(MARK),
    re.IGNORECASE,
)

QUOTE_NUMBER_RE = re.compile(r"\bQU-?\s?(\d{3,5})\b", re.IGNORECASE)

PROJECT_LINE_RE = re.compile(r"^\s*\U0001f4c1\s*Project:\s*(.+?)\s*$")

#: The strict title patterns R0 uses, and the status a job has to be in
#: for each one to mean anything. Overridable from settings under
#: triggers.title_patterns, and kept here as well so the module works in
#: a test with no settings file.
#:
#: Strict on purpose. R0 writes a trigger that will later move a real
#: job, so a loose pattern would eventually set a wrong status. Anything
#: these miss is added by the agent when it creates the task, which is
#: the normal path.
DEFAULT_TITLE_PATTERNS = {
    "quoted": {
        "when_status": "quoting",
        "patterns": [r"Send (the )?quote", r"Send .* quote"],
    },
    "won": {
        "when_status": "quoted",
        "patterns": [r"Confirm (the )?PO", r"Receive (the )?PO",
                     r"Confirm (the )?order"],
    },
    "invoicing": {
        "when_status": "won",
        "patterns": [r"Raise (the )?invoice", r"Invoice .* for"],
    },
    "invoiced": {
        "when_status": "invoicing",
        "patterns": [r"Send (the )?invoice"],
    },
}


# ---------------------------------------------------------------- lines
def render_on_complete(status):
    """The trigger line for a task whose completion moves its job.

    >>> render_on_complete("quoted")
    '\U0001f501 On complete: job to quoted'
    """
    return "%s On complete: job to %s" % (MARK, status)


def render_auto(kind, job_id):
    """The line that says the engine made this task, not a person."""
    return "%s Auto: %s for job %s" % (MARK, kind, job_id)


def parse_on_complete(description):
    """The status this task moves its job to, or an empty string.

    Read from the header only (the same lines core/realdue.py treats as
    the header), so a quoted email in the body cannot be mistaken for an
    instruction. Only the four TARGETS match. Two trigger lines in one
    header are not a tie to break: nothing is returned, and the validator
    reports the task.
    """
    found = []
    for line in _header_lines(description):
        match = ON_COMPLETE_RE.match(line)
        if match:
            found.append(match.group(1).lower())
    if len(found) != 1:
        return ""
    return found[0]


def on_complete_count(description):
    """How many trigger lines the header carries. More than one is damage."""
    return sum(1 for line in _header_lines(description)
               if ON_COMPLETE_RE.match(line))


def parse_auto(description):
    """(kind, job id) for an engine created task, or (None, None)."""
    for line in _header_lines(description):
        match = AUTO_RE.match(line)
        if match:
            return match.group(1).lower(), match.group(2)
    return None, None


def project_path(description):
    """The project path off the task's own project line, or an empty string."""
    for line in _header_lines(description):
        match = PROJECT_LINE_RE.match(line)
        if match:
            return match.group(1).strip()
    return ""


def _header_lines(description):
    """The leading run of lines this system owns.

    Uses core/realdue.py's `header_end`, so both modules always agree
    on where the header stops and the body starts.
    """
    lines = (description or "").splitlines()
    return lines[:realdue.header_end(description)] if lines else []


def place(description, line):
    """The description with `line` in the header, below the pin.

    New lines go at the bottom of the header, so they never push the pin
    or the project line to a different position. A description with no
    header gets the line at the top, followed by a blank line.

    Adding a line that is already in the header is a no-op, so a pass
    that runs twice does not stack them up.
    """
    description = description or ""
    wanted = line.strip()
    if any(existing.strip() == wanted for existing in _header_lines(description)):
        return description
    if not description.strip():
        return wanted

    lines = description.splitlines()
    at = realdue.header_end(description)
    if at == 0:
        return "%s\n\n%s" % (wanted, description.lstrip("\n"))
    head, rest = lines[:at], lines[at:]
    while head and not head[-1].strip():
        head.pop()
    while rest and not rest[0].strip():
        rest.pop(0)
    if not rest:
        return "\n".join(head + [wanted])
    return "\n".join(head + [wanted, ""] + rest)


def mismatch(description, stored):
    """Where the line and the stored field disagree, describe it.

    PocketBase wins, as it does for the real due date and for the same
    reason: a Todoist description is the copy a person can overwrite by
    accident. The disagreement is reported rather than repaired, so the
    operator sees it. Returns None when they agree.
    """
    on_line = parse_on_complete(description)
    held = str(stored or "").strip().lower()
    if not on_line and not held:
        return None
    if on_line == held:
        return None
    return {
        "line": on_line or "none",
        "pocketbase": held or "none",
        "winner": held or "none",
    }


# ---------------------------------------------------------------- rules
class Rules:
    """The settings the four rules read, with the defaults in one place.

    Reads `triggers.quoted_chase_after_days`, `triggers.forward_order`
    and `triggers.title_patterns`, falling back to the module defaults
    when there is no settings object (as in tests).
    """

    def __init__(self, settings=None):
        get = (settings.get if settings else lambda key, default=None: default)
        self.settings = settings
        self.chase_after_days = int(
            get("triggers.quoted_chase_after_days", 7) or 7
        )
        order = get("triggers.forward_order", None) or FORWARD_ORDER
        self.forward_order = tuple(str(s).lower() for s in order)
        raw = get("triggers.title_patterns", None) or DEFAULT_TITLE_PATTERNS
        self.title_patterns = {}
        for target in TARGETS:
            spec = raw.get(target) or {}
            patterns = spec.get("patterns") or []
            if not patterns:
                continue
            self.title_patterns[target] = {
                "when_status": str(spec.get("when_status") or "").lower(),
                "matchers": [re.compile(p, re.IGNORECASE) for p in patterns],
                "source": [str(p) for p in patterns],
            }

    def rank(self, status):
        """Where a status sits on the forward line, or None if it is off it."""
        try:
            return self.forward_order.index(str(status or "").lower())
        except ValueError:
            return None

    def match_title(self, content, current_status):
        """The target a title implies for a job in this status, or None.

        Returns (target, pattern) or (None, ""). The title pattern and the
        job's current status both have to agree: "Send the quote" on a job
        that is already won says nothing, because the quote went out long
        ago and the task is about something else.
        """
        current = str(current_status or "").lower()
        text = str(content or "")
        for target in TARGETS:
            spec = self.title_patterns.get(target)
            if not spec or spec["when_status"] != current:
                continue
            for number, matcher in enumerate(spec["matchers"]):
                if matcher.search(text):
                    return target, spec["source"][number]
        return None, ""


# ------------------------------------------------------------ the plan
def source_id(rule_name, *parts):
    """The stable id for the interaction one action writes.

    Built from the rule and the record ids and nothing else, so a second
    run over the same board recognises its own work and writes nothing.
    A timestamp in the id would make every pass look like a new event and
    write a duplicate interaction.

    >>> source_id("r1", "a1", "quoted")
    'r1:a1:quoted'
    """
    return ":".join([rule_name] + [str(part) for part in parts])


def _as_date(value):
    """Any of PocketBase's or Todoist's date shapes, or None."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip().replace("Z", "").replace("T", " ").strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _open(record):
    return not record.get("archived") and record.get("status") != "completed"


def _text(value):
    return str(value or "").strip()


def quote_number(quote):
    """The QU number out of a quote's title, or an empty string.

    The quotes collection has no number field. The number is in the
    title (or notes) when it is known at all, and nothing is invented
    when it is not, because a made-up quote number in a chase email to a
    client is worse than none.
    """
    for key in ("title", "notes"):
        match = QUOTE_NUMBER_RE.search(_text(quote.get(key)))
        if match:
            return "QU-" + match.group(1)
    return ""


class Board:
    """One consistent picture of everything the rules read.

    Built once per pass from records that were all fetched before any
    write, so no rule can see half of an update another rule made.
    """

    def __init__(self, seen):
        self.assignments = list(seen.get("assignments") or [])
        self.jobs = list(seen.get("jobs") or [])
        self.clients = list(seen.get("clients") or [])
        self.contacts = list(seen.get("contacts") or [])
        self.quotes = list(seen.get("quotes") or [])
        self.invoices = list(seen.get("invoices") or [])
        self.task_groups = list(seen.get("task_groups") or [])
        self.tasks = list(seen.get("tasks") or [])
        self.interaction_keys = set(seen.get("interaction_keys") or ())

        self.jobs_by_id = {j.get("id"): j for j in self.jobs if j.get("id")}
        self.clients_by_id = {c.get("id"): c for c in self.clients if c.get("id")}
        self.tasks_by_id = {str(t.get("id")): t for t in self.tasks if t.get("id")}
        self.critical = {
            c.get("id") for c in self.clients if c.get("critical")
        }

    # what a job is called, for a message a person reads
    def job_title(self, job_id):
        job = self.jobs_by_id.get(job_id) or {}
        return _text(job.get("title")) or str(job_id)

    def client_of(self, job):
        return _text((job or {}).get("client"))

    def client_name(self, client_id):
        return _text((self.clients_by_id.get(client_id) or {}).get("name"))

    def is_sensitive(self, job):
        """A client marked critical in PocketBase. Reported, never acted on.

        Read from the client record rather than matched on a name, so the
        operator changes the list of sensitive accounts in the data, not
        in code.
        """
        return self.client_of(job) in self.critical

    def contact_name(self, client_id):
        for contact in self.contacts:
            if _text(contact.get("client")) == _text(client_id):
                name = _text(contact.get("name"))
                if name:
                    return name
        return ""

    def description_of(self, record):
        """The live description if Todoist has one, else the mirror."""
        task = self.tasks_by_id.get(_text(record.get("todoist_id")))
        if task is not None:
            return task.get("description") or ""
        return record.get("description") or ""

    def is_live(self, record):
        return _text(record.get("todoist_id")) in self.tasks_by_id

    def quotes_for(self, job_id):
        return [q for q in self.quotes
                if _text(q.get("job")) == _text(job_id) and not q.get("archived")]

    def has_invoice(self, job_id):
        for invoice in self.invoices:
            if _text(invoice.get("job")) != _text(job_id):
                continue
            if invoice.get("archived"):
                continue
            if _text(invoice.get("invoice_number")):
                return True
        return False

    def chase_for(self, job_id):
        """Any chase task for this job, in any state, archived included.

        Any state on purpose: one chase per job, ever. A completed chase
        shows the chase happened, so recreating it would ask the operator
        to do the same thing twice.
        """
        for record in self.assignments:
            if _text(record.get(PB_AUTO_FIELD)) != QUOTED_CHASE:
                continue
            if _text(record.get("job")) == _text(job_id):
                return record
        return None

    def group_path(self, job_id):
        for group in self.task_groups:
            if _text(group.get("job")) == _text(job_id):
                path = _text(group.get("project_path"))
                if path:
                    return path
        return ""


def _decision(rule_name, what, why, **extra):
    row = {"rule": rule_name, "what": what, "why": why}
    row.update(extra)
    return row


def _skip(rule_name, what, why):
    return {"rule": rule_name, "what": what, "why": why}


def assign_missing(board, rules):
    """R0. Give a task the trigger it should already have had.

    Only open tasks with a job link and no stored trigger are considered.
    Two sources, in this order:

      The trigger line is already in the header and the field is empty.
      The agent writes the line when it creates the task and nothing else
      writes the field, so this is the normal path: a mirror, not a guess.

      The title matches a strict pattern for the job's current status.
      This is the guess. It is narrow, and every one is reported so the
      operator can remove it if the pattern was wrong.

    Never links a job to make a trigger possible: a task with no project
    is left exactly as it is. `write_line` is True only when the line has
    to be added to a task that is still live in Todoist.
    """
    actions, decisions, skipped = [], [], []
    for record in board.assignments:
        if not _open(record):
            continue
        if _text(record.get(PB_STATUS_FIELD)):
            continue
        job_id = _text(record.get("job"))
        if not job_id:
            continue
        job = board.jobs_by_id.get(job_id)
        if job is None or job.get("archived"):
            continue

        content = _text(record.get("content"))
        description = board.description_of(record)
        target = parse_on_complete(description)
        why = "the line was already on the task"
        pattern = ""
        if not target:
            target, pattern = rules.match_title(content, job.get("status"))
            if not target:
                continue
            why = "the title matches %r for a job in %s" % (
                pattern, _text(job.get("status")) or "no status")

        if board.is_sensitive(job):
            decisions.append(_decision(
                "r0", content,
                "sensitive account, nothing was changed. The trigger it "
                "would have been given is %s." % target,
                assignment=record.get("id"), job=job_id))
            continue

        key = source_id("r0", record.get("id"), target)
        if key in board.interaction_keys:
            skipped.append(_skip("r0", content, "already added on an earlier pass"))
            continue

        line = render_on_complete(target)
        needs_line = not parse_on_complete(description)
        actions.append({
            "rule": "r0",
            "assignment": record.get("id"),
            "todoist_id": _text(record.get("todoist_id")),
            "content": content,
            "status": target,
            "job": job_id,
            "client": board.client_of(job),
            "line": line,
            "write_line": bool(needs_line and board.is_live(record)),
            "description_after": place(description, line) if needs_line else description,
            "source_id": key,
            "subject": "Trigger added to task: %s (to %s)" % (content, target),
            "summary": (
                "Task %r (Todoist %s) had no trigger recorded, so one was "
                "added: completing it moves the project to %s. Added because "
                "%s."
                % (content, _text(record.get("todoist_id")) or "none",
                   target, why)
            ),
        })
    return actions, decisions, skipped


def fire_completions(board, rules):
    """R1. A completed task moves its job.

    Each guard below is a case where moving the job would be wrong. Each
    one records why, in needs_decision (a person should look) or skipped
    (nothing to do), so no refusal is silent.
    """
    actions, decisions, skipped = [], [], []
    for record in board.assignments:
        target = _text(record.get(PB_STATUS_FIELD)).lower()
        if target not in TARGETS:
            continue
        if record.get("status") != "completed":
            continue

        content = _text(record.get("content"))
        job_id = _text(record.get("job"))
        if not job_id:
            decisions.append(_decision(
                "r1", content,
                "the task is done and says the project moves to %s, but it is "
                "not linked to a project. Nothing was guessed." % target,
                assignment=record.get("id")))
            continue

        job = board.jobs_by_id.get(job_id)
        if job is None:
            decisions.append(_decision(
                "r1", content,
                "the project link points at %s, which is not in the database"
                % job_id,
                assignment=record.get("id"), job=job_id))
            continue

        if job.get("archived"):
            skipped.append(_skip("r1", content, "the project is archived"))
            continue

        key = source_id("r1", record.get("id"), target)
        if key in board.interaction_keys:
            skipped.append(_skip("r1", content, "already fired on an earlier pass"))
            continue

        if board.is_sensitive(job):
            decisions.append(_decision(
                "r1", content,
                "sensitive account. The project would have moved to %s and "
                "nothing was changed." % target,
                assignment=record.get("id"), job=job_id))
            continue

        current = _text(job.get("status")).lower()
        if current in PAST_THE_LINE:
            skipped.append(_skip(
                "r1", content,
                "the project is %s, which is past %s" % (current, target)))
            continue

        here, there = rules.rank(current), rules.rank(target)
        if here is None:
            decisions.append(_decision(
                "r1", content,
                "the project is %s, which is not on the forward line, so "
                "moving it to %s is not a decision a machine should make"
                % (current or "unset", target),
                assignment=record.get("id"), job=job_id))
            continue
        if here >= there:
            skipped.append(_skip(
                "r1", content,
                "the project is already %s, at or past %s" % (current, target)))
            continue

        if target == "invoiced" and not board.has_invoice(job_id):
            decisions.append(_decision(
                "r1", content,
                "moving the project to invoiced needs an invoice record with "
                "a number on it, and there is none. gate_invoiced blocks this "
                "and cannot be bypassed, so the move was refused rather than "
                "made and then failed.",
                assignment=record.get("id"), job=job_id))
            continue

        todoist_id = _text(record.get("todoist_id"))
        title = board.job_title(job_id)
        actions.append({
            "rule": "r1",
            "assignment": record.get("id"),
            "todoist_id": todoist_id,
            "content": content,
            "job": job_id,
            "job_title": title,
            "client": board.client_of(job),
            "from": current,
            "to": target,
            "changed_by": "task:%s" % todoist_id if todoist_id else "task",
            "source_id": key,
            "subject": "Project %s: %s" % (target, title),
            "summary": (
                "Task %r (Todoist %s) was completed, so the project moved "
                "from %s to %s."
                % (content, todoist_id or "none", current, target)
            ),
        })
    return actions, decisions, skipped


def generate_chases(board, rules, today, schedule=None):
    """R2. One chase task, seven days after the job was quoted.

    Once. From the moment it exists it is an ordinary task, and the
    waiting engine owns its chase dates, any second chase and its move to
    Stalled. This engine does not interfere with the one that already
    moves tasks.

    The seven-day clock starts at the job's `status_changed_at`. A quoted
    job without one goes to needs_decision with a pointer to
    tools/backfill_status_changed.py rather than a guessed start date.
    """
    actions, decisions, skipped = [], [], []
    schedule = schedule or planner_mod.Planner(rules.settings, board.tasks)

    for job in board.jobs:
        if job.get("archived"):
            continue
        if _text(job.get("status")).lower() != "quoted":
            continue
        job_id = job.get("id")
        title = board.job_title(job_id)

        changed = _as_date(job.get(JOB_CHANGED_AT))
        if changed is None:
            decisions.append(_decision(
                "r2", title,
                "the project is quoted but nothing recorded when it moved "
                "there, so the seven day clock has no start. Run "
                "tools.backfill_status_changed.",
                job=job_id))
            continue

        waited = (today - changed).days
        if waited < rules.chase_after_days:
            continue

        existing = board.chase_for(job_id)
        if existing is not None:
            skipped.append(_skip(
                "r2", title,
                "a chase task already exists for this project (%s)"
                % existing.get("id")))
            continue

        key = source_id("r2", job_id)
        if key in board.interaction_keys:
            skipped.append(_skip("r2", title, "a chase was already created once"))
            continue

        if board.is_sensitive(job):
            decisions.append(_decision(
                "r2", title,
                "sensitive account, quoted %d days ago with no chase task. "
                "Nothing was created." % waited,
                job=job_id))
            continue

        path = _chase_project_path(board, job_id)
        if not path:
            decisions.append(_decision(
                "r2", title,
                "no project path could be found, on the send quote task or on "
                "the task group, so the chase task would have nowhere to "
                "point. Nothing was created.",
                job=job_id))
            continue

        client_id = board.client_of(job)
        quotes = board.quotes_for(job_id)
        quote = quotes[0] if quotes else {}
        number = quote_number(quote)

        # The chase belongs on the day it falls due, unless that day has
        # passed. A slot in the past would land among parked overdue work.
        due_on = max(changed + timedelta(days=rules.chase_after_days), today)
        slot = schedule.reserve(due_on, CHASE_MINUTES)
        if slot is None:
            decisions.append(_decision(
                "r2", title,
                "no free %d minute slot inside working hours in the next two "
                "weeks, so the chase was not booked on top of something else."
                % CHASE_MINUTES,
                job=job_id))
            continue

        actions.append({
            "rule": "r2",
            "job": job_id,
            "job_title": title,
            "client": client_id,
            "title": _chase_title(board, client_id, number),
            "description": _chase_description(
                path, job_id, quote, number, changed, waited),
            "due": planner_mod.Planner.as_due_string(slot),
            "section": "today" if slot.date() == today else "upcoming",
            "quote": number or "none",
            "waited": waited,
            "source_id": key,
            "subject": "Chase task created: %s"
                       % _chase_title(board, client_id, number),
            "summary": (
                "The project has been quoted since %s with no reply, %d days, "
                "so one chase task was created for %s. The waiting engine owns "
                "it from here."
                % (changed.isoformat(), waited,
                   planner_mod.Planner.as_due_string(slot))
            ),
        })
    return actions, decisions, skipped


def _chase_project_path(board, job_id):
    """Where the chase task should say it belongs.

    The project line of any task linked to the job first (usually the
    "Send the quote" task), then the task group's `project_path`. No path
    is constructed from a client name, because a guessed path would point
    the reader at a folder that may not exist.
    """
    for record in board.assignments:
        if _text(record.get("job")) != _text(job_id):
            continue
        path = project_path(board.description_of(record))
        if path:
            return path
    return board.group_path(job_id)


def _chase_title(board, client_id, number):
    client = board.client_name(client_id) or "Unknown client"
    contact = board.contact_name(client_id)
    who = "%s / %s" % (client, contact) if contact else client
    tail = " %s" % number if number else ""
    return "%s — Chase quote%s" % (who, tail)


def _chase_description(path, job_id, quote, number, changed, waited):
    amount = quote.get("amount")
    sent = _as_date(quote.get("sent_date")) or changed
    lines = [
        "⏱ Est: %dm" % CHASE_MINUTES,
        "\U0001f4c1 Project: %s" % path,
        render_auto(QUOTED_CHASE, job_id),
        "",
        "Quote %s%s, sent %s."
        % (number or "with no number on it",
           " — %s" % _text(quote.get("title")) if quote.get("title") else "",
           sent.isoformat()),
        "Value %s. Quoted %s, %d days with no reply."
        % (("$%s" % amount) if amount not in (None, "", 0) else "not recorded",
           changed.isoformat(), waited),
    ]
    return "\n".join(lines)


def clean_up(board, rules):
    """R3. Close the chase once the job has moved on.

    Completed with a comment, never deleted. The comment explains in
    Todoist's completed view why the task was closed.

    Only a task carrying `auto_kind` is touched. A task with the same
    title that a person wrote is left alone, whatever it says.
    """
    actions, decisions, skipped = [], [], []
    for record in board.assignments:
        if _text(record.get(PB_AUTO_FIELD)) != QUOTED_CHASE:
            continue
        if not _open(record):
            continue
        job_id = _text(record.get("job"))
        job = board.jobs_by_id.get(job_id)
        content = _text(record.get("content"))
        if job is None:
            skipped.append(_skip(
                "r3", content,
                "the chase is not linked to a project that exists, so there "
                "is nothing to compare it against"))
            continue

        status = _text(job.get("status")).lower()
        if status == "quoted":
            continue

        key = source_id("r3", record.get("id"), status)
        if key in board.interaction_keys:
            skipped.append(_skip("r3", content, "already closed on an earlier pass"))
            continue

        if board.is_sensitive(job):
            decisions.append(_decision(
                "r3", content,
                "sensitive account. The project is %s and this chase is still "
                "open. Nothing was closed." % status,
                assignment=record.get("id"), job=job_id))
            continue

        when = datetime.now(timezone.utc).date().isoformat()
        actions.append({
            "rule": "r3",
            "assignment": record.get("id"),
            "todoist_id": _text(record.get("todoist_id")),
            "content": content,
            "job": job_id,
            "job_title": board.job_title(job_id),
            "client": board.client_of(job),
            "status": status,
            "comment": "Closed by trigger: job moved to %s on %s" % (status, when),
            "archived_reason": "trigger: job moved to %s" % status,
            "source_id": key,
            "subject": "Chase task closed: %s (job moved to %s)"
                       % (content, status),
            "summary": (
                "The project moved to %s, so the chase task %r (Todoist %s) "
                "was completed and archived. Nothing was deleted."
                % (status, content, _text(record.get("todoist_id")) or "none")
            ),
        })
    return actions, decisions, skipped


def plan(seen, rules=None, today=None, schedule=None):
    """Everything one pass would do, worked out before anything is sent.

    Nothing here touches the network, so the whole plan can be printed
    and read before a single write happens. All four rules read the same
    snapshot, taken before any write, so no rule sees a half-applied
    change from another, and a trigger R0 adds is reported before it can
    ever move a job.
    """
    rules = rules or Rules()
    today = today or datetime.now(timezone.utc).date()
    board = Board(seen)

    decisions, skipped = [], []
    r0, first_d, first_s = assign_missing(board, rules)
    r1, second_d, second_s = fire_completions(board, rules)
    r2, third_d, third_s = generate_chases(board, rules, today, schedule)
    r3, fourth_d, fourth_s = clean_up(board, rules)
    for chunk in (first_d, second_d, third_d, fourth_d):
        decisions.extend(chunk)
    for chunk in (first_s, second_s, third_s, fourth_s):
        skipped.extend(chunk)

    return {
        "r0": r0,
        "r1": r1,
        "r2": r2,
        "r3": r3,
        "needs_decision": decisions,
        "skipped": skipped,
        "at": datetime.now(timezone.utc).isoformat(),
        "today": today.isoformat(),
    }


def actions(item):
    """Every action in a plan, in the order a pass applies them."""
    return list(item["r0"]) + list(item["r1"]) + list(item["r2"]) + list(item["r3"])


def describe(item):
    """The plan in words, for the pass that writes nothing."""
    lines = ["", "Trigger pass for %s." % item["today"], ""]

    def block(title, rows, render):
        lines.append("%s: %d" % (title, len(rows)))
        for row in rows[:100]:
            lines.append("  %s" % render(row))
        if len(rows) > 100:
            lines.append("  and %d more" % (len(rows) - 100))
        lines.append("")

    block("Tasks that would gain a trigger", item["r0"],
          lambda r: "%s\n      to %s" % (r["content"][:78], r["status"]))
    block("Projects a finished task would move", item["r1"],
          lambda r: "%s\n      %s, %s to %s"
                    % (r["job_title"][:78], r["content"][:60], r["from"], r["to"]))
    block("Chase tasks that would be created", item["r2"],
          lambda r: "%s\n      %s, quoted %d days ago"
                    % (r["title"][:78], r["due"], r["waited"]))
    block("Chase tasks that would be closed", item["r3"],
          lambda r: "%s\n      the project is now %s" % (r["content"][:78], r["status"]))

    if item["needs_decision"]:
        lines.append("These need a decision from you. Nothing was done to them.")
        for row in item["needs_decision"][:40]:
            lines.append("  [%s] %s" % (row["rule"], row["what"][:80]))
            lines.append("      %s" % row["why"])
        lines.append("")

    if item["skipped"]:
        counts = {}
        for row in item["skipped"]:
            counts[row["why"]] = counts.get(row["why"], 0) + 1
        lines.append("Left alone: %d" % len(item["skipped"]))
        for why, count in sorted(counts.items(), key=lambda pair: -pair[1]):
            lines.append("  %-3d %s" % (count, why))
        lines.append("")

    return "\n".join(lines)
