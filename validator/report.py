"""
Reports: one set of results rendered three ways.

  markdown  the full report for the operator, printed and saved to reports/
  json      what the agent parses to decide whether it may report success
  telegram  a short summary that fits in one message

The JSON keys are a contract with the agent, which reads the output and
not this code (tests/test_validator.py checks they stay present):

  run_id, at            which run, and when the report was made
  verdict               "PASSED", "PASSED WITH WARNINGS" or "BLOCKED"
  may_report_success    false when any block-severity rule failed
  counts                results per status, e.g. {"pass": 40, "fail": 2}
  blocking, warnings    failing results (rule_id, severity, status,
                        subject, summary, evidence, at)
  bypassed              failures waived by an active bypass
  skipped               checks that could not be evaluated. Not passes.
  exceptions            rules under a temporary exception: rule_id,
                        really (declared severity), treated_as, until,
                        days_left, reason
  notes                 run notes added by the checks

The markdown report groups failures by rule and prints each rule's
description and notes under it, so the reader sees what the rule is for
next to what went wrong.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from . import result

ICON = {
    result.PASS: "ok",
    result.FAIL: "FAIL",
    result.ERROR: "ERROR",
    result.SKIP: "skipped",
    result.BYPASSED: "bypassed",
}


def _group(results):
    grouped = {}
    for item in results:
        grouped.setdefault(item.rule_id, []).append(item)
    return grouped


def verdict(engine):
    """BLOCKED if anything blocking failed, PASSED WITH WARNINGS if only
    warn rules failed, otherwise PASSED."""
    if engine.blocking_failures():
        return "BLOCKED"
    if engine.warnings():
        return "PASSED WITH WARNINGS"
    return "PASSED"


def to_json(engine, run_id=""):
    return json.dumps(
        {
            "run_id": run_id or engine.ctx.run_id,
            "at": datetime.now(timezone.utc).isoformat(),
            "verdict": verdict(engine),
            "may_report_success": engine.may_report_success(),
            "counts": engine.counts(),
            "blocking": [r.to_dict() for r in engine.blocking_failures()],
            "warnings": [r.to_dict() for r in engine.warnings()],
            "bypassed": [r.to_dict() for r in engine.bypassed()],
            # Listed so the agent can say what could not be checked,
            # rather than implying it was fine.
            "skipped": [r.to_dict() for r in engine.skipped()],
            # Rules running softer than declared, with the date each one
            # reverts, so the agent can mention every active exception.
            "exceptions": _exceptions(engine),
            "notes": engine.ctx.notes,
        },
        indent=2,
    )


def _exceptions(engine):
    """Every rule under an active exception, with its end date and days left."""
    ruleset = getattr(engine, "ruleset", None)
    if not ruleset or not hasattr(ruleset, "under_exception"):
        return []
    out = []
    for rule in ruleset.under_exception():
        out.append(
            {
                "rule_id": rule.id,
                "really": rule.declared_severity,
                "treated_as": rule.severity,
                "until": str(rule.exception_until),
                "days_left": rule.exception_days_left(),
                "reason": " ".join(rule.exception_reason.split()),
            }
        )
    return out


def to_markdown(engine, run_id=""):
    lines = []
    lines.append("# Splatt ops validation report")
    lines.append("")
    lines.append("Run %s at %s" % (run_id or engine.ctx.run_id,
                                   datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")))
    lines.append("")
    lines.append("Verdict: **%s**" % verdict(engine))
    lines.append("")
    counts = engine.counts()
    lines.append("Checks run: %d. %s" % (
        len(engine.results),
        ", ".join("%s %s" % (v, k) for k, v in sorted(counts.items())),
    ))
    lines.append("")

    if engine.blocking_failures():
        lines.append("## Blocking")
        lines.append("")
        lines.append("The agent may not report success until these are resolved.")
        lines.append("")
        lines.extend(_section(engine.blocking_failures()))

    if engine.warnings():
        lines.append("## Warnings")
        lines.append("")
        lines.append("Not blocking, but do not let these accumulate.")
        lines.append("")
        lines.extend(_section(engine.warnings()))

    if engine.bypassed():
        lines.append("## Bypassed")
        lines.append("")
        lines.append("Waived by the operator. Each waiver expires and the check returns.")
        lines.append("")
        lines.extend(_section(engine.bypassed()))

    passes = [r for r in engine.results if r.status == result.PASS]
    if passes:
        lines.append("## Passed")
        lines.append("")
        for item in passes:
            suffix = " (%s)" % item.subject if item.subject else ""
            lines.append("- %s%s: %s" % (item.rule_id, suffix, item.summary))
        lines.append("")

    skips = [r for r in engine.results if r.status == result.SKIP]
    if skips:
        lines.append("## Not checked")
        lines.append("")
        lines.append("These could not be evaluated. A skip is not a pass.")
        lines.append("")
        for item in skips:
            suffix = " (%s)" % item.subject if item.subject else ""
            lines.append("- %s%s: %s" % (item.rule_id, suffix, item.summary))
        lines.append("")

    active = _exceptions(engine)
    if active:
        lines.append("## Temporary exceptions")
        lines.append("")
        lines.append(
            "These rules are being treated as softer than they are. Each one "
            "goes back to full strength on its own, on the date shown."
        )
        lines.append("")
        for item in active:
            lines.append(
                "- %s is really **%s**, treated as **%s** until %s, %d days left"
                % (item["rule_id"], item["really"], item["treated_as"],
                   item["until"], item["days_left"])
            )
            lines.append("  %s" % item["reason"])
        lines.append("")

    if engine.ctx.notes:
        lines.append("## Run notes")
        lines.append("")
        for note in engine.ctx.notes:
            lines.append("- %s" % note)
        lines.append("")

    return "\n".join(lines)


def _section(results):
    lines = []
    for rule_id, items in _group(results).items():
        rule = items[0].rule
        lines.append("### %s" % rule_id)
        lines.append("")
        description = getattr(rule, "description", "")
        if description:
            lines.append(description)
            lines.append("")
        for item in items:
            subject = " on %s" % item.subject if item.subject else ""
            lines.append("- **%s**%s: %s" % (ICON.get(item.status, item.status), subject, item.summary))
            for line in item.evidence[:12]:
                lines.append("    - %s" % line)
            if len(item.evidence) > 12:
                lines.append("    - and %d more" % (len(item.evidence) - 12))
        notes = getattr(rule, "notes", "")
        if notes:
            lines.append("")
            lines.append("Note: %s" % notes)
        lines.append("")
    return lines


def to_telegram(engine, run_id=""):
    """A short summary for one Telegram message: the verdict, up to ten
    blocking failures and up to six warnings, each with a count of the rest."""
    head = "Splatt validation %s" % verdict(engine)
    lines = [head, "run %s" % (run_id or engine.ctx.run_id), ""]
    blocking = engine.blocking_failures()
    warnings = engine.warnings()

    if blocking:
        lines.append("BLOCKING (%d)" % len(blocking))
        for item in blocking[:10]:
            subject = " [%s]" % item.subject if item.subject else ""
            lines.append("- %s%s: %s" % (item.rule_id, subject, item.summary))
        if len(blocking) > 10:
            lines.append("- and %d more" % (len(blocking) - 10))
        lines.append("")

    if warnings:
        lines.append("Warnings (%d)" % len(warnings))
        for item in warnings[:6]:
            subject = " [%s]" % item.subject if item.subject else ""
            lines.append("- %s%s: %s" % (item.rule_id, subject, item.summary))
        if len(warnings) > 6:
            lines.append("- and %d more" % (len(warnings) - 6))
        lines.append("")

    if not blocking and not warnings:
        lines.append("All %d checks clean." % len(engine.results))

    return "\n".join(lines)
