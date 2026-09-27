"""
Job statuses: the eleven values a job may hold, defined once for the Python side.

What it does
    `JOB_STATUSES` lists every valid value of `jobs.status`, in the order a
    job normally moves through them, so anything that renders the list
    top to bottom reads as a pipeline. The tuples below group the
    statuses the same way the dashboard's filters do, and `check()`
    refuses an invalid status before it reaches the database.

Where else the list lives
    - The database: the `status` select field on `jobs` (mirrored in the
      generated core/schema.py).
    - The dashboard: `JOB_STATUS_META` in dashboard/index.html, with the
      groups in `STATUS_GROUPS` in dashboard/filter_engine.js.
    tests/test_job_status.py fails if this module, the schema, the
    dashboard or a live PocketBase disagree, so a status cannot be saved
    that the dashboard cannot draw, or offered by the dashboard and then
    refused by the database.

Two values are intentionally absent
    `paid`: payment is recorded on the job's `paid` flag, `paid_amount` and
    `paid_at` fields. A finished, paid job is `completed` with
    `paid = True`, so a status would duplicate the flag.
    `active`: it said a job was live without saying what stage it was at.
    New jobs start as `quoting` instead.
"""

from __future__ import annotations

#: Every status a job may hold, in status flow order. Nothing outside this
#: tuple is a valid job status anywhere in the system.
JOB_STATUSES = (
    "quoting",
    "quoted",
    "won",
    "invoicing",
    "invoiced",
    "commissioning",
    "lost",
    "on_hold",
    "cancelled",
    "completed",
    "reengage",
)

#: Work that has not been won or lost yet. Matches STATUS_GROUPS.open in
#: dashboard/filter_engine.js.
OPEN = ("quoting", "quoted")

#: Won work, at some stage of being delivered and billed. Matches
#: STATUS_GROUPS.confirmed.
CONFIRMED = ("won", "invoicing", "invoiced", "commissioning")

#: Finished, one way or the other. STATUS_GROUPS.closed plus
#: closed_negative. Nothing further is expected to happen on these.
TERMINAL = ("completed", "lost", "cancelled")

#: Paused but still the business's work. STATUS_GROUPS.other.
PARKED = ("on_hold",)

#: Gone cold, worth approaching again later. STATUS_GROUPS.parked.
REENGAGE = ("reengage",)

#: Still moving. The pipeline the revenue forecast is built from.
PIPELINE = OPEN + CONFIRMED

#: The status a new job starts with.
DEFAULT = "quoting"


def is_valid(status):
    """True when `status` is one of JOB_STATUSES."""
    return status in JOB_STATUSES


def allowed_list():
    """The statuses as one comma-separated string, for error messages.

    Every place that refuses a bad status names the valid ones in the same
    message, so the caller can fix the value without looking it up.
    """
    return ", ".join(JOB_STATUSES)


def check(status, field="status"):
    """Return `status` unchanged if valid, otherwise raise ValueError.

    >>> check("won")      # 'won'
    >>> check("paid")     # ValueError: status 'paid' is not a job status. Allowed: quoting, ...

    Called before a write, so the caller gets a message listing the eleven
    allowed values instead of a PocketBase 400.
    """
    if not is_valid(status):
        raise ValueError(
            "%s %r is not a job status. Allowed: %s"
            % (field, status, allowed_list()))
    return status
