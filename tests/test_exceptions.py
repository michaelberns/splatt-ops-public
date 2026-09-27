"""
Tests for temporary exceptions (see validator/rules.py).

A temporary exception runs a rule at a softer severity until a stated
date. These tests check the guarantees the loader gives:

  - an exception must have an end date, at most MAX_EXCEPTION_DAYS ahead
  - it must give a reason of at least MIN_REASON_LENGTH characters
  - it must actually change the severity
  - it is refused on a rule marked bypassable: false
  - after the end date the rule reverts to its declared severity on its
    own, and the expired entry is listed so it can be removed

Every test builds its own small rules file, so none depends on what the
shipped config/validation-rules.yaml contains, except the two tests at the
end that check the shipped file on purpose.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from validator.rules import MAX_EXCEPTION_DAYS, RuleError, RuleSet

TODAY = date(2026, 8, 8)
RULES = Path(__file__).resolve().parents[1] / "config" / "validation-rules.yaml"

REASON = (
    "the backfill has not run yet and blocking every run until it does "
    "would make the validator unusable"
)


def write_rules(tmp_path, exception_yaml, severity="block", bypassable=True):
    """A one-rule rules file. exception_yaml is appended under the rule."""
    path = tmp_path / "rules.yaml"
    path.write_text(
        "meta: {version: 1}\n"
        "standing:\n"
        "  - id: x\n"
        "    severity: %s\n"
        "    bypassable: %s\n"
        "    check: rules_freshness\n"
        "    last_reviewed: 2026-08-08\n"
        "%s" % (severity, "true" if bypassable else "false", exception_yaml)
    )
    return path


def good(until, severity="warn", reason=REASON):
    """A well-formed exception block."""
    return (
        "    exception:\n"
        "      until: %s\n"
        "      severity: %s\n"
        "      reason: %s\n" % (until, severity, reason)
    )


# while it holds
def test_a_live_exception_softens_the_rule(tmp_path):
    ruleset = RuleSet(write_rules(tmp_path, good("2026-09-30")), today=TODAY)
    rule = ruleset.by_id("x")
    assert rule.declared_severity == "block"
    assert rule.severity == "warn"
    assert rule.exception_active(TODAY)
    assert not rule.blocking


def test_the_rule_still_knows_what_it_really_is(tmp_path):
    ruleset = RuleSet(write_rules(tmp_path, good("2026-09-30")), today=TODAY)
    rule = ruleset.by_id("x")
    assert rule.declared_severity == "block"
    assert rule.severity == "warn"


def test_the_countdown_is_available_so_it_can_be_printed(tmp_path):
    ruleset = RuleSet(write_rules(tmp_path, good("2026-08-18")), today=TODAY)
    assert ruleset.by_id("x").exception_days_left(TODAY) == 10


def test_a_rule_under_exception_is_listed_for_the_report(tmp_path):
    ruleset = RuleSet(write_rules(tmp_path, good("2026-09-30")), today=TODAY)
    assert [r.id for r in ruleset.under_exception(TODAY)] == ["x"]


def test_the_reason_is_kept_for_the_report(tmp_path):
    """The report prints the reason next to the countdown, so it is kept
    in full (surrounding whitespace removed)."""
    ruleset = RuleSet(write_rules(tmp_path, good("2026-09-30")), today=TODAY)
    assert ruleset.by_id("x").exception_reason == REASON


# the day it lapses
def test_the_rule_comes_back_on_its_own_the_day_after(tmp_path):
    """The exception applies up to and including `until`, and the rule is
    back at its declared severity the next day without any edit."""
    path = write_rules(tmp_path, good("2026-08-08"))

    on_the_day = RuleSet(path, today=date(2026, 8, 8))
    assert on_the_day.by_id("x").severity == "warn"

    the_day_after = RuleSet(path, today=date(2026, 8, 9))
    assert the_day_after.by_id("x").severity == "block"


def test_an_expired_exception_is_listed_so_the_dead_entry_gets_removed(tmp_path):
    ruleset = RuleSet(write_rules(tmp_path, good("2026-07-01")), today=TODAY)
    assert [r.id for r in ruleset.expired_exceptions(TODAY)] == ["x"]
    assert ruleset.by_id("x").severity == "block"


def test_an_expired_exception_does_not_stop_the_validator_loading(tmp_path):
    """An expired exception no longer applies, so it is accepted. Refusing
    to load would stop the validator at the moment the rule is back at
    full strength."""
    ruleset = RuleSet(write_rules(tmp_path, good("2026-01-01")), today=TODAY)
    assert ruleset.by_id("x").severity == "block"


# badly written exceptions are refused at load time
def test_an_exception_with_no_end_date_is_refused(tmp_path):
    path = write_rules(
        tmp_path, "    exception:\n      severity: warn\n      reason: %s\n" % REASON
    )
    with pytest.raises(RuleError) as caught:
        RuleSet(path, today=TODAY)
    assert "until" in str(caught.value)


def test_an_end_date_too_far_out_is_refused(tmp_path):
    far = TODAY + timedelta(days=MAX_EXCEPTION_DAYS + 1)
    with pytest.raises(RuleError) as caught:
        RuleSet(write_rules(tmp_path, good(far)), today=TODAY)
    assert "days out" in str(caught.value)


def test_an_end_date_at_the_limit_is_allowed(tmp_path):
    edge = TODAY + timedelta(days=MAX_EXCEPTION_DAYS)
    ruleset = RuleSet(write_rules(tmp_path, good(edge)), today=TODAY)
    assert ruleset.by_id("x").severity == "warn"


def test_a_reason_nobody_could_act_on_is_refused(tmp_path):
    with pytest.raises(RuleError) as caught:
        RuleSet(write_rules(tmp_path, good("2026-09-01", reason="temp")), today=TODAY)
    assert "reason" in str(caught.value)


def test_an_exception_that_changes_nothing_is_refused(tmp_path):
    """Softening a warn rule to warn has no effect, so it is refused
    rather than left in the file looking like a decision."""
    with pytest.raises(RuleError) as caught:
        RuleSet(write_rules(tmp_path, good("2026-09-01", severity="warn"),
                            severity="warn"), today=TODAY)
    assert "does not change anything" in str(caught.value)


def test_a_nonsense_severity_is_refused(tmp_path):
    with pytest.raises(RuleError):
        RuleSet(write_rules(tmp_path, good("2026-09-01", severity="ignore")), today=TODAY)


def test_a_rule_that_cannot_be_waived_cannot_be_excepted_either(tmp_path):
    """A rule marked bypassable: false protects money or a document. An
    exception would soften it for weeks, so it is refused the same way a
    bypass is."""
    with pytest.raises(RuleError) as caught:
        RuleSet(write_rules(tmp_path, good("2026-09-01"), bypassable=False),
                today=TODAY)
    assert "side door" in str(caught.value)


# the shipped file
def test_the_shipped_rules_file_has_no_exception_in_force():
    """The shipped file carries no temporary exception. Adding one means
    changing this test too, so the decision shows up in a reviewed diff."""
    ruleset = RuleSet(RULES, today=TODAY)
    assert ruleset.under_exception(TODAY) == []
    assert [r.id for r in ruleset.rules if r.exception] == []


def test_no_money_rule_may_hide_behind_an_exception():
    """Money rules are not bypassable, so they must not carry an exception
    either."""
    ruleset = RuleSet(RULES, today=TODAY)
    softened = [r.id for r in ruleset.money() if r.exception]
    assert not softened, "money rules must not have exceptions: %s" % softened


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
