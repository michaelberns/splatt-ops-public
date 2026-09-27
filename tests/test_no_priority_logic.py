"""
Tests that priority is a visual aid only: no decision in the overdue
engine or the validator reads a task's priority.

The column a task sits in is what the engine acts on. Priority is painted
as the colour of the column when a task is moved and never read back, so
a priority the operator changes by hand has no effect on the engine. If
the engine escalated by priority, most of the board would soon be P1 and
the colour would carry no information.

Checked two ways:

  behaviour  the same task at every priority gets exactly the same
             treatment, apart from the colour written
  source     no line in the engine or validator modules reads a task's
             priority (a text scan, so it also covers code paths the
             behaviour tests do not reach)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daemon.overdue import OverdueEngine, Thresholds, plan_for
from daemon.waiting import WaitingRouter
from tests.conftest_splatt import TODAY, FakeNotifier, FakeSettings, task
from tests.fakes import FakeTodoist

ROOT = Path(__file__).resolve().parents[1]

#: Every priority Todoist has, as the API numbers them.
ALL_PRIORITIES = [1, 2, 3, 4]


def outcome(priority, days_late, content="do the thing", labels=None):
    """Everything the engine did, with the painted colour taken out.

    The colour is the only thing allowed to differ, and in practice it
    does not, because it depends on the column, not on the old priority.
    """
    tasks = [task(days_late=days_late, content=content, priority=priority,
                  labels=labels)]
    eng = OverdueEngine(FakeTodoist(tasks), FakeSettings(),
                        notifier=FakeNotifier())
    eng.run(today=TODAY)
    written = []
    for task_id, fields in eng.todoist.rescheduled:
        clean = {k: v for k, v in fields.items() if k != "priority"}
        written.append((task_id, sorted(clean.items())))
    return {
        "moved": list(eng.todoist.moved),
        "written": written,
        "decisions": [(a, r) for _, a, r in eng.needs_decision],
        "failures": list(eng.failures),
    }


# behaviour
@pytest.mark.parametrize("days_late,content", [
    (0, "not late yet"),
    (1, "one day late"),
    (3, "three days late"),
    (7, "a week late"),
    (14, "two weeks late"),
    (27, "nearly stalled"),
    (28, "stalled"),
    (49, "long dead"),
    (20, "awaiting client approval of QU-7994"),
    (20, "awaiting the Coastline freight ETA"),
])
def test_the_priority_a_task_arrives_with_changes_nothing(days_late, content):
    """The same task at P1, P2, P3 and P4 must be treated identically."""
    results = [outcome(p, days_late, content) for p in ALL_PRIORITIES]
    first = results[0]
    for other in results[1:]:
        assert other == first


def test_a_p4_task_still_climbs_the_ladder():
    """A task at the lowest priority (API 1, P4) still climbs the ladder
    to Stalled at 30 days."""
    eng_actions = outcome(1, 30)
    assert eng_actions["moved"] == [("6aaaaaaaaaaaaaaa", "stalled")]


def test_a_p1_task_is_not_treated_as_urgent_by_the_engine():
    assert outcome(4, 2)["moved"] == [("6aaaaaaaaaaaaaaa", "overdue")]


def test_the_colour_written_depends_on_the_column_not_on_what_was_there():
    """Whatever priority a task had, it ends up with the colour of the
    column it landed in (Overdue is p2, API 3)."""
    for before in ALL_PRIORITIES:
        tasks = [task(days_late=2, priority=before)]
        eng = OverdueEngine(FakeTodoist(tasks), FakeSettings())
        eng.run(today=TODAY)
        _, fields = eng.todoist.rescheduled[0]
        # p2, and the API numbers P2 as 3.
        assert fields["priority"] == 3


def test_the_engine_never_paints_p1():
    """The engine never writes P1 (API 4). P1 is left for the operator
    to mark the few tasks chosen for the day."""
    for days_late in (1, 3, 7, 14, 28, 60):
        for content in ("own work", "awaiting client reply", "awaiting supplier"):
            tasks = [task(days_late=days_late, content=content)]
            eng = OverdueEngine(FakeTodoist(tasks), FakeSettings())
            eng.run(today=TODAY)
            for _, fields in eng.todoist.rescheduled:
                assert fields.get("priority") != 4


def test_the_pure_decision_never_sees_a_priority_at_all():
    """plan_for does not look at priority: tasks that differ only in
    priority produce identical plans."""
    thresholds = Thresholds(FakeSettings())
    router = WaitingRouter(FakeSettings())
    plans = []
    for priority in ALL_PRIORITIES:
        one = task(days_late=15, priority=priority)
        one["_today"] = TODAY
        plan = plan_for(one, 15, router.classify(one), thresholds)
        plans.append((plan.column, plan.due, plan.clear_due,
                      tuple(plan.labels), plan.note, plan.alert, plan.decision))
    assert len(set(plans)) == 1


# source
ENGINE_FILES = [
    "daemon/overdue.py",
    "daemon/waiting.py",
    "core/planner.py",
    "core/realdue.py",
]

#: Reading a priority off a task. Writing one is fine, so an assignment
#: into a fields dict does not match, but a lookup does.
READ_PRIORITY = re.compile(
    r"""(\.get\(\s*["']priority["']|\[\s*["']priority["']\s*\]"""
    r"""|task\.priority|\.priority\b)""",
)

#: The one place the translation table lives, plus the function that uses
#: it. Both write, neither reads a task.
ALLOWED = ("PRIORITY_BY_LABEL", "api_priority", "priority.autotidy")


def test_no_engine_module_reads_a_task_priority():
    """No line in the engine modules reads a task's priority, apart
    from the translation table and api_priority, which only write."""
    offences = []
    for name in ENGINE_FILES:
        text = (ROOT / name).read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                continue
            if any(allow in line for allow in ALLOWED):
                continue
            if READ_PRIORITY.search(line):
                offences.append("%s:%d  %s" % (name, number, stripped))
    assert not offences, "priority is being read back:\n" + "\n".join(offences)


def test_no_validator_check_reads_a_task_priority():
    """No validator module reads a task's priority, so a run is never
    blocked because of how the operator coloured a task."""
    offences = []
    for path in sorted((ROOT / "validator").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                continue
            if any(allow in line for allow in ALLOWED):
                continue
            if READ_PRIORITY.search(line):
                offences.append("%s:%d  %s" % (
                    path.relative_to(ROOT), number, stripped
                ))
    assert not offences, "a validator check reads priority:\n" + "\n".join(offences)


#: Ways a rules file could turn priority into a condition. Setting one on
#: a task the validator raises is a write and is fine. Testing one is not.
PRIORITY_AS_A_CONDITION = re.compile(
    r"(min_priority|max_priority|priority_is|priority_at_least|if_priority"
    r"|priority_in|require_priority|expect_priority)",
    re.IGNORECASE,
)


def test_no_validator_rule_uses_priority_as_a_condition():
    rules = ROOT / "config" / "validation-rules.yaml"
    if not rules.exists():
        pytest.skip("no rules file")
    offending = [
        line.strip()
        for line in rules.read_text(encoding="utf-8").splitlines()
        if PRIORITY_AS_A_CONDITION.search(line)
        and not line.strip().startswith("#")
    ]
    assert not offending, (
        "a validator rule gates on priority:\n" + "\n".join(offending)
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
