"""
The Result object: what every check hands back to the engine.

A result carries a status and the evidence behind it, so a failure in the
report says exactly which record is wrong and why, and the operator can
act on it without re-running anything.

Statuses
    pass      the check looked and found nothing wrong
    fail      the check looked and found a problem
    error     the check itself broke (raised, or named something that does
              not exist). Treated exactly like a failure.
    skip      the check could not look, for example because Todoist was not
              configured. Never counted as a pass, and listed separately in
              the report.
    bypassed  a failure that the operator explicitly waived (see engine.py)

Whether a failure stops the agent depends on the rule's severity: only a
`fail` or `error` on a `block` rule is blocking. On a `warn` rule the agent
may still report success but has to mention it.
"""

from __future__ import annotations

from datetime import datetime, timezone

PASS = "pass"
FAIL = "fail"
SKIP = "skip"
ERROR = "error"
BYPASSED = "bypassed"


class Result:
    """One outcome of one rule, optionally about one subject (for a gate,
    the job title). Severity and bypassable are copied from the rule so
    the result can be judged on its own. `evidence` is a list of
    human-readable lines."""

    def __init__(self, rule, status, summary="", evidence=None, subject=""):
        self.rule = rule
        self.rule_id = getattr(rule, "id", str(rule))
        self.severity = getattr(rule, "severity", "warn")
        self.bypassable = getattr(rule, "bypassable", False)
        self.status = status
        self.summary = summary
        self.evidence = list(evidence or [])
        self.subject = subject
        self.at = datetime.now(timezone.utc).isoformat()

    @property
    def blocking(self):
        """True for a fail or error on a block rule. This is what stops the
        agent reporting success. A skip is not a failure, and a bypassed
        result has already been waived, so neither is blocking."""
        return self.status in (FAIL, ERROR) and self.severity == "block"

    @property
    def failed(self):
        return self.status in (FAIL, ERROR)

    def add(self, line):
        self.evidence.append(str(line))
        return self

    def to_dict(self):
        return {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "status": self.status,
            "subject": self.subject,
            "summary": self.summary,
            "evidence": self.evidence,
            "at": self.at,
        }

    def __repr__(self):
        return "<Result %s %s>" % (self.rule_id, self.status)


def passed(rule, summary="", evidence=None, subject=""):
    return Result(rule, PASS, summary, evidence, subject)


def failed(rule, summary, evidence=None, subject=""):
    return Result(rule, FAIL, summary, evidence, subject)


def skipped(rule, summary, subject=""):
    return Result(rule, SKIP, summary, subject=subject)


def errored(rule, summary, evidence=None, subject=""):
    """The check itself broke. Counted as a failure, never as a pass,
    because a check that could not run has not shown that anything is fine."""
    return Result(rule, ERROR, summary, evidence, subject)
