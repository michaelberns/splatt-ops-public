"""
The overdue engine: what happens to a task once it is past its due date.

The loop runs it once a day, on the first pass at or after 05:00 local
time. It can also be run by hand with `python -m daemon sweep`.

Terms
    real due date   the date a task was first due. core/realdue.py records
                    it once, on a pinned first line of the Todoist
                    description ("📌 Real due: 2026-07-12 | 49 days late")
                    and, when the engine is given `pb`, in a real_due
                    field on the PocketBase assignment.
    parked date     the date the engine writes into Todoist's due field
                    for late work: yesterday, date only, so the task sits
                    in the overdue view without taking up a time slot. It
                    is scratch space and is never used to measure age.
    age             whole days from the real due date to today.
    the ladder      the escalation steps for the operator's own late work.
    waiting work    a task blocked on a reply from a client or supplier
                    (see daemon/waiting.py). It is chased, not escalated.

The ladder (day counts are settings under `overdue.*`)
    day 1    move to Overdue and park on yesterday (date only, no time)
    day 3    tag @escalated and note why
    day 7    one Telegram alert to the ops channel
    day 14   tag @stall-warning and note the date it will stall. It stays
             in Overdue, parked as before.
    day 28   move to Stalled, clear the due date, and hand it to the
             operator as a decision

    Each rung writes its note once. The labels and markers already on the
    task (@escalated, @stall-warning, "alerted ops") record that a rung
    has fired.

Waiting work
    Moved to Waiting on Client or Waiting on Supplier instead of Overdue,
    and given a chase: a due date and time a number of business days
    ahead (5 for a client, 3 for a supplier) in a free working-hours slot
    found by core/planner.py. After two chases with no reply, or when a
    wait was never chased and is already past the stall threshold, it
    moves to Stalled with its date cleared and becomes a decision.

Rules the engine keeps
    - Age is measured from the real due date, never from the parked date.
      A task re-parked on yesterday every morning for seven weeks is 49
      days late, not 1.
    - Nothing is completed or deleted. A task is only moved, re-dated,
      tagged, or given a note.
    - Priority is a colour and nothing else. The engine paints the colour
      of the column a task lands in, and no decision here reads a task's
      priority. tests/test_no_priority_logic.py checks the source for it.
    - A placement made by a person is respected. A late task the operator
      moved to today or a later day is left alone until that day passes,
      a task with an explicit time of day keeps its slot, and a task the
      operator filed in a waiting column stays there.
    - Every action is recorded in the write ledger (as a `todoist_task`
      update from source "overdue_engine"), so the validator can re-read
      it.
    - If a column the engine needs has no section id in settings, it
      refuses to run rather than half-sort the board.

Why Stalled clears the due date
    A Todoist task with no date does not appear in Today, Upcoming or any
    overdue filter, so stalled work leaves every working view while
    staying intact in its own column. The real date is kept on the pinned
    line, so clearing the due date loses nothing.
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone

from core import planner as planner_mod
from core import realdue
from core.ledger import TODOIST_COLLECTION
from core.planner import Planner

from .waiting import CHASE_MARKER, CLIENT, SUPPLIER, WaitingRouter

# Read only. Existing descriptions can hold "auto rescheduled" lines, one
# per automatic re-date; the engine no longer writes them. The count is
# shown as the push count on the pinned line and in the daily summary,
# and is never used to work out a task's age.
RESCHEDULE_MARKER = "auto rescheduled"
# Starts the note written on the day 3 rung.
ESCALATION_MARKER = "ESCALATED"

STALL_WARNING_LABEL = "stall-warning"

#: Written into the description when the day 7 alert is sent, and read
#: back so each task is alerted about only once rather than every morning.
ALERT_MARKER = "alerted ops"

#: What Todoist accepts to mean "remove the due date".
NO_DATE = "no date"

# Todoist's apps show P1 as the top priority and P4 as the bottom, but its
# API numbers them the other way round, so API 4 is P1. Every priority in
# this repo, settings included, is written the way the apps show it, and
# this table is the only place that translates. The engine only ever
# writes a priority (as a colour), so a reversed table would paint every
# task the wrong colour.
PRIORITY_BY_LABEL = {"p1": 4, "p2": 3, "p3": 2, "p4": 1}


class PriorityError(ValueError):
    """The configured priority is not one Todoist has."""


def api_priority(value):
    """Turn a priority written the way Todoist shows it into the API number.

    >>> api_priority("p1")   # 4
    >>> api_priority("P4")   # 1

    A bare number is refused rather than guessed at, because a bare 1
    reads as P1 to a person and means P4 to the API.
    """
    text = str(value or "").strip().lower()
    if text in PRIORITY_BY_LABEL:
        return PRIORITY_BY_LABEL[text]
    raise PriorityError(
        "priority %r is not valid. Write it the way Todoist shows it, one of "
        "p1, p2, p3 or p4, where p1 is the top." % value
    )


def reschedule_count(task):
    """How many "auto rescheduled" lines the description holds. Display only."""
    return len(re.findall(RESCHEDULE_MARKER, (task or {}).get("description") or "",
                          re.IGNORECASE))


class Action:
    """One thing the engine did or would do, in plain words.

    `kind` is, for example, "moved", "updated" or "mismatch", or in a dry
    run "would move", "would update" or "would alert".
    """

    def __init__(self, task_id, kind, detail, content=""):
        self.task_id = task_id
        self.kind = kind
        self.detail = detail
        self.content = content
        self.at = datetime.now(timezone.utc).isoformat()

    def __str__(self):
        return "%s %s: %s" % (self.kind, self.task_id, self.detail)

    def to_dict(self):
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "detail": self.detail,
            "content": self.content,
            "at": self.at,
        }


class Plan:
    """What should happen to one task, as plain data.

    plan_for builds a Plan without touching the network, and
    OverdueEngine._apply turns it into Todoist calls. Keeping the decision
    as data means it can be tested on its own.

    column     section name to move the task to, or None
    due        due string to write (a park or a chase slot), or None
    clear_due  remove the due date (Stalled)
    labels     labels to add
    note       one line appended to the description, dated today
    colour     priority to paint, written the Todoist way ("p2")
    alert      send the day 7 Telegram alert
    decision   hand the task to the operator; `reason` says why
    """

    def __init__(self, column=None, due=None, clear_due=False, labels=(),
                 note="", colour="", alert=False, decision=False, reason=""):
        self.column = column
        self.due = due
        self.clear_due = clear_due
        self.labels = list(labels)
        self.note = note
        self.colour = colour
        self.alert = alert
        self.decision = decision
        self.reason = reason

    def is_noop(self):
        """True when the plan changes nothing. A colour on its own does not
        count, so a task is never updated only to repaint it."""
        return not any([
            self.column, self.due, self.clear_due, self.labels,
            self.note, self.alert, self.decision,
        ])


class Thresholds:
    """The ladder's day counts, labels and colours, read once from settings."""

    def __init__(self, settings=None):
        get = (settings.get if settings else lambda key, default=None: default)
        self.overdue_after = int(get("overdue.reschedule_after_days", 1) or 1)
        self.escalate_after = int(get("overdue.escalate_after_days", 3) or 3)
        self.alert_after = int(get("overdue.alert_after_days", 7) or 7)
        self.warn_after = int(get("overdue.stall_warning_days", 14) or 14)
        self.stall_after = int(get("overdue.stall_after_days", 28) or 28)
        self.escalated_label = str(
            get("overdue.escalated_label", "escalated") or ""
        ).strip().lstrip("@")
        self.warning_label = str(
            get("overdue.stall_warning_label", STALL_WARNING_LABEL) or ""
        ).strip().lstrip("@")
        self.colours = {
            "today": str(get("overdue.colours.today", "p1") or "p1"),
            "overdue": str(get("overdue.colours.overdue", "p2") or "p2"),
            "waiting": str(get("overdue.colours.waiting", "p3") or "p3"),
            "stalled": str(get("overdue.colours.stalled", "p4") or "p4"),
        }


def plan_for(task, age, waiting_kind, thresholds, chase_slot=None,
             chases_done=0, chases_allowed=2, stall_date=None,
             chase_pending=False):
    """Decide what happens to one task. Pure, so it can be tested alone.

    `age` is days past the real due date. `waiting_kind` is CLIENT,
    SUPPLIER, or None for the operator's own work. The chase arguments
    come from the WaitingRouter and the planner. Nothing in here looks at
    the task's priority, its current section or its parked date.

    Each rung of the ladder writes its note once. The engine runs daily,
    so a note written on every pass would add a line a day to the
    description, and on waiting work would count as a fresh chase each
    morning, using up two chases in two days. The labels and markers
    already on the task are what say whether a rung has fired.
    """
    if age < thresholds.overdue_after:
        return Plan()

    if waiting_kind:
        return _waiting_plan(
            waiting_kind, age, thresholds, chase_slot, chases_done,
            chases_allowed, chase_pending,
        )
    return _own_work_plan(task, age, thresholds, stall_date)


def _waiting_plan(kind, age, thresholds, chase_slot, chases_done,
                  chases_allowed, chase_pending):
    """The plan for work blocked on someone else. It never escalates."""
    column = "waiting_supplier" if kind == SUPPLIER else "waiting_client"

    if chase_pending:
        # A chase is already booked for a day that has not arrived. Only
        # make sure the task is in the right column.
        return Plan(column=column, colour=thresholds.colours["waiting"])

    if chases_done == 0 and age >= thresholds.stall_after:
        # Never chased, and already past the stall threshold. A first
        # chase email on a thread this cold (possibly many of them on the
        # same morning) does more harm than good, so the task is parked
        # in Stalled for the operator to pick up, for example by phone.
        #
        # This only applies to a thread nobody has chased. A task already
        # on the chase ladder is being worked and stays on it, however
        # old it is.
        return Plan(
            column="stalled",
            clear_due=True,
            colour=thresholds.colours["stalled"],
            note="%d days waiting and never chased. Too cold for a chase "
                 "email, so it is parked. Pick it up by phone if it still "
                 "matters." % age,
            decision=True,
            reason="%d days waiting, never chased" % age,
        )

    if chases_done >= chases_allowed:
        return Plan(
            column="stalled",
            clear_due=True,
            colour=thresholds.colours["stalled"],
            note="chased %d times with no reply, stopping. This needs you, "
                 "not another email." % chases_done,
            decision=True,
            reason="%d chases, no reply" % chases_done,
        )

    plan = Plan(column=column, colour=thresholds.colours["waiting"])
    if chase_slot:
        plan.due = chase_slot
        plan.note = "%s for %s (chase %d of %d)" % (
            CHASE_MARKER, chase_slot, chases_done + 1, chases_allowed
        )
    else:
        # No free slot inside the planner's search window. Flag it for
        # the operator rather than book on top of existing work.
        plan.note = "no free slot found for a chase, needs scheduling by hand"
        plan.decision = True
        plan.reason = "no free slot for chase"
    return plan


def _own_work_plan(task, age, thresholds, stall_date):
    """The operator's own late work: the ladder."""
    if age >= thresholds.stall_after:
        return Plan(
            column="stalled",
            clear_due=True,
            colour=thresholds.colours["stalled"],
            note="%d days past the real due date, stalled. Due date cleared, "
                 "no longer scheduled." % age,
            decision=True,
            reason="%d days overdue" % age,
        )

    plan = Plan(column="overdue", colour=thresholds.colours["overdue"])
    # A due time of day set by a person is a placement: the ladder still
    # runs, but the slot is not overwritten with the park. Everything else
    # is parked on yesterday.
    if not (task or {}).get("_placed"):
        plan.due = planner_mod.park_string(*_park_reference(task))

    already = set((task or {}).get("labels") or [])
    description = (task or {}).get("description") or ""

    warning = thresholds.warning_label
    if age >= thresholds.warn_after and warning and warning not in already:
        plan.labels.append(warning)
        plan.note = "%d days past the real due date. Will move to Stalled on %s " \
                    "unless it is done or rescheduled." % (
                        age,
                        stall_date.isoformat() if stall_date
                        else "day %d" % thresholds.stall_after,
                    )

    escalated = thresholds.escalated_label
    if age >= thresholds.escalate_after and escalated and escalated not in already:
        plan.labels.append(escalated)
        if not plan.note:
            plan.note = "%s: %d days past the real due date." % (
                ESCALATION_MARKER, age
            )

    if age >= thresholds.alert_after and ALERT_MARKER not in description:
        plan.alert = True
        plan.note = ((plan.note + " ") if plan.note else "") + (
            "%s at %d days late." % (ALERT_MARKER, age)
        )

    return plan


def _park_reference(task):
    """The day and time late work parks on.

    Parking is relative to today, not to the task, so the engine passes
    today and the park time in on the task dict (`_today`, `_park_time`).
    That keeps plan_for pure and lets a test pin the clock without
    monkeypatching.
    """
    task = task or {}
    return task.get("_today"), task.get("_park_time") or planner_mod.PARK_TIME


def _park_time(settings):
    """The configured parking time (`planner.park_time`), or midnight.

    A malformed value falls back to midnight rather than raising, so a
    typo in one settings line does not stop the whole sweep. Midnight is
    the safe answer because it books no working time.
    """
    get = (settings.get if settings else lambda key, default=None: default)
    raw = str(get("planner.park_time", "") or "").strip()
    if not raw:
        return planner_mod.PARK_TIME
    try:
        hour, _, minute = raw.partition(":")
        return time(int(hour), int(minute or 0))
    except ValueError:
        return planner_mod.PARK_TIME


class OverdueEngine:
    """Applies the ladder and the waiting rules to every task on the board.

    `todoist` is a core.todoist.TodoistClient (or a test fake) and
    `settings` the loaded configuration. Optional: `notifier` sends
    Telegram alerts, `ledger` records every action, `pb` lets the engine
    read and write the PocketBase copy of each real due date, and
    `dry_run` records what would happen without writing anything.

    After run(): `actions` lists what was done, `needs_decision` holds
    (task, age, reason) for every task handed to the operator, `failures`
    lists what went wrong, and `mismatches` lists tasks whose pinned real
    due date disagrees with PocketBase.
    """

    def __init__(self, todoist, settings, notifier=None, ledger=None,
                 dry_run=False, logger=None, pb=None):
        self.todoist = todoist
        self.settings = settings
        self.notifier = notifier
        self.ledger = ledger
        self.dry_run = dry_run
        self.log = logger
        self.pb = pb
        self.thresholds = Thresholds(settings)
        self.router = WaitingRouter(settings)
        self.park_time = _park_time(settings)
        self.actions = []
        self.needs_decision = []
        self.failures = []
        self.mismatches = []

    def run(self, today=None):
        """Sweep every task once and return the actions. `today` defaults
        to the current UTC date."""
        today = today or datetime.now(timezone.utc).date()
        try:
            tasks = self.todoist.tasks()
        except Exception as exc:
            self.failures.append("could not read Todoist: %s" % exc)
            return self.actions

        missing = self._missing_sections()
        if missing:
            # Refuse rather than half-apply. A sweep that moved some tasks
            # and not others would leave a board that the next sweep reads
            # as intentional.
            self.failures.append(
                "these columns are not configured yet, so nothing was moved: %s. "
                "Create them in Todoist and put their ids in "
                "config/settings.yaml under todoist.sections."
                % ", ".join(sorted(missing))
            )
            return self.actions

        schedule = Planner(self.settings, tasks)

        for task in tasks:
            try:
                self._handle(task, today, schedule)
            except Exception as exc:
                self.failures.append(
                    "could not handle %s: %s" % (task.get("id"), exc)
                )

        if self.needs_decision:
            self._alert_decisions()
        return self.actions

    def _missing_sections(self):
        """Columns the engine needs that have no section id in settings."""
        configured = dict(getattr(self.todoist, "sections", None) or {})
        needed = ["overdue", "waiting_client", "waiting_supplier", "stalled"]
        return {name for name in needed if not configured.get(name)}

    def _handle(self, task, today, schedule):
        """Work out the plan for one task and apply it."""
        task_id = str(task.get("id"))
        content = task.get("content") or ""
        description = task.get("description") or ""

        mirror = realdue.read_mirror(self.pb, task_id) if self.pb else None
        clash = realdue.mismatch(description, mirror)
        if clash:
            # PocketBase wins. The disagreement is reported rather than
            # silently repaired, so a person can check which date is right.
            self.mismatches.append(dict(clash, task_id=task_id, content=content))
            self._record(task_id, "mismatch",
                         "pinned %s, PocketBase %s, PocketBase wins"
                         % (clash["pinned"], clash["pocketbase"]), content)

        real = realdue.resolve(task, today=today, pb_value=mirror)
        age = real.age(today)

        kind = self._side_of_desk(task)
        chases = self.router.chase_count(task)

        # A task due today or on a later day was put there by a person.
        # The operator's own work is left entirely alone until that day
        # has passed. Waiting work carries on below, where chase_pending
        # gives it the same protection.
        due_day = planner_mod.task_date(task)
        if not kind and due_day and due_day >= today:
            return

        # An explicit time of day, other than the park time itself, is
        # also a placement. The ladder still runs once it is late, but
        # the slot is never overwritten with the park.
        due_at = planner_mod.task_datetime(task)
        placed = bool(due_at and due_at.time() != self.park_time)

        # A waiting task's due date is its chase date. If that day has not
        # arrived, the chase is already booked and nothing is re-booked.
        # Otherwise a new chase would be booked every morning and the
        # two-chase limit used up in two days.
        chase_due = planner_mod.task_date(task)
        chase_pending = bool(kind and chase_due and chase_due > today)

        # Book the next chase N business days out, in a free slot.
        chase_slot = None
        if kind and not chase_pending and chases < self.router.max_chases:
            wait_days = self.router.chase_days(kind)
            chase_on = planner_mod.add_business_days(today, wait_days)
            slot = schedule.reserve(
                chase_on, planner_mod.estimate_minutes(task)
            )
            chase_slot = Planner.as_due_string(slot) if slot else None

        stall_on = real.date + timedelta(days=self.thresholds.stall_after)
        task = dict(task, _today=today, _park_time=self.park_time,
                    _placed=placed)
        plan = plan_for(
            task, age, kind, self.thresholds,
            chase_slot=chase_slot,
            chases_done=chases,
            chases_allowed=self.router.max_chases,
            stall_date=stall_on,
            chase_pending=chase_pending,
        )
        if plan.is_noop():
            return

        self._apply(task, plan, real, age, today)

    def _apply(self, task, plan, real, age, today):
        """Turn a Plan into Todoist calls, ledger entries and alerts."""
        task_id = str(task.get("id"))
        content = task.get("content") or ""
        description = task.get("description") or ""

        # The pinned real-due line goes on every task the engine touches.
        # It is write-once: if a real due date is already recorded,
        # realdue.apply keeps it and only refreshes the derived age.
        new_description = realdue.apply(
            description, real, today=today, pushes=reschedule_count(task)
        )
        if plan.note:
            new_description = "%s\n%s: %s" % (
                new_description, today.isoformat(), plan.note
            )

        if plan.column and not self._already_in(task, plan.column):
            self._move(task_id, plan.column, content)

        fields = {"description": new_description}
        if plan.clear_due:
            fields["due_string"] = NO_DATE
        elif plan.due:
            fields["due_string"] = plan.due
        if plan.labels:
            fields["labels"] = self._labels_with(task, plan.labels)
        if plan.colour:
            # Painted, never read. See the module docstring.
            fields["priority"] = api_priority(plan.colour)

        detail = self._describe(plan, age, real)
        if self.dry_run:
            self._record(task_id, "would update", detail, content)
        else:
            try:
                self.todoist.update_task(task_id, **fields)
                self._record(task_id, "updated", detail, content)
            except Exception as exc:
                self.failures.append("could not update %s: %s" % (task_id, exc))
                return

        if self.pb and not self.dry_run:
            result = realdue.write_mirror(self.pb, task_id, real)
            if result == "failed":
                self.failures.append(
                    "could not mirror the real due date for %s" % task_id
                )

        if plan.decision:
            self.needs_decision.append((task, age, plan.reason))
        if plan.alert:
            self._alert_one(task, age)

    def _describe(self, plan, age, real):
        """One line describing a plan, for the action list and the ledger."""
        bits = ["%d days past real due (%s)" % (age, real.date.isoformat())]
        if real.guessed:
            bits.append("date is a guess")
        if plan.column:
            bits.append("to %s" % plan.column)
        if plan.clear_due:
            bits.append("due date cleared")
        elif plan.due:
            bits.append("parked at %s" % plan.due)
        if plan.labels:
            bits.append("tagged " + ", ".join("@%s" % lab for lab in plan.labels))
        return ", ".join(bits)

    def _column_of(self, task):
        """The name of the column a task is sitting in, or "".

        Empty covers a task at the top level of the project and a section
        this system does not manage. Neither is something to act on.
        """
        current = str((task or {}).get("section_id") or "")
        if not current:
            return ""
        configured = dict(getattr(self.todoist, "sections", None) or {})
        for name, section_id in configured.items():
            if section_id and str(section_id) == current:
                return name
        return ""

    def _already_in(self, task, column):
        """True when the task is in that column already.

        Skipping those moves saves an API call per task per sweep and
        keeps the ledger and the report to real changes.
        """
        return bool(column) and self._column_of(task) == column

    def _side_of_desk(self, task):
        """Who the task is waiting on: CLIENT, SUPPLIER, or None.

        A task the operator has filed in one of the two waiting columns by
        hand is believed first. That is a person's answer to the question
        the classifier guesses at from the wording, and overruling it
        would undo the correction on the next sweep.

        The classifier (daemon/waiting.py) is only used for tasks not
        filed that way, and it cannot always tell the two sides apart:
        Splatt recharges freight to clients and buys parts for client
        machines, so one task can use both vocabularies.

        Only the two waiting columns count as an instruction. Overdue is
        where late work lands automatically, so a task sitting there says
        nothing about who it is waiting on.
        """
        placed = self._column_of(task)
        if placed == "waiting_client":
            return CLIENT
        if placed == "waiting_supplier":
            return SUPPLIER
        return self.router.classify(task)

    def _move(self, task_id, column, content):
        """Move a task to a column, or record that it would be moved."""
        if self.dry_run:
            self._record(task_id, "would move", "to %s" % column, content)
            return
        try:
            self.todoist.move_to_section(task_id, column)
            self._record(task_id, "moved", "to %s" % column, content)
        except Exception as exc:
            self.failures.append("could not move %s: %s" % (task_id, exc))

    @staticmethod
    def _labels_with(task, wanted):
        """The task's own labels plus the ones being added.

        Todoist replaces the whole label list on an update rather than
        adding to it, so the existing ones have to be sent back or they
        are wiped. Order is kept so a person's own labels stay put.
        """
        existing = list((task or {}).get("labels") or [])
        for label in wanted:
            if label and label not in existing:
                existing.append(label)
        return existing

    # Telegram alerts
    def _alert_one(self, task, age):
        """The day 7 alert for one task, sent silently."""
        if not self.notifier:
            return
        message = "Overdue %d days: %s\nTodoist task %s" % (
            age, (task.get("content") or "")[:120], task.get("id")
        )
        if self.dry_run:
            self._record(str(task.get("id")), "would alert", "%d days overdue" % age)
            return
        if not self.notifier.send(message, silent=True):
            self.failures.append(
                "telegram alert failed for %s: %s"
                % (task.get("id"), self.notifier.last_error)
            )

    def _alert_decisions(self):
        """One message listing every task handed to the operator this sweep
        (the first 15 by name, then a count)."""
        if not self.notifier:
            return
        lines = [
            "%d tasks need a decision. The system has stopped working on these."
            % len(self.needs_decision),
            "",
        ]
        for task, age, reason in self.needs_decision[:15]:
            lines.append(
                "- %s (%s)" % ((task.get("content") or "")[:90], reason)
            )
        if len(self.needs_decision) > 15:
            lines.append("- and %d more" % (len(self.needs_decision) - 15))
        lines.append("")
        lines.append("Do them, delegate them, or drop them.")
        message = "\n".join(lines)
        if self.dry_run:
            self.actions.append(
                Action("", "would alert", "decision list of %d" % len(self.needs_decision))
            )
            return
        if not self.notifier.send(message):
            self.failures.append(
                "telegram decision list failed: %s" % self.notifier.last_error
            )

    # Action list and ledger
    def _record(self, task_id, kind, detail, content=""):
        """Keep an action for the report, log it, and outside a dry run
        record it in the ledger as an update to a `todoist_task`."""
        action = Action(task_id, kind, detail, content)
        self.actions.append(action)
        if self.log:
            self.log.info("overdue %s", action)
        if self.ledger and not self.dry_run:
            self.ledger.record(
                "update", TODOIST_COLLECTION, task_id, [kind], "overdue_engine", detail
            )

    def summary(self):
        """One line: actions counted by kind, then decisions, real-due
        mismatches and failures."""
        counts = {}
        for action in self.actions:
            counts[action.kind] = counts.get(action.kind, 0) + 1
        parts = ["%d %s" % (v, k) for k, v in sorted(counts.items())]
        text = ", ".join(parts) if parts else "nothing was overdue"
        if self.needs_decision:
            text += ". %d need a decision" % len(self.needs_decision)
        if self.mismatches:
            text += ". %d real due dates disagree with PocketBase" % len(self.mismatches)
        if self.failures:
            text += ". %d actions failed" % len(self.failures)
        return text
