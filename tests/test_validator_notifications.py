"""
Tests for validator/notifications.py: which ops-channel messages a run
produces, how the throttle counts what it holds back, and the heartbeat
behind silence_is_a_fault.

The focus is on the messages that are easy to lose: the one sent on a
clean run (without it a clean run and a stopped validator look the same),
and the ones the throttle decides not to send.

Nothing here talks to Telegram. The notifier is a recorder, and the tests
check which messages the dispatcher decides to produce.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from validator.notifications import (
    Dispatcher,
    quiet_for_hours,
    render,
    silence_check,
    write_heartbeat,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
RULES = REPO_ROOT / "config" / "validation-rules.yaml"


class FakeNotifier:
    """Records what it was asked to send. Can be told to fail."""

    def __init__(self, works=True):
        self.messages = []
        self.works = works
        self.last_error = "the fake was told to fail"

    def send(self, text):
        if not self.works:
            return False
        self.messages.append(text)
        return True


class FakeConf:
    def __init__(self, repo_root):
        self.repo_root = Path(repo_root)


def shipped_actions():
    """The actions block from the shipped rules file, so these tests also
    cover the configuration that is actually deployed."""
    data = yaml.safe_load(RULES.read_text(encoding="utf-8"))
    actions = dict(data["actions"])
    actions["_notifications"] = data.get("notifications") or {}
    return actions


def make(tmp_path, works=True):
    notifier = FakeNotifier(works=works)
    return notifier, Dispatcher(notifier, shipped_actions(), FakeConf(tmp_path), "run1")


# the clean-run message
def test_a_clean_run_still_sends(tmp_path):
    """No failures, no warnings, nothing changed, and on_pass is still sent."""
    notifier, d = make(tmp_path)
    d.on_pass({"run_id": "run1", "rules_passed": 0, "rule_count": 0, "warn_count": 0})
    assert len(notifier.messages) == 1
    assert "Validation passed" in notifier.messages[0]


def test_on_start_fires_before_anything_is_known(tmp_path):
    notifier, d = make(tmp_path)
    d.on_start({"run_id": "run1", "trigger": "test", "rule_count": 3})
    assert "Validation started" in notifier.messages[0]


def test_a_field_with_no_source_is_visibly_unsourced():
    """A placeholder with no value is shown as not recorded rather than
    filled with an invented value."""
    out = render("Run: {run_id}\nTook {duration_seconds}s", {"run_id": "abc"})
    assert "abc" in out
    assert "not recorded" in out


def test_a_stray_brace_does_not_cost_the_message():
    out = render("Rule: {rule_id} and a { loose brace", {"rule_id": "gate_won"})
    assert out


# the throttle
def test_eight_failures_do_not_send_eight_messages(tmp_path):
    notifier, d = make(tmp_path)
    for i, rule_id in enumerate(["gate_quoted"] * 3 + ["gate_won"] * 3
                                + ["gate_invoiced", "gate_paid"]):
        d.on_block({"run_id": "run1", "rule_id": rule_id, "record_id": "rec%d" % i})
    assert len(notifier.messages) == 3


def test_nothing_the_throttle_holds_back_is_lost(tmp_path):
    """Held back means counted, not dropped: messages sent plus messages
    held back equals messages requested."""
    notifier, d = make(tmp_path)
    raised = ["gate_quoted"] * 3 + ["gate_won"] * 3 + ["gate_invoiced", "gate_paid"]
    for i, rule_id in enumerate(raised):
        d.on_block({"run_id": "run1", "rule_id": rule_id, "record_id": "rec%d" % i})

    sent = len([h for h, _ in d.sent if h == "on_block"])
    assert sent + sum(d.held_back("on_block").values()) == len(raised)


def test_one_rule_failing_many_times_reads_as_one_problem(tmp_path):
    notifier, d = make(tmp_path)
    for i in range(20):
        d.on_block({"run_id": "run1", "rule_id": "gate_won", "record_id": "rec%d" % i})
    assert len(notifier.messages) == 1
    assert d.held_back("on_block") == {"gate_won": 19}


def test_the_summary_is_never_throttled(tmp_path):
    """on_block is capped, so the closing summary must be sent regardless."""
    notifier, d = make(tmp_path)
    for i in range(20):
        d.on_block({"run_id": "run1", "rule_id": "gate_won", "record_id": "rec%d" % i})
    d.on_block_summary({"run_id": "run1", "blocking_count": 20})
    assert "Validation finished BLOCKED" in notifier.messages[-1]


def test_blocks_and_warnings_are_counted_apart(tmp_path):
    notifier, d = make(tmp_path)
    for i in range(4):
        d.on_block({"run_id": "run1", "rule_id": "gate_won", "record_id": "r%d" % i})
        d.on_warn({"run_id": "run1", "rule_id": "schema_drift", "record_id": "r%d" % i})
    assert d.held_back("on_block") == {"gate_won": 3}
    assert d.held_back("on_warn") == {"schema_drift": 3}


def test_the_held_back_line_says_so_when_nothing_was(tmp_path):
    """The held-back line is never empty; with nothing held back it says so."""
    _, d = make(tmp_path)
    d.on_block({"run_id": "run1", "rule_id": "gate_won", "record_id": "r1"})
    assert "nothing" in d.held_back_line("on_block")


# the heartbeat, which is what silence_is_a_fault measures
def test_a_send_that_worked_writes_a_heartbeat(tmp_path):
    _, d = make(tmp_path)
    d.on_pass({"run_id": "run1"})
    assert (tmp_path / "state" / "validation-heartbeat.json").exists()


def test_a_send_that_failed_does_not(tmp_path):
    """A failed send must not refresh the heartbeat, otherwise expired
    Telegram credentials would look like a healthy validator."""
    _, d = make(tmp_path, works=False)
    d.on_pass({"run_id": "run1"})
    assert not (tmp_path / "state" / "validation-heartbeat.json").exists()
    assert d.failures


def test_never_having_sent_is_not_the_same_as_just_sent(tmp_path):
    """A missing heartbeat returns None, not zero: no message has ever
    been confirmed, which is treated as at least as bad as a stale one."""
    assert quiet_for_hours(FakeConf(tmp_path)) is None


def test_a_heartbeat_nobody_can_read_counts_as_never(tmp_path):
    path = tmp_path / "state" / "validation-heartbeat.json"
    path.parent.mkdir(parents=True)
    path.write_text("{ this is not json")
    assert quiet_for_hours(FakeConf(tmp_path)) is None


# silence_is_a_fault
def config_with(hours=24, enabled=True):
    return {"silence_is_a_fault": {"enabled": enabled, "max_quiet_hours": hours}}


def test_quiet_for_too_long_trips(tmp_path):
    conf = FakeConf(tmp_path)
    write_heartbeat(conf, "run1", "on_pass")
    later = datetime.now(timezone.utc) + timedelta(hours=30)
    tripped, message = silence_check(conf, config_with(), now=later)
    assert tripped
    assert "30" in message


def test_a_recent_message_does_not_trip(tmp_path):
    conf = FakeConf(tmp_path)
    write_heartbeat(conf, "run1", "on_pass")
    tripped, _ = silence_check(conf, config_with())
    assert not tripped


def test_having_never_sent_anything_trips(tmp_path):
    tripped, message = silence_check(FakeConf(tmp_path), config_with())
    assert tripped
    assert "never" in message.lower() or "no heartbeat" in message.lower()


def test_the_check_can_be_turned_off_in_the_yaml(tmp_path):
    tripped, _ = silence_check(FakeConf(tmp_path), config_with(enabled=False))
    assert not tripped


def test_the_shipped_rules_file_has_it_switched_on():
    """The shipped rules file has silence_is_a_fault enabled."""
    data = yaml.safe_load(RULES.read_text(encoding="utf-8"))
    assert data["notifications"]["silence_is_a_fault"]["enabled"] is True
