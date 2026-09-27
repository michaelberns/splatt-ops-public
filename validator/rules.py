"""
Rule loading: turns config/validation-rules.yaml into Rule objects and refuses a malformed file.

The three kinds of rule
    standing   checks that run on every validation pass, each backed by a
               named Python function (`check:`), for example "every write
               this run claimed actually landed in PocketBase".
    gates      state gates. Each one guards a job status (`on_transition_to`)
               or a boolean flag on a job (`on_flag_true`) and lists what
               must be true for a job in that state (`requires:`). Gates are
               evaluated against every non-archived job on every pass, not
               only at the moment a status changes.
    money      money gates. Same shape as the others, but they guard cash:
               freight recharged, supplier bills recharged, bills linked to
               a job. None of them can be bypassed.

Severity
    block   the agent may not report success while this rule fails
    warn    the agent may report success but must mention the failure

What is checked at load time
    Every rule needs an id (unique across the file), a severity of block
    or warn, a `check` or a `requires` list, and a `last_reviewed` date.
    A gate also needs `on_transition_to` or `on_flag_true`, otherwise
    nothing would ever run it. Any problem raises RuleError naming the
    rule, so a typo is found before a run rather than during one.

    Check names and requirement types are matched to code when the rule
    runs: an unknown name becomes an ERROR result (never a pass), and
    tests/test_validator.py fails if the shipped file names a check or a
    requirement type that is not registered.

Temporary exceptions
    Sometimes a rule is correct but the data is not ready for it yet, for
    example while a backfill is pending. A rule may then carry an
    `exception:` block that runs it at a softer severity for a stated
    reason until a stated date:

        exception:
          until: 2026-10-31      # at most MAX_EXCEPTION_DAYS from today
          severity: warn         # must differ from the declared severity
          reason: >              # at least MIN_REASON_LENGTH characters
            The client links are being backfilled; remove this afterwards.

    The day after `until` the rule reverts to its declared severity on
    its own. Exceptions are refused on rules marked `bypassable: false`.
    Every active exception, with its countdown, is printed in each report.

Why the validator is a separate package
    It shares core/ with the rest of the system (the PocketBase and Todoist
    clients, the schema) and nothing else. It never imports the daemon, so
    it checks the data rather than re-running the daemon's own logic.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import yaml

SEVERITIES = {"block", "warn"}
SECTIONS = ("standing", "gates", "money")

# The longest a temporary exception may run, counted from the day the
# rules are loaded. Long enough to plan a backfill around, short enough
# that an exception is always reviewed within a quarter.
MAX_EXCEPTION_DAYS = 90

# An exception must explain itself in a sentence someone can act on.
# A reason like "temp" is refused.
MIN_REASON_LENGTH = 25


class RuleError(Exception):
    """The rules file itself is wrong. The message names the rule and the problem."""


class Rule:
    """One rule from the YAML file.

    `raw` keeps the original mapping so a check can read its own extra
    keys (for example `options` on promises_are_guarded). `severity` is
    what the rule counts as today, which differs from `declared_severity`
    only while a temporary exception is active.
    """

    def __init__(self, section, raw):
        self.section = section
        self.raw = raw
        self.id = raw.get("id", "")
        self.version = raw.get("version", 0)
        self.description = (raw.get("description") or "").strip()
        # The severity written in the file. It never changes, so a report
        # can show both what the rule really is and what it is being
        # treated as while an exception is active.
        self.declared_severity = raw.get("severity", "warn")
        self.severity = self.declared_severity
        self.exception = raw.get("exception") or {}
        self.bypassable = bool(raw.get("bypassable", False))
        self.check = raw.get("check")
        self.requires = raw.get("requires") or []
        self.applies_when = raw.get("applies_when", "")
        self.on_transition_to = raw.get("on_transition_to")
        # A gate normally guards a status. Some claims are booleans instead:
        # "paid" is the `paid` flag plus `paid_amount` and `paid_at`, not a
        # status. on_flag_true names such a field, and the gate is then
        # evaluated on every job where that field is true.
        self.on_flag_true = raw.get("on_flag_true")
        self.on_fail = raw.get("on_fail") or {}
        self.tolerance = raw.get("tolerance") or {}
        self.notes = (raw.get("notes") or "").strip()
        self.last_reviewed = raw.get("last_reviewed")

    @property
    def blocking(self):
        return self.severity == "block"

    def message(self, fallback):
        """The rule's own on_fail message if it has one, else the fallback."""
        return self.on_fail.get("message") or fallback

    def transitions(self):
        """Normalise on_transition_to to a list of statuses, or ['any']."""
        value = self.on_transition_to
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return list(value)

    def applies_to_transition(self, status):
        targets = self.transitions()
        return "any" in targets or status in targets

    def flags(self):
        """Normalise on_flag_true to a list of field names."""
        value = self.on_flag_true
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return list(value)

    def applies_to_job(self, job):
        """Whether this gate should be evaluated against this job.

        True when the job is in one of the gate's statuses, or when any of
        the gate's flags is true on the job. A rule may carry both, which
        lets a gate move from a status to a flag without a gap.

        For example gate_paid has `on_flag_true: paid`, so it applies to
        {"status": "completed", "paid": True} and to a paid job in any
        other status, but not to an unpaid one.
        """
        if self.on_transition_to is not None:
            if self.applies_to_transition(str(job.get("status") or "")):
                return True
        for field in self.flags():
            if job.get(field):
                return True
        return False

    # temporary exceptions
    @property
    def exception_until(self):
        value = self.exception.get("until")
        return value if isinstance(value, date) else None

    @property
    def exception_reason(self):
        return (self.exception.get("reason") or "").strip()

    def exception_active(self, today=None):
        """True from the day the rules are loaded up to and including `until`."""
        until = self.exception_until
        if not until:
            return False
        return (today or date.today()) <= until

    def exception_expired(self, today=None):
        until = self.exception_until
        if not until:
            return False
        return (today or date.today()) > until

    def exception_days_left(self, today=None):
        until = self.exception_until
        if not until:
            return None
        return (until - (today or date.today())).days

    def apply_exception(self, today=None):
        """Set the severity this rule counts as today, and return it.

        While the exception is active the rule runs at the exception's
        softer severity. Once `until` has passed it runs at its declared
        severity again, without anyone editing the file.
        """
        if self.exception_active(today):
            self.severity = self.exception.get("severity", "warn")
        else:
            self.severity = self.declared_severity
        return self.severity

    def stale_after(self, days, today=None):
        """True if this rule has not been reviewed within `days` days."""
        if not isinstance(self.last_reviewed, date):
            return True
        today = today or date.today()
        return self.last_reviewed < today - timedelta(days=days)

    def __repr__(self):
        return "<Rule %s/%s %s>" % (self.section, self.id, self.severity)


class RuleSet:
    """Every rule in the file, validated, with exceptions applied for `today`.

    Also keeps the file's other top-level blocks: `meta`, `actions` (the
    notification hooks, read by validator/notifications.py) and
    `notifications` (channel routing and the silence_is_a_fault settings).
    """

    def __init__(self, path, today=None):
        self.path = Path(path)
        self.today = today or date.today()
        if not self.path.exists():
            raise RuleError("rules file not found at %s" % self.path)
        data = yaml.safe_load(self.path.read_text()) or {}
        self.meta = data.get("meta") or {}
        self.actions = data.get("actions") or {}
        self.notifications = data.get("notifications") or {}
        self.rules = []
        seen = set()
        for section in SECTIONS:
            for raw in data.get(section) or []:
                rule = Rule(section, raw)
                self._validate(rule, seen)
                self._validate_exception(rule, self.today)
                rule.apply_exception(self.today)
                seen.add(rule.id)
                self.rules.append(rule)
        if not self.rules:
            raise RuleError("no rules found in %s" % self.path)

    @staticmethod
    def _validate_exception(rule, today):
        """Refuse an exception that is missing an end date or a reason,
        runs too long, changes nothing, or sits on a non-bypassable rule.

        An exception whose date has already passed is accepted: it no
        longer applies, and refusing to load would stop the validator
        running at the moment the rule is back at full strength.
        """
        if not rule.exception:
            return
        where = "%s rule '%s' exception" % (rule.section, rule.id)
        until = rule.exception_until
        if not until:
            raise RuleError(
                "%s has no 'until' date. Every exception must end on a "
                "stated date." % where
            )
        if len(rule.exception_reason) < MIN_REASON_LENGTH:
            raise RuleError(
                "%s needs a reason of at least %d characters saying what has "
                "to happen before it can be removed." % (where, MIN_REASON_LENGTH)
            )
        if not rule.bypassable:
            raise RuleError(
                "%s is not allowed. The rule is marked bypassable: false "
                "because it protects money or a document, and an exception "
                "would be a side door around that." % where
            )
        severity = rule.exception.get("severity", "warn")
        if severity not in SEVERITIES:
            raise RuleError(
                "%s has severity '%s', expected one of %s"
                % (where, severity, sorted(SEVERITIES))
            )
        if severity == rule.declared_severity:
            raise RuleError(
                "%s does not change anything. It softens the rule to '%s', "
                "which is what it already is." % (where, severity)
            )
        latest = today + timedelta(days=MAX_EXCEPTION_DAYS)
        if until > latest:
            raise RuleError(
                "%s runs until %s, which is more than %d days out. Choose an "
                "earlier end date." % (where, until, MAX_EXCEPTION_DAYS)
            )

    @staticmethod
    def _validate(rule, seen):
        """Structural checks every rule must pass. See the module docstring."""
        where = "%s rule '%s'" % (rule.section, rule.id or "<no id>")
        if not rule.id:
            raise RuleError("%s has no id" % where)
        if rule.id in seen:
            raise RuleError("duplicate rule id '%s'" % rule.id)
        if rule.severity not in SEVERITIES:
            raise RuleError(
                "%s has severity '%s', expected one of %s"
                % (where, rule.severity, sorted(SEVERITIES))
            )
        if not rule.check and not rule.requires:
            raise RuleError("%s has neither a check nor a requires list" % where)
        if (rule.section == "gates" and rule.on_transition_to is None
                and rule.on_flag_true is None):
            raise RuleError(
                "%s is a gate but has neither on_transition_to nor "
                "on_flag_true, so nothing would ever run it" % where)
        if not isinstance(rule.last_reviewed, date):
            raise RuleError(
                "%s has no valid last_reviewed date. Every rule must record "
                "when a person last reviewed it." % where
            )

    # selection
    def standing(self):
        return [r for r in self.rules if r.section == "standing"]

    def gates(self):
        return [r for r in self.rules if r.section == "gates"]

    def money(self):
        return [r for r in self.rules if r.section == "money"]

    def under_exception(self, today=None):
        """Rules currently running softer than declared. Every report lists
        these with the number of days left."""
        today = today or self.today
        return [r for r in self.rules if r.exception_active(today)]

    def expired_exceptions(self, today=None):
        """Exceptions whose date has passed. The rules are already back at
        full strength; these are listed so the stale block can be deleted
        from the YAML."""
        today = today or self.today
        return [r for r in self.rules if r.exception_expired(today)]

    def by_id(self, rule_id):
        for rule in self.rules:
            if rule.id == rule_id:
                return rule
        return None

    def gates_for(self, status):
        return [r for r in self.gates() if r.applies_to_transition(status)]

    def review_after_days(self):
        """How old a rule's last_reviewed may be before rules_are_fresh warns."""
        return int(self.meta.get("review_after_days", 180))

    def stale_rules(self, today=None):
        days = self.review_after_days()
        return [r for r in self.rules if r.stale_after(days, today)]
