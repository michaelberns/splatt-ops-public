"""
Shared test helpers, imported by name.

Contents
    - Fixed values and fakes for the engine tests (overdue, waiting,
      triggers, tick-off and others): a fixed TODAY, the policy settings
      they depend on, and stand-ins for settings, the Telegram notifier
      and the write ledger.
    - `live_pocketbase()`, for the few tests that compare the code with a
      real PocketBase server and must skip cleanly when none is available.

Why not conftest.py
    pytest loads every conftest.py automatically for every test below it.
    Importing this module by name keeps the dependency visible in each
    test file that uses it, and stops one suite from silently relying on
    another suite's setup.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

#: A fixed Thursday, so business-day arithmetic (weekends skipped) gives
#: the same answer on every run.
TODAY = date(2026, 8, 27)

#: The policy values from config/settings.yaml, copied here so a failing
#: test points at a policy change rather than at a missing key.
DEFAULTS = {
    "overdue.reschedule_after_days": 1,
    "overdue.escalate_after_days": 3,
    "overdue.alert_after_days": 7,
    "overdue.stall_warning_days": 14,
    "overdue.stall_after_days": 28,
    "overdue.escalated_label": "escalated",
    "overdue.stall_warning_label": "stall-warning",
    "overdue.colours.today": "p1",
    "overdue.colours.overdue": "p2",
    "overdue.colours.waiting": "p3",
    "overdue.colours.stalled": "p4",
    "planner.park_time": "00:00",
    "planner.working_hours.start": 8,
    "planner.working_hours.end": 17,
    "planner.buffer_minutes": 15,
    "planner.search_days": 14,
    "planner.skip_weekends": True,
    "waiting.client_chase_business_days": 5,
    "waiting.supplier_chase_business_days": 3,
    "waiting.max_chases": 2,
    "priority.autotidy": False,
}


class FakeSettings:
    """Answers `get(dotted)` from DEFAULTS plus any overrides."""

    def __init__(self, overrides=None):
        self.values = dict(DEFAULTS)
        self.values.update(overrides or {})

    def get(self, dotted, default=None):
        return self.values.get(dotted, default)


class FakeNotifier:
    """Records messages instead of sending them. `works=False` makes every
    send report failure, like an unreachable Telegram."""

    def __init__(self, works=True):
        self.messages = []
        self.works = works
        self.last_error = "" if works else "telegram said no"

    def send(self, message, silent=False):
        self.messages.append(message)
        return self.works


class FakeLedger:
    """Collects write-ledger claims in a list instead of a file."""

    def __init__(self):
        self.entries = []

    def record(self, action, collection, record_id, fields, source, note=""):
        self.entries.append({
            "action": action,
            "collection": collection,
            "record_id": record_id,
            "fields": fields,
            "source": source,
            "note": note,
        })


def task(task_id="6aaaaaaaaaaaaaaa", days_late=0, content="do the thing",
         description="", priority=1, labels=None, section=None, due=None):
    """A Todoist-shaped task that is `days_late` past its real due date.

    The real due date is the date the task was first due, before any
    automatic rescheduling; the system pins it in the task description as
    a "Real due: YYYY-MM-DD" line. It is pinned here explicitly because
    these tests are about what the engines do with a task's age, not
    about how the age is worked out (tests/test_realdue.py covers that).
    """
    real = TODAY - timedelta(days=days_late)
    pin = "📌 Real due: %s | %d days late" % (real.isoformat(), days_late)
    body = "%s\n\n%s" % (pin, description) if description else pin
    out = {
        "id": task_id,
        "content": content,
        "description": body,
        "priority": priority,
        "labels": list(labels or []),
        "due": {"date": (due or real).isoformat() if hasattr(due or real, "isoformat")
                else due},
    }
    if section:
        out["section_id"] = section
    return out


def live_pocketbase():
    """An authenticated client for the configured PocketBase, or a skip.

    Used by the tests that compare the code with a real server. The test
    is skipped, with the reason shown, when:
      1. the settings or the admin credentials (PB_ADMIN_EMAIL,
         PB_ADMIN_PASSWORD, from the environment or the repo .env) are
         missing, or
      2. the server at the configured URL does not answer its health check.
    Credentials are checked first, so an unconfigured machine makes no
    network call. Wrong credentials against a reachable server still fail
    the test, because that is a real configuration error.

    The caller closes the returned client.
    """
    from core.config import ConfigError, settings
    from core.pb import PocketBaseClient

    try:
        conf = settings().pocketbase()
    except ConfigError as exc:
        pytest.skip("live PocketBase not configured: %s" % exc)
    pb = PocketBaseClient(**conf)
    if not pb.health():
        pb.close()
        pytest.skip("live PocketBase not reachable at %s" % conf["base_url"])
    pb.auth_admin()
    return pb
