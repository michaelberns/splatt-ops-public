"""
Scheduling arithmetic: where a task sits in time.

Inputs are Todoist task dicts and dates. Outputs are dates, datetimes and
due strings in the format the Todoist API accepts. Nothing here writes to
Todoist: the overdue engine (daemon/overdue.py) and the trigger engine
(core/triggers.py) decide, and the daemon sends. Keeping the date
handling here lets those engines read as policy.

The parking rule
    A parked date is where the overdue engine puts a late task: yesterday,
    as a plain date with no time. Yesterday is before anything the
    operator planned for today, so overdue work collects behind today's
    plan instead of on top of it. A date-only park shows as an all-day
    item in Todoist, and parked tasks never occupy working hours, so a
    large overdue pile does not make the day look full. The overdue
    column is a pile, not a schedule, so its tasks need no distinct times.

The chase rule
    Chasing someone is real work, so a chase gets a real slot inside
    working hours (08:00 to 17:00 by default, from settings `planner.*`)
    in a genuine gap between tasks already booked. This is where the
    "⏱ Est: 45m" estimate line in a task description is used.

The business day rule
    Follow-up timing is counted in business days, so weekends do not
    count: three business days after a Friday is the Wednesday.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone

# The estimate line the agent writes on every task it creates: "⏱ Est: 45m".
# Minutes or hours are accepted ("Est: 30 mins", "Est: 2h").
EST_RE = re.compile(r"⏱?\s*Est:\s*(\d+)\s*(m|min|mins|h|hr|hrs)?\b", re.IGNORECASE)

#: Used when a task carries no estimate. Short on purpose: a task with no
#: estimate is usually a quick reply, and guessing long would push real
#: work out of the day.
DEFAULT_MINUTES = 15

#: Midnight. Tasks due at this time count as parked, not booked (see Day).
PARK_TIME = time(0, 0)


def estimate_minutes(task, default=DEFAULT_MINUTES):
    """How long a task claims it will take, in minutes.

    >>> estimate_minutes({"description": "⏱ Est: 2h"})
    120
    """
    description = (task or {}).get("description") or ""
    match = EST_RE.search(description)
    if not match:
        return default
    try:
        value = int(match.group(1))
    except (TypeError, ValueError):
        return default
    unit = (match.group(2) or "m").lower()
    if unit.startswith("h"):
        value *= 60
    # A zero or negative estimate is a typo, not an instruction to book
    # nothing, and a task booked for no time silently overlaps whatever
    # comes next.
    return value if value > 0 else default


def is_weekend(day):
    return day.weekday() >= 5


def add_business_days(start, days):
    """The date N business days after start. Weekends do not count."""
    day = start
    if days <= 0:
        return day
    remaining = days
    while remaining > 0:
        day += timedelta(days=1)
        if not is_weekend(day):
            remaining -= 1
    return day


def next_business_day(day):
    """The first weekday on or after a date."""
    while is_weekend(day):
        day += timedelta(days=1)
    return day


def park_datetime(today=None, park_time=PARK_TIME):
    """Where an overdue task goes: yesterday, at midnight.

    Yesterday rather than today, because today holds only what the
    operator planned for today. Overdue work collects behind that instead
    of on top of it.
    """
    today = today or datetime.now(timezone.utc).date()
    return datetime.combine(today - timedelta(days=1), park_time)


def park_string(today=None, park_time=PARK_TIME):
    """The parking slot in the format the Todoist API takes.

    A literal date, not the word "yesterday". Todoist parses "yesterday"
    in the account timezone when the request arrives, so a sweep running
    near midnight could park a task on the wrong day.

    Date only, no time of day. A park written with "00:00" would be
    stored as a timed task at midnight, and many of those stack up at the
    top of the calendar day. A date-only park shows as an all-day item.

    >>> park_string(date(2026, 8, 27))
    '2026-08-26'
    """
    return park_datetime(today, park_time).strftime("%Y-%m-%d")


class Day:
    """One day's booked work, used to find a free slot in it.

    Only tasks with a time of day count as booked. A task that is merely
    dated to this day, which includes everything parked at midnight, does
    not occupy working hours and so does not block anything.
    """

    def __init__(self, day, start_hour=8, end_hour=17, buffer_minutes=15):
        self.day = day
        self.start = datetime.combine(day, time(start_hour, 0))
        self.end = datetime.combine(day, time(end_hour, 0))
        self.buffer = timedelta(minutes=buffer_minutes)
        self.booked = []

    def add(self, start_at, minutes):
        """Book a span. Ignores anything outside this day."""
        if start_at.date() != self.day:
            return
        self.booked.append((start_at, start_at + timedelta(minutes=minutes)))
        self.booked.sort()

    def add_task(self, task, default_minutes=DEFAULT_MINUTES):
        """Book a Todoist task if it has a time of day.

        A date only task is deliberately skipped. Parked overdue work is
        all date only or midnight, so a full overdue column does not make
        the day look busy.
        """
        start_at = task_datetime(task)
        if start_at is None:
            return
        if start_at.time() == PARK_TIME:
            return
        self.add(start_at, estimate_minutes(task, default_minutes))

    def free_slot(self, minutes):
        """The start of the first gap that fits, or None if the day is full.

        Leaves `buffer_minutes` either side of existing work where the gap
        allows it, so work is not stacked back to back, and drops the
        buffer when that is the only way the task fits.
        """
        need = timedelta(minutes=minutes)
        cursor = self.start
        for start_at, end_at in self.booked:
            if end_at <= cursor:
                cursor = max(cursor, end_at)
                continue
            gap_end = start_at
            # Try with a buffer on both sides first, then without. A gap
            # that only fits snugly is still better than pushing the task
            # to another day.
            for pad in (self.buffer, timedelta(0)):
                candidate = cursor + (pad if cursor > self.start else timedelta(0))
                if candidate + need + pad <= gap_end:
                    return candidate
            cursor = max(cursor, end_at)
        # After everything booked, up to the end of the day.
        for pad in (self.buffer, timedelta(0)):
            candidate = cursor + (pad if self.booked else timedelta(0))
            if candidate + need <= self.end:
                return candidate
        return None


def task_datetime(task):
    """A task's due moment, or None if it has no date.

    Returns None for a date only task, so callers can tell "due that day"
    apart from "booked at that time".
    """
    due = (task or {}).get("due") or {}
    raw = due.get("date") or (task or {}).get("dueDate")
    if not raw:
        return None
    text = str(raw)
    if len(text) == 10:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def task_date(task):
    """A task's due date, with or without a time. None if undated."""
    due = (task or {}).get("due") or {}
    raw = due.get("date") or (task or {}).get("dueDate")
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


class Planner:
    """Finds slots for chase work across the coming days."""

    def __init__(self, settings=None, tasks=None):
        get = (settings.get if settings else lambda key, default=None: default)
        self.start_hour = int(get("planner.working_hours.start", 8) or 8)
        self.end_hour = int(get("planner.working_hours.end", 17) or 17)
        self.buffer_minutes = int(get("planner.buffer_minutes", 15) or 15)
        self.search_days = int(get("planner.search_days", 14) or 14)
        self.skip_weekends = bool(get("planner.skip_weekends", True))
        self._days = {}
        for task in tasks or []:
            self.book(task)

    def _day(self, day):
        if day not in self._days:
            self._days[day] = Day(
                day, self.start_hour, self.end_hour, self.buffer_minutes
            )
        return self._days[day]

    def book(self, task):
        """Record an existing task as occupying its slot."""
        start_at = task_datetime(task)
        if start_at is None:
            return
        self._day(start_at.date()).add_task(task)

    def find_slot(self, on, minutes):
        """The first free slot on or after a date, searching forward.

        Weekends are skipped when `planner.skip_weekends` is set. Returns
        None if nothing fits inside `planner.search_days`; the caller must
        then report it for the operator to decide rather than book on top
        of something.
        """
        day = next_business_day(on) if self.skip_weekends else on
        for offset in range(self.search_days):
            candidate_day = day + timedelta(days=offset)
            if self.skip_weekends and is_weekend(candidate_day):
                continue
            slot = self._day(candidate_day).free_slot(minutes)
            if slot is not None:
                return slot
        return None

    def reserve(self, on, minutes):
        """Find a slot and mark it taken, so two tasks in one sweep cannot
        both be given the same time."""
        slot = self.find_slot(on, minutes)
        if slot is None:
            return None
        self._day(slot.date()).add(slot, minutes)
        return slot

    @staticmethod
    def as_due_string(slot):
        """A slot in the format the Todoist API takes."""
        return slot.strftime("%Y-%m-%d %H:%M") if slot else ""
