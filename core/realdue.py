"""
Real due date: the date a task was really due, recorded once and never moved.

Terms
    Real due date  the date a task was actually due. It is recorded once
                   and treated as read-only afterwards.
    Parked date    the due date Todoist shows for a late task. The overdue
                   engine (daemon/overdue.py) moves late tasks to
                   yesterday ("parks" them, see core/planner.py) so they
                   collect in one place, so the Todoist date no longer
                   says how late a task is.
    Pin line       the description line that records the real due date:
                       📌 Real due: 2026-07-12 | 49 days late | 3 pushes
                   or, when the date had to be estimated,
                       📌 Real due: 2026-07-12 (guessed) | 49 days late
    Header         the run of lines at the top of a description that this
                   system writes: the pin line, the project line
                   ("📁 Project: ..."), the estimate ("⏱ Est: 45m") and the
                   trigger lines ("🔁 ...", see core/triggers.py). The first
                   line that is none of these starts the body.

Why it exists
    The overdue engine's ladder (move to Overdue, tag, alert, stall) is
    driven by age. Parking rewrites the Todoist due date, so age has to be
    measured from a date that parking never touches.

Two copies
    The pin line, so the operator can see the date in Todoist, and the
    `real_due` field on the PocketBase assignment, so an accidental edit
    of the description cannot lose it. If they disagree, PocketBase wins,
    because the description is the copy a person can overwrite. The
    disagreement is reported by `mismatch()`, never repaired silently.

Resolve order (see `resolve`)
    1. the pin line
    2. the PocketBase mirror
    3. recovered from legacy escalation notes in the description
    4. the Todoist creation date, labelled "(guessed)"
    5. the current Todoist due date, labelled "(guessed)"
    6. today, labelled "(guessed)", so a task is never left without an age

Write once
    `apply()` never changes a real due date that is already pinned. It
    only refreshes the derived parts of the line (the age and the push
    count), which change every day. Correcting a real due date is a
    deliberate manual edit, and `mismatch()` is how the need shows up.

Guessed dates are labelled
    A date estimated from the creation date is marked "(guessed)" on the
    line and in `RealDue.guessed`, so an estimate is never presented as a
    recorded fact.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone

# The pin line. `render()` writes it and PIN_RE reads it back. Both are
# defined here, next to each other, so the writer and the reader agree.
#
#   📌 Real due: 2026-07-12 | 49 days late | 3 pushes
#   📌 Real due: 2026-07-12 (guessed) | 49 days late | 3 pushes
PIN = "📌"
GUESSED_LABEL = "guessed"

PIN_RE = re.compile(
    r"^\s*%s\s*Real due:\s*(\d{4}-\d{2}-\d{2})\s*(\(%s\))?"
    % (re.escape(PIN), GUESSED_LABEL),
    re.IGNORECASE,
)

# The other header lines this system writes around the pin.
#
# A pin counts wherever it sits in the header: the run of blank lines,
# pins and these lines at the very top of a description. Scanning stops
# at the first body line, so a quoted email further down that happens to
# contain "📌 Real due:" is never read as the record.
#
# Every line this system writes into the header must be listed here. A
# header line this pattern does not know ends the header early, and a
# pin below it becomes invisible. The 🔁 lines belong to core/triggers.py:
# "On complete: job to quoted" says what finishing the task does, and
# "Auto: quoted_chase for job x" marks a task the trigger engine created.
HEADER_RE = re.compile(
    r"^\s*(📁\s*Project:|⏱\s*Est:|🔁\s*(?:On complete|Auto):)",
    re.IGNORECASE,
)

# Legacy escalation notes. Descriptions written by earlier escalation
# tooling carry lines such as
#
#   ESCALATED 2026-08-19: auto rescheduled to today, 38 days overdue
#
# Each records the true age on the day it was written, so the real due
# date is 2026-08-19 minus 38 days = 2026-07-12. `recover()` uses these
# when the system is adopted on a board that already has them. The gap
# allowed between the date and the count is generous but capped, and it
# cannot cross a line break, so a date is never paired with a count from
# an unrelated note.
HISTORY_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})[^\n]{0,120}?(\d+)\s+days?\s+overdue",
    re.IGNORECASE,
)

# "auto rescheduled" markers count how many times a task was pushed. The
# count is shown on the pin line as history but never used to compute
# age: pushes stop at overdue.max_auto_reschedules, so an age derived
# from the count would stop growing.
PUSH_RE = re.compile(r"auto rescheduled", re.IGNORECASE)

PB_COLLECTION = "assignments"
PB_FIELD = "real_due"


class RealDue:
    """A real due date, where it came from, and whether it is a guess.

    `source` is one of the constants below. `guessed` is False for the
    trusted sources (pin line, PocketBase, recovered notes) and True for
    the fallbacks, unless the caller says otherwise.
    """

    #: source values, in the order resolve() prefers them
    PINNED = "pinned"
    POCKETBASE = "pocketbase"
    RECOVERED = "recovered"
    CREATED = "created"
    DUE = "due"

    #: the sources that are an honest record rather than a fallback guess
    TRUSTED = (PINNED, POCKETBASE, RECOVERED)

    def __init__(self, day, source, guessed=None):
        self.date = day
        self.source = source
        # A date is a guess unless it came from a trusted source. The pin
        # line passes `guessed` explicitly, because it records its own
        # "(guessed)" label and that must not be recomputed on read.
        if guessed is None:
            guessed = source not in self.TRUSTED
        self.guessed = bool(guessed)

    def age(self, today=None):
        """Days past the real due date. Negative means it is not due yet."""
        today = today or datetime.now(timezone.utc).date()
        return (today - self.date).days

    def __eq__(self, other):
        return (
            isinstance(other, RealDue)
            and other.date == self.date
            and other.guessed == self.guessed
        )

    def __repr__(self):
        return "RealDue(%s, %s%s)" % (
            self.date, self.source, ", guessed" if self.guessed else ""
        )


def _as_date(value):
    """Accept a date, a datetime, or any of Todoist's and PocketBase's
    string shapes. Returns None rather than raising, because a bad date on
    one task must not stop a sweep over the whole board."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    # PocketBase writes "2026-07-12 00:00:00.000Z", Todoist writes
    # "2026-07-12" or a full timestamp.
    text = text.replace("Z", "").replace("T", " ").strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def header_pins(description):
    """Every pinned line in the header, as (line number, RealDue) pairs.

    The header is the leading run of blank lines, pins, and lines matched
    by HEADER_RE. The walk stops at the first line that is none of those,
    because that line starts the body, and a pin in the body is not the
    record.
    """
    found = []
    for number, line in enumerate((description or "").splitlines()):
        if not line.strip():
            continue
        match = PIN_RE.match(line)
        if match:
            day = _as_date(match.group(1))
            if day:
                found.append(
                    (number, RealDue(day, RealDue.PINNED, guessed=bool(match.group(2))))
                )
            continue
        if HEADER_RE.match(line):
            continue
        break
    return found


def header_end(description):
    """The line number the body starts on.

    A new pin is inserted here, at the bottom of the header and below any
    project line, so adding it never pushes the existing header lines
    down.
    """
    end = 0
    for number, line in enumerate((description or "").splitlines()):
        if not line.strip():
            continue
        if PIN_RE.match(line) or HEADER_RE.match(line):
            end = number + 1
            continue
        break
    return end


def parse(description):
    """The real due date already pinned to a description, or None.

    A description should carry one pin. If it carries more, the record is
    chosen by evidence rather than position: a recorded date beats a
    guessed one, and an older date beats a newer one. The older date is
    the safer one to be wrong about, because it makes a task look more
    overdue, never less. `recover()` prefers the earliest date for the
    same reason.
    """
    pins = [pin for _, pin in header_pins(description)]
    if not pins:
        return None
    return sorted(pins, key=lambda pin: (pin.guessed, pin.date))[0]


def recover(description):
    """Recover the real due date from legacy escalation notes (HISTORY_RE).

    Takes the earliest date the notes imply. A task escalated several
    times carries several notes, and each later one was computed from a
    due date that had already been moved, so the earliest is closest to
    the truth. Returns None when there are no usable notes.

    >>> recover("ESCALATED 2026-08-19: auto rescheduled, 38 days overdue")
    datetime.date(2026, 7, 12)
    """
    best = None
    for match in HISTORY_RE.finditer(description or ""):
        written = _as_date(match.group(1))
        if not written:
            continue
        try:
            days = int(match.group(2))
        except ValueError:
            continue
        # A sanity bound: more than ten years late is a parse accident.
        if days < 0 or days > 3650:
            continue
        candidate = date.fromordinal(written.toordinal() - days)
        if best is None or candidate < best:
            best = candidate
    return best


def push_count(description):
    """How many "auto rescheduled" markers the description carries. History only."""
    return len(PUSH_RE.findall(description or ""))


def resolve(task, today=None, pb_value=None):
    """The real due date for a task, and where it came from.

    Preference order, best evidence first:

      1. the pin line, because it is the recorded decision
      2. PocketBase (`pb_value`), the durable mirror
      3. recovered from legacy escalation notes, exact where present
      4. the Todoist creation date, a labelled guess
      5. the current due date, a last resort and also a labelled guess

    Never returns None. A task with no usable date at all gets today,
    marked as a guess, because a task with no age would silently skip
    the overdue ladder.
    """
    today = today or datetime.now(timezone.utc).date()
    description = (task or {}).get("description") or ""

    pinned = parse(description)
    if pinned:
        return pinned

    mirrored = _as_date(pb_value)
    if mirrored:
        return RealDue(mirrored, RealDue.POCKETBASE)

    recovered = recover(description)
    if recovered:
        return RealDue(recovered, RealDue.RECOVERED)

    created = _as_date(
        (task or {}).get("added_at") or (task or {}).get("created_at")
    )
    if created:
        return RealDue(created, RealDue.CREATED)

    due = (task or {}).get("due") or {}
    current = _as_date(due.get("date") or (task or {}).get("dueDate"))
    if current:
        return RealDue(current, RealDue.DUE)

    return RealDue(today, RealDue.CREATED)


def render(real_due, today=None, pushes=0):
    """The pin line for a real due date.

    >>> render(RealDue(date(2026, 7, 12), RealDue.PINNED),
    ...        today=date(2026, 8, 30), pushes=3)
    '📌 Real due: 2026-07-12 | 49 days late | 3 pushes'
    """
    today = today or datetime.now(timezone.utc).date()
    age = real_due.age(today)
    if age > 0:
        age_text = "%d days late" % age
    elif age == 0:
        age_text = "due today"
    else:
        age_text = "due in %d days" % abs(age)

    line = "%s Real due: %s%s | %s" % (
        PIN,
        real_due.date.isoformat(),
        " (%s)" % GUESSED_LABEL if real_due.guessed else "",
        age_text,
    )
    if pushes:
        line += " | %d pushes" % pushes
    return line


def strip_pin(description):
    """The description without its pinned line. Header lines are kept."""
    numbers = [number for number, _ in header_pins(description)]
    if not numbers:
        return description or ""
    lines = (description or "").splitlines()
    for number in reversed(numbers):
        del lines[number]
        # Remove the blank line that followed the pin as well, or the gap
        # it leaves would grow by one line each time the pin is rewritten.
        if number < len(lines) and not lines[number].strip():
            if number == 0 or not lines[number - 1].strip():
                del lines[number]
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines)


def apply(description, real_due, today=None, pushes=None):
    """Return the description with its pinned line up to date, in place.

    Write once: if a real due date is already pinned, that date is kept
    even when the caller passes a different one, and only the derived age
    and push count are refreshed. The stored date always beats the
    caller's date; that is the safety property of this module.

    In place: the pin is rewritten on the line where it was found, not
    moved to the top, so the other header lines keep their positions. A
    description with no pin gets one at the bottom of its header.
    """
    description = description or ""
    existing = parse(description)
    keep = existing or real_due
    if pushes is None:
        pushes = push_count(description)
    line = render(keep, today=today, pushes=pushes)

    numbers = [number for number, _ in header_pins(description)]

    if len(numbers) == 1:
        lines = description.splitlines()
        lines[numbers[0]] = line
        return "\n".join(lines)

    # No pin, or more than one. Several pins are repaired here rather than
    # only reported, because parse() has already chosen which one is the
    # record: all pins are removed and that date is written once, at the
    # bottom of the header.
    body = strip_pin(description) if numbers else description
    if not body.strip():
        return line

    lines = body.splitlines()
    at = header_end(body)
    if at == 0:
        return "%s\n\n%s" % (line, body.lstrip("\n"))
    head, rest = lines[:at], lines[at:]
    while head and not head[-1].strip():
        head.pop()
    while rest and not rest[0].strip():
        rest.pop(0)
    return "\n".join(head + ["", line, ""] + rest)


def would_change_recorded_date(description, real_due):
    """True when a caller is trying to overwrite a date already recorded.

    apply() quietly keeps the stored date, which is right for a sweep
    over the whole board. This lets a caller such as a backfill tool or
    the validator report that an overwrite was attempted, so the attempt
    does not go unnoticed.
    """
    existing = parse(description)
    return bool(existing) and existing.date != real_due.date


# the PocketBase mirror
def read_mirror(pb, todoist_id):
    """The stored real due date for a task, or None.

    A missing field, a missing record or an unreachable server all mean
    the same thing to the caller, which is that there is no mirror to
    read. None of them are worth failing a sweep over, so none of them
    raise.
    """
    if not pb or not todoist_id:
        return None
    try:
        record = pb.find_one(PB_COLLECTION, 'todoist_id="%s"' % todoist_id)
    except Exception:
        return None
    if not record:
        return None
    return _as_date(record.get(PB_FIELD))


def write_mirror(pb, todoist_id, real_due):
    """Mirror the real due date to PocketBase. Returns what happened.

    One of: "written", "already set", "no record", "no field", "failed".
    A string rather than a bool, so a caller's report can tell "there
    was nothing to do" apart from "it did not work".
    """
    if not pb or not todoist_id or not real_due:
        return "failed"
    try:
        if PB_FIELD not in set(pb.field_names(PB_COLLECTION) or []):
            return "no field"
    except Exception:
        return "failed"
    try:
        record = pb.find_one(PB_COLLECTION, 'todoist_id="%s"' % todoist_id)
    except Exception:
        return "failed"
    if not record:
        return "no record"
    if _as_date(record.get(PB_FIELD)) == real_due.date:
        return "already set"
    try:
        pb.update(PB_COLLECTION, record["id"], {PB_FIELD: real_due.date.isoformat()})
    except Exception:
        return "failed"
    return "written"


def mismatch(description, pb_value):
    """Where the pinned line and the mirror disagree, describe it.

    PocketBase wins, because the Todoist description is the copy a person
    can overwrite by accident. Returns None when they agree or when
    either side is absent.
    """
    pinned = parse(description)
    mirrored = _as_date(pb_value)
    if not pinned or not mirrored or pinned.date == mirrored:
        return None
    return {
        "pinned": pinned.date.isoformat(),
        "pocketbase": mirrored.isoformat(),
        "winner": mirrored.isoformat(),
    }
