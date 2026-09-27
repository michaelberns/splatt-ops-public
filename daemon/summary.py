"""
The daily summary: five numbers about the board, printed and optionally
sent to Telegram.

    python -m daemon summary            work the numbers out and print them
    python -m daemon summary --send     print them and send them to Telegram

The numbers (see `numbers`)
    active            open tasks in the Todoist project
    completed_today   tasks completed since local midnight
    overdue           open tasks past their due date
    needing_decision  overdue tasks whose "auto rescheduled" count has
                      reached overdue.max_auto_reschedules
    unlinked          open assignments with no client, which cannot be
                      found by client on the dashboard

The numbers are read fresh on every run rather than kept in counters, so
they cannot drift, and nothing here is expensive enough to cache.

"Today" starts at local midnight in `meta.timezone` (Pacific/Auckland by
default), not at UTC midnight. Auckland is twelve or thirteen hours ahead
of UTC, so a UTC day would count the previous evening's work as today's.

Exit codes: 0 printed (and sent, with --send), 1 the Telegram send
failed, 2 PocketBase unreachable.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from core import alerts
from core.notify import from_settings as notifier_from_settings
from core.pb import PocketBaseClient
from core.todoist import TodoistClient

from .overdue import reschedule_count

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_BROKEN = 2


def day_start(zone_name, now=None):
    """Local midnight at the start of today in `zone_name`, returned in UTC.

    Returned in UTC because that is what Todoist's completed-tasks
    endpoint takes. For example, 09:00 on 8 Aug in Auckland gives
    2026-08-07 12:00 UTC (midnight on the 8th, local winter time).
    """
    zone = ZoneInfo(zone_name)
    local = (now or datetime.now(zone)).astimezone(zone)
    return datetime.combine(local.date(), time.min, tzinfo=zone).astimezone(
        ZoneInfo("UTC"))


def late(task):
    """Whether a task is past its due date.

    A task with no due date gets None from `days_overdue`, which is
    treated as not late.
    """
    days = TodoistClient.days_overdue(task)
    return days is not None and days > 0


def numbers(tasks, finished, records, max_pushes):
    """The five counts, worked out from what was read. No network.

    `tasks` are the open Todoist tasks, `finished` the tasks completed
    today, `records` the PocketBase assignments, and `max_pushes` the
    push count at which an overdue task counts as needing a decision.
    Open assignments with no client are counted because they cannot be
    found by client on the dashboard, so they need filing.
    """
    open_records = [r for r in records
                    if r.get("status") != "completed" and not r.get("archived")]
    return {
        "active": len(tasks),
        "completed_today": len(finished),
        "overdue": sum(1 for t in tasks if late(t)),
        "needing_decision": sum(1 for t in tasks
                                if late(t) and reschedule_count(t) >= max_pushes),
        "unlinked": sum(1 for r in open_records if not r.get("client")),
    }


def cmd_summary(args, conf, log):
    """Print the summary and, with --send, post it to the Telegram general
    channel. Returns an exit code (see the module docstring)."""
    td = conf.todoist()
    todoist = TodoistClient(td["token"], td["project_id"], td["sections"], td["timeout"])
    pb = PocketBaseClient(**conf.pocketbase())

    if not pb.health():
        log.error("PocketBase is not reachable")
        todoist.close()
        return EXIT_BROKEN
    pb.auth_admin()

    zone = conf.get("meta.timezone", "Pacific/Auckland")
    since = day_start(zone)
    tasks = todoist.tasks()
    try:
        finished = todoist.completed_since(since.isoformat())
    except Exception as exc:
        # The completed count falls back to zero. The warning in the log
        # is the only sign that the number is incomplete.
        log.warning("could not read completed tasks: %s", exc)
        finished = []

    counts = numbers(tasks, finished, pb.list_all("assignments"),
                     int(conf.get("overdue.max_auto_reschedules", 3)))
    message = alerts.daily(**counts)

    print(message.replace("<b>", "").replace("</b>", ""))
    print("")
    print("Day counted from %s local, which is %s UTC."
          % (zone, since.strftime("%Y-%m-%d %H:%M")))

    if not args.send:
        print("Nothing was sent. Pass --send to put this on Telegram.")
        todoist.close()
        pb.close()
        return EXIT_OK

    notifier = notifier_from_settings(conf, channel="general")
    sent = notifier.send(message)
    notifier.close()
    if not sent:
        print("Telegram would not take it: %s" % notifier.last_error)

    todoist.close()
    pb.close()
    return EXIT_OK if sent else EXIT_PARTIAL
