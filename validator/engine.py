"""
The engine: runs every rule against the data and collects the results.

What it does
    The engine does not know what any rule checks. It decides the order,
    runs each rule, turns anything unexpected into a failure, applies
    bypasses, and answers the one question the agent asks:
    `may_report_success()`, which is true when no blocking rule failed.

Order of a full run (`run_all`)
    1. Standing checks, each a named function in checks/standing.py.
    2. State gates, for every non-archived job whose status (or flag) the
       gate guards. Gates without `gate_passed` dependencies run first.
    3. Money gates, either as a named check or as a `requires` list per job.

Guarantees
    1. A check that raises becomes an ERROR result, never a PASS. ERROR
       counts as a failure, so on a block rule it blocks.
    2. A rule whose `check:` names no registered function also becomes an
       ERROR when it runs, as does an unknown requirement type. The test
       suite (tests/test_validator.py) refuses a shipped rules file that
       names either, so such a rule is caught before it is deployed.
    3. A requirement that cannot be evaluated (for example Xero is not
       reachable from the validator) produces a SKIP, which is reported as
       "not checked" and never counted as a pass.

Bypasses
    The operator can waive a failing rule with
    `python -m validator bypass <rule_id> --reason "..." [--subject ...]`.
    A bypass is:
      explicit   granted only through that command, never by the agent's
                 own code path
      reasoned   `--reason` is required and is copied into the evidence
      recorded   stored in state/bypasses.json with who and when
      temporary  ignored once older than validator.bypass_ttl_hours
                 (72 by default), after which the failure returns
      limited    impossible on a rule marked `bypassable: false`. Those
                 rules guard money or documents, so the CLI refuses the
                 bypass and the engine ignores any stored one.
"""

from __future__ import annotations

import json
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import job_status

from . import result
from .checks import requirements, standing
from .rules import RuleSet


class BypassStore:
    """Bypasses on disk, one per (rule id, subject), each with a reason and a timestamp.

    An entry looks like:

        "gate_won|Line upgrade": {"rule_id": "gate_won",
                                   "subject": "Line upgrade",
                                   "reason": "client confirmed by phone",
                                   "by": "operator",
                                   "granted_at": "2026-09-01T02:00:00+00:00"}

    `active()` returns an entry only while it is younger than the ttl.
    Expired entries stay in the file but have no effect.
    """

    def __init__(self, path, ttl_hours=72):
        self.path = Path(path).expanduser()
        self.ttl = timedelta(hours=ttl_hours)
        self.data = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except json.JSONDecodeError:
                self.data = {}

    def _key(self, rule_id, subject=""):
        return "%s|%s" % (rule_id, subject or "")

    def active(self, rule_id, subject=""):
        """The bypass for this rule and subject if one exists and has not expired, else None."""
        entry = self.data.get(self._key(rule_id, subject))
        if not entry:
            return None
        granted = entry.get("granted_at")
        if not granted:
            return None
        try:
            when = datetime.fromisoformat(granted)
        except ValueError:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - when > self.ttl:
            return None
        return entry

    def grant(self, rule_id, reason, subject="", by="operator"):
        """Record a bypass and save the file. The caller (cli.cmd_bypass)
        has already refused rules that are not bypassable."""
        entry = {
            "rule_id": rule_id,
            "subject": subject,
            "reason": reason,
            "by": by,
            "granted_at": datetime.now(timezone.utc).isoformat(),
        }
        self.data[self._key(rule_id, subject)] = entry
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2))
        return entry

    def revoke(self, rule_id, subject=""):
        self.data.pop(self._key(rule_id, subject), None)
        self.path.write_text(json.dumps(self.data, indent=2))


class Engine:
    """Runs a RuleSet against a Context. `results` holds every Result."""

    def __init__(self, ruleset, context, bypasses=None):
        self.ruleset = ruleset
        self.ctx = context
        # Checks such as rules_freshness read the ruleset from the context,
        # and gate_passed requirements call back into _resolve_gate.
        self.ctx.ruleset = ruleset
        self.ctx.gate_resolver = self._resolve_gate
        self.bypasses = bypasses
        self.results = []
        self._gate_cache = {}
        self._resolving = set()

    # entry points
    def run_all(self):
        self.results = []
        self.run_standing()
        self.run_gates()
        self.run_money()
        return self.results

    def run_standing(self):
        for rule in self.ruleset.standing():
            self._collect(self._run_named_check(rule))
        return self.results

    def run_gates(self):
        """Evaluate every gate against every non-archived job it applies to.

        This runs on every pass, not only when a status changes, so a job
        that is sitting in a status without the evidence that status needs
        keeps being reported until it is fixed.
        """
        jobs = [j for j in self.ctx.records("jobs") if not j.get("archived")]
        # A gate_passed requirement reads the cached outcome of the gate it
        # names, so gates without such dependencies run first.
        ordered = sorted(
            self.ruleset.gates(),
            key=lambda r: any(
                req.get("type") == "gate_passed" for req in r.requires
            ),
        )
        for rule in ordered:
            for job in jobs:
                if not rule.applies_to_job(job):
                    continue
                self._collect(self._run_requirements(rule, job))
        return self.results

    def run_money(self):
        """Money rules with a `check:` run once; the rest run per matching job."""
        jobs = [j for j in self.ctx.records("jobs") if not j.get("archived")]
        for rule in self.ruleset.money():
            if rule.check:
                self._collect(self._run_named_check(rule))
                continue
            for job in jobs:
                if not self._applies(rule, job):
                    continue
                self._collect(self._run_requirements(rule, job))
        return self.results

    # rule execution
    def _run_named_check(self, rule):
        """Run a rule's named check. An unknown name or an exception becomes ERROR."""
        fn = standing.REGISTRY.get(rule.check)
        if not fn:
            return result.errored(
                rule,
                "check '%s' is named in the rules but not implemented" % rule.check,
            )
        try:
            return fn(rule, self.ctx)
        except Exception as exc:
            return result.errored(
                rule,
                "check '%s' raised %s" % (rule.check, exc.__class__.__name__),
                [str(exc), traceback.format_exc(limit=3)],
            )

    def _run_requirements(self, rule, subject):
        """Evaluate every requirement of a gate or money rule against one job.

        Each requirement returns (ok, detail) where ok is True, False, or
        None for "could not check". Any False makes the result FAIL; with
        no False but at least one None the result is SKIP; otherwise PASS.
        The outcome is cached so later gate_passed requirements can use it.
        """
        subject_label = subject.get("title") or subject.get("id")
        failures = []
        unchecked = []
        evidence = []
        for spec in rule.requires:
            try:
                ok, detail = requirements.evaluate(spec, subject, self.ctx, self._gate_cache)
            except requirements.UnknownRequirement as exc:
                return result.errored(rule, str(exc), subject=subject_label)
            except Exception as exc:
                return result.errored(
                    rule,
                    "requirement '%s' raised %s" % (spec.get("type"), exc.__class__.__name__),
                    [str(exc)],
                    subject=subject_label,
                )
            line = "%s: %s" % (spec.get("type"), detail)
            evidence.append(line)
            if ok is False:
                failures.append(line)
            elif ok is None:
                unchecked.append(line)

        key = (rule.id, subject.get("id"))
        self._gate_cache[key] = not failures

        if failures:
            summary = rule.message(
                "%s failed for '%s'" % (rule.id, subject_label)
            )
            return result.failed(rule, summary, failures + unchecked, subject=subject_label)
        if unchecked:
            return result.Result(
                rule,
                result.SKIP,
                "%s could not be fully checked for '%s'" % (rule.id, subject_label),
                unchecked,
                subject=subject_label,
            )
        return result.passed(rule, "%s passed" % rule.id, evidence, subject=subject_label)

    def _resolve_gate(self, gate_id, subject):
        """Evaluate another gate's requirements against a job, on demand.

        Needed because gates run against the status a job is in. gate_won
        requires gate_quoted, but a won job is no longer in quoted, so
        gate_quoted never ran for it. This evaluates it now instead.

        Returns (ok, detail). Nothing is added to self.results, because
        this is a lookup on behalf of another rule. A circular dependency
        in the rules file returns None ("could not check") instead of
        recursing forever.
        """
        key = (gate_id, subject.get("id"))
        if key in self._gate_cache:
            return self._gate_cache[key], "cached"
        if key in self._resolving:
            return None, "circular dependency on %s in the rules file" % gate_id

        rule = self.ruleset.by_id(gate_id)
        if not rule:
            return None, "no rule with id '%s'" % gate_id

        self._resolving.add(key)
        try:
            failures = []
            for spec in rule.requires:
                if spec.get("type") == "gate_passed":
                    continue
                try:
                    ok, detail = requirements.evaluate(spec, subject, self.ctx, self._gate_cache)
                except Exception as exc:
                    return None, "%s raised %s" % (spec.get("type"), exc.__class__.__name__)
                if ok is False:
                    failures.append("%s: %s" % (spec.get("type"), detail))
        finally:
            self._resolving.discard(key)

        passed = not failures
        self._gate_cache[key] = passed
        return passed, " and ".join(failures) if failures else "all requirements met"

    def _applies(self, rule, job):
        """Whether a money rule's `applies_when` matches this job.

        `applies_when` is written for a person to read. Rather than build an
        expression language, the two conditions in use are recognised by
        the field they mention:

          has_shipping    the job has shipping and is won or further along
                          (core.job_status.CONFIRMED, plus completed)
          supplier_paid   the job's supplier has been paid and the job is
                          not cancelled

        Anything else applies to every job. Adding a condition means adding
        a branch here, so every filter in use is visible in code and tested.
        """
        expression = (rule.applies_when or "").strip()
        if not expression:
            return True
        status = str(job.get("status") or "")
        if "has_shipping" in expression:
            if not job.get("has_shipping"):
                return False
            # The status list comes from core.job_status so it stays in step
            # with the canonical list of job statuses.
            return status in job_status.CONFIRMED + ("completed",)
        if "supplier_paid" in expression:
            return bool(job.get("supplier_paid")) and status != "cancelled"
        return True

    # bypass
    def _collect(self, res):
        """Add a result (or list of results), converting a failure to BYPASSED
        when the rule is bypassable and an unexpired bypass exists for it."""
        if res is None:
            return
        items = res if isinstance(res, list) else [res]
        for item in items:
            if item.failed and item.bypassable and self.bypasses:
                waiver = self.bypasses.active(item.rule_id, item.subject)
                if waiver:
                    item.status = result.BYPASSED
                    item.add(
                        "bypassed by %s: %s" % (waiver.get("by"), waiver.get("reason"))
                    )
            self.results.append(item)

    # verdict
    def blocking_failures(self):
        return [r for r in self.results if r.blocking]

    def warnings(self):
        return [r for r in self.results if r.failed and not r.blocking]

    def bypassed(self):
        return [r for r in self.results if r.status == result.BYPASSED]

    def skipped(self):
        """Checks that could not be evaluated. Reported as such, never as passes."""
        return [r for r in self.results if r.status == result.SKIP]

    def may_report_success(self):
        """The question the agent asks: true when no blocking rule failed."""
        return not self.blocking_failures()

    def counts(self):
        """Number of results per status, for example {"pass": 40, "fail": 2}."""
        out = {}
        for item in self.results:
            out[item.status] = out.get(item.status, 0) + 1
        return out


def build_ruleset(path):
    return RuleSet(path)
