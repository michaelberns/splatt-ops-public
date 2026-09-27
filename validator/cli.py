"""
Command line entry point for the validator.

Commands
    python -m validator run              full pass, prints and writes a report
    python -m validator run --standing   standing checks only (faster)
    python -m validator run --json       print the JSON report the agent reads
    python -m validator run --notify     also send the ops Telegram messages
    python -m validator heartbeat        has the ops channel gone quiet?
    python -m validator doctor           configuration and connectivity only
    python -m validator bypass <rule> --reason "..." [--subject ...]
    python -m validator rules            list the rules and when each was reviewed

bin/splatt-validate wraps `run --notify` and is what the agent calls at
the end of a piece of work.

Exit codes
    0  passed. Warnings may exist; the agent may report success but must
       mention them.
    1  blocked. At least one block-severity rule failed; the agent may not
       report success.
    2  the validator could not run: the rules file is invalid, PocketBase
       is unreachable, the configuration is incomplete, or the run crashed.

Exit 2 must never be read as a pass. It means nothing was checked, so the
agent has no evidence either way and must say the validation did not run.
A caller that only tests "non-zero means blocked" is safe; a caller that
treats anything other than 1 as success is not.

The JSON report (`--json`)
    verdict              "PASSED", "PASSED WITH WARNINGS" or "BLOCKED"
    may_report_success   false when anything blocking failed
    counts               number of results per status
    blocking, warnings   the failing results, each with rule_id, severity,
                         status, subject, summary and evidence
    bypassed             failures waived by an active bypass
    skipped              checks that could not be evaluated (not passes)
    exceptions           rules running under a temporary exception, with
                         their declared and current severity and days left
    notes                run notes, for example "Todoist not configured"
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from core.config import ConfigError, settings
from core.ledger import WriteLedger, ledger_path
from core.logging_setup import RUN_ID, setup
from core.pb import PocketBaseClient
from core.todoist import TodoistClient

from . import report
from .context import Context
from .engine import BypassStore, Engine
from .rules import RuleError, RuleSet

EXIT_OK = 0
EXIT_BLOCKED = 1
EXIT_BROKEN = 2


def _build(conf, want_todoist=True):
    """Connect to PocketBase (required) and Todoist (optional).

    A missing Todoist configuration is not fatal: the Todoist checks then
    report themselves as skipped rather than passed.
    """
    pb_conf = conf.pocketbase()
    pb = PocketBaseClient(**pb_conf)
    if not pb.health():
        raise ConfigError("PocketBase is not reachable at %s" % pb_conf["base_url"])
    pb.auth_admin()

    todoist = None
    if want_todoist:
        try:
            td_conf = conf.todoist()
            todoist = TodoistClient(
                td_conf["token"], td_conf["project_id"], td_conf["sections"], td_conf["timeout"]
            )
        except ConfigError:
            todoist = None
    return pb, todoist


def cmd_run(args, conf, log):
    """Run the rules, print the report, write it to reports/, and return 0, 1 or 2."""
    try:
        ruleset = RuleSet(conf.repo_root / conf.get("validator.rules_file"))
    except RuleError as exc:
        log.error("rules file is invalid: %s", exc)
        return EXIT_BROKEN

    # The start message is sent as soon as the rules have parsed and before
    # connecting to PocketBase. An unreachable database is the most common
    # way for a run to fail, and announcing the start first means such a
    # run still leaves a trace (on_start followed by on_complete).
    run_id = args.run_id or RUN_ID
    dispatcher = _dispatcher(conf, ruleset, run_id, log, args.notify)
    started = datetime.now(timezone.utc)
    trigger = args.trigger or "manual"
    rule_count = len(ruleset.standing()) if args.standing else len(ruleset.rules)

    if dispatcher:
        dispatcher.on_start(
            {
                "run_id": run_id,
                "trigger": trigger,
                "rule_count": rule_count,
                "timestamp": started.strftime("%H:%M · %d %b %Y UTC"),
            }
        )

    try:
        pb, todoist = _build(conf, want_todoist=not args.no_todoist)
    except Exception as exc:
        log.error("could not connect: %s", exc)
        if dispatcher:
            dispatcher.on_complete(
                {
                    "run_id": run_id,
                    "last_rule_id": "none, the run never reached a rule",
                    "exit_reason": "could not connect: %s" % exc,
                }
            )
        return EXIT_BROKEN

    ledger = None
    path = ledger_path(conf)
    if path.exists():
        ledger = WriteLedger(path, run_id=run_id)

    ctx = Context(
        pb=pb,
        todoist=todoist,
        ledger=ledger,
        settings=conf,
        project_root=conf.path("paths.projects"),
        started_at=datetime.now(timezone.utc),
        run_id=run_id,
    )
    bypasses = BypassStore(
        conf.repo_root / "state" / "bypasses.json",
        ttl_hours=int(conf.get("validator.bypass_ttl_hours", 72)),
    )
    engine = Engine(ruleset, ctx, bypasses)

    log.info("running %d rules", len(ruleset.rules))
    last_rule_id = ""
    try:
        if args.standing:
            engine.run_standing()
        else:
            engine.run_all()
    except Exception as exc:
        # Report the crash on the ops channel before returning, so a run
        # that died part way is distinguishable from one that never started.
        if engine.results:
            last_rule_id = engine.results[-1].rule_id
        if dispatcher:
            dispatcher.on_complete(
                {
                    "run_id": ctx.run_id,
                    "last_rule_id": last_rule_id or "none reached",
                    "exit_reason": "%s: %s" % (exc.__class__.__name__, exc),
                }
            )
        log.error("run failed after %s: %s", last_rule_id or "no rules", exc)
        pb.close()
        if todoist:
            todoist.close()
        return EXIT_BROKEN

    if args.json:
        print(report.to_json(engine, ctx.run_id))
    else:
        print(report.to_markdown(engine, ctx.run_id))

    report_dir = conf.repo_root / conf.get("validator.report_dir", "reports")
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = report_dir / ("validation-%s-%s.md" % (stamp, ctx.run_id))
    path.write_text(report.to_markdown(engine, ctx.run_id), encoding="utf-8")
    log.info("report written to %s", path)

    if dispatcher:
        _notify_outcome(dispatcher, engine, ctx, started, rule_count, path, log)

    pb.close()
    if todoist:
        todoist.close()

    verdict = report.verdict(engine)
    log.info("verdict %s", verdict)
    return EXIT_BLOCKED if engine.blocking_failures() else EXIT_OK


def _dispatcher(conf, ruleset, run_id, log, notify_enabled):
    """Build the notifications dispatcher, or return None if Telegram is not configured.

    A missing Telegram token is logged and the run continues, because the
    report and the exit code matter more than the message. With
    `notify_enabled` false the dispatcher still decides what it would send
    but does not send it.
    """
    from core import notify as notify_mod
    from .notifications import Dispatcher

    actions = dict(ruleset.actions or {})
    actions["_notifications"] = ruleset.notifications or {}
    try:
        notifier = notify_mod.from_settings(conf, ruleset.notifications.get("channel", "ops"))
    except ConfigError as exc:
        log.error("telegram not configured: %s", exc)
        return None
    return Dispatcher(notifier, actions, conf, run_id, log=log, enabled=notify_enabled)


def _notify_outcome(dispatcher, engine, ctx, started, rule_count, report_path, log):
    """Send the per-failure messages (throttled), then exactly one closing message.

    The closing message is on_block_summary when anything blocked and
    on_pass otherwise. on_pass is sent on every clean run, including one
    that found nothing at all, so a quiet channel always means the
    validator did not run rather than that it had nothing to say.
    """
    duration = (datetime.now(timezone.utc) - started).total_seconds()
    blocking = engine.blocking_failures()
    warnings = engine.warnings()
    passes = [r for r in engine.results if r.status == "pass"]
    checked = sorted({r.rule_id for r in engine.results})

    for res in blocking:
        rule = res.rule
        dispatcher.on_block(
            {
                "run_id": ctx.run_id,
                "rule_id": res.rule_id,
                "rule_version": getattr(rule, "version", "?"),
                "fail_message": res.summary,
                "collection": _collection_of(rule),
                "record_id": res.subject or "n/a",
                "bypassable": "yes" if res.bypassable else "no",
                "blocking_count": len(blocking),
            }
        )

    for res in warnings:
        dispatcher.on_warn(
            {
                "run_id": ctx.run_id,
                "rule_id": res.rule_id,
                "fail_message": res.summary,
                "collection": _collection_of(res.rule),
                "record_id": res.subject or "n/a",
            }
        )
    if blocking:
        blocked_rules = sorted({r.rule_id for r in blocking})
        dispatcher.on_block_summary(
            {
                "run_id": ctx.run_id,
                "blocking_count": len(blocking),
                "rules_blocked": len(blocked_rules),
                "rule_ids_blocked": ", ".join(blocked_rules),
                "rules_passed": len(passes),
                "rule_count": rule_count,
                "warn_count": len(warnings),
                "held_back_blocks": dispatcher.held_back_line("on_block"),
                "held_back_warnings": dispatcher.held_back_line("on_warn"),
                "actions_taken": _actions_taken(engine, report_path),
                "duration_seconds": "%.1f" % duration,
            }
        )
    else:
        # Only on a clean run. A blocked run's summary already carries the
        # held-back counts, so a second summary would repeat them.
        dispatcher.summarise_suppressed()
        dispatcher.on_pass(
            {
                "run_id": ctx.run_id,
                "rules_passed": len(passes),
                "rule_count": rule_count,
                "warn_count": len(warnings),
                "rule_ids_checked": ", ".join(checked) if checked else "none",
                "actions_taken": _actions_taken(engine, report_path),
                "duration_seconds": "%.1f" % duration,
            }
        )

    if dispatcher.failures:
        log.error("telegram sends failed: %s", " | ".join(dispatcher.failures))

    _print_receipts(dispatcher, log)


def _print_receipts(dispatcher, log):
    """Print the Telegram message ids of what was delivered, and save them.

    A message id can be matched against the channel, so the run output
    shows what was actually delivered rather than only what was attempted.
    The receipts are also written to state/last-telegram-receipts.json.
    """
    receipts = getattr(dispatcher.notifier, "receipts", None) or []
    if not receipts:
        if dispatcher.enabled:
            print("")
            print("Telegram: nothing was delivered. See the log for why.")
        return
    print("")
    print("Telegram delivery receipts, ops channel")
    hooks = [h for h, _ in dispatcher.sent]
    for hook, receipt in zip(hooks, receipts):
        first_line = (receipt.get("text") or "").splitlines()
        print("  %-12s message_id %-8s chat %s  %s"
              % (hook,
                 receipt.get("message_id"),
                 receipt.get("chat_title") or receipt.get("chat_id"),
                 first_line[0] if first_line else ""))

    # Saved so the exact text that reached the channel can be read back
    # later without re-rendering the templates.
    try:
        out = Path(dispatcher.conf.repo_root) / "state" / "last-telegram-receipts.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                [dict(r, hook=h) for h, r in zip(hooks, receipts)],
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception as exc:
        log.error("could not write receipts file: %s", exc)


def _collection_of(rule):
    """The collection to name in a message. Gates and money rules are
    evaluated against jobs. Standing checks span collections, so they
    report "various" rather than naming one that might be wrong."""
    section = getattr(rule, "section", "")
    if section in ("gates", "money"):
        return "jobs"
    return "various"


def _actions_taken(engine, report_path):
    """The "what was done" line for the closing message.

    Lists only what this run actually did. The validator is read-only, so
    on a clean run that is writing the report and nothing else.
    """
    done = ["report written to %s" % report_path.name]
    bypassed = engine.bypassed()
    if bypassed:
        done.append("%d bypass(es) applied" % len(bypassed))
    skipped = engine.skipped()
    if skipped:
        done.append("%d check(s) could not be evaluated" % len(skipped))
    done.append("no records changed, the validator is read only")
    return ", ".join(done)


def cmd_heartbeat(args, conf, log):
    """Check silence_is_a_fault: exit 1 when the ops channel has been quiet too long.

    "Quiet" is measured from state/validation-heartbeat.json, which
    validator/notifications.py rewrites every time Telegram accepts a
    validation message. Telegram's Bot API does not let a bot read its own
    channel history, so an accepted send is the closest available proof
    that a message arrived.

    Whatever schedules this command can treat exit 1 as a fault. The
    `on_trip` entries in the rules file are printed but not executed.
    """
    from .notifications import quiet_for_hours, silence_check

    try:
        ruleset = RuleSet(conf.repo_root / conf.get("validator.rules_file"))
    except RuleError as exc:
        log.error("rules file is invalid: %s", exc)
        return EXIT_BROKEN

    tripped, message = silence_check(conf, ruleset.notifications)
    hours = quiet_for_hours(conf)
    print("Ops channel heartbeat")
    print("  last message: %s" % ("never" if hours is None else "%.1f hours ago" % hours))
    print("  %s" % message)

    if not tripped:
        return EXIT_OK

    print("")
    print("SILENCE IS A FAULT. The validator has gone quiet.")

    # Print each on_trip entry as the rules file writes it, without this
    # code naming any action type. A new entry added to the YAML then
    # shows up here without a code change.
    cfg = (ruleset.notifications or {}).get("silence_is_a_fault") or {}
    for entry in cfg.get("on_trip") or []:
        if not isinstance(entry, dict):
            continue
        for kind, spec in entry.items():
            detail = spec.get("title", spec) if isinstance(spec, dict) else spec
            print("  The rules file asks for: %s, %s" % (kind, detail))
    print("")
    print("  None of those are carried out by this command. It reports the")
    print("  fault and exits 1. See the note at the top of")
    print("  validator/notifications.py for what is and is not wired.")
    return EXIT_BLOCKED


def cmd_doctor(args, conf, log):
    """Check secrets, PocketBase, Todoist, the rules file and the projects
    folder. Exit 0 if all are present, 2 with a list of problems if not."""
    problems = []
    missing = conf.missing_secrets()
    for name in missing:
        problems.append("secret %s is not set" % name)

    try:
        pb_conf = conf.pocketbase()
        pb = PocketBaseClient(**pb_conf)
        if pb.health():
            pb.auth_admin()
            print("PocketBase   ok   %s, %d collections"
                  % (pb_conf["base_url"], len(pb.collections())))
        else:
            problems.append("PocketBase not reachable at %s" % pb_conf["base_url"])
        pb.close()
    except Exception as exc:
        problems.append("PocketBase: %s" % exc)

    try:
        td_conf = conf.todoist()
        todoist = TodoistClient(td_conf["token"], td_conf["project_id"],
                                td_conf["sections"], td_conf["timeout"])
        tasks = todoist.tasks()
        print("Todoist      ok   project %s, %d open tasks"
              % (td_conf["project_id"], len(tasks)))
        todoist.close()
    except Exception as exc:
        problems.append("Todoist: %s" % exc)

    try:
        ruleset = RuleSet(conf.repo_root / conf.get("validator.rules_file"))
        stale = ruleset.stale_rules()
        print("Rules        ok   %d rules, %d stale" % (len(ruleset.rules), len(stale)))
        for rule in stale:
            problems.append("rule %s last reviewed %s" % (rule.id, rule.last_reviewed))
    except RuleError as exc:
        problems.append("Rules: %s" % exc)

    projects = conf.path("paths.projects")
    if projects.exists():
        print("Projects     ok   %s" % projects)
    else:
        problems.append("projects folder not found at %s" % projects)

    print("")
    if problems:
        print("Problems:")
        for line in problems:
            print("  - %s" % line)
        return EXIT_BROKEN
    print("Everything the validator needs is present.")
    return EXIT_OK


def cmd_bypass(args, conf, log):
    """Record a bypass for a bypassable rule. Refuses (exit 2) an unknown
    rule id or a rule marked `bypassable: false`."""
    ruleset = RuleSet(conf.repo_root / conf.get("validator.rules_file"))
    rule = ruleset.by_id(args.rule)
    if not rule:
        log.error("no rule with id '%s'", args.rule)
        return EXIT_BROKEN
    if not rule.bypassable:
        log.error(
            "rule '%s' is not bypassable. It guards money or a document, so "
            "waiving it would lose something.", args.rule
        )
        return EXIT_BROKEN
    store = BypassStore(
        conf.repo_root / "state" / "bypasses.json",
        ttl_hours=int(conf.get("validator.bypass_ttl_hours", 72)),
    )
    entry = store.grant(args.rule, args.reason, args.subject)
    print("Bypass recorded for %s%s" % (args.rule, " on %s" % args.subject if args.subject else ""))
    print("Reason: %s" % entry["reason"])
    print("Expires after %s hours." % conf.get("validator.bypass_ttl_hours", 72))
    return EXIT_OK


def cmd_rules(args, conf, log):
    """Print every rule with its severity, whether it can be waived, and its review date."""
    ruleset = RuleSet(conf.repo_root / conf.get("validator.rules_file"))
    days = ruleset.review_after_days()
    stale = {r.id for r in ruleset.stale_rules()}
    for section in ("standing", "gates", "money"):
        rules = [r for r in ruleset.rules if r.section == section]
        if not rules:
            continue
        print("")
        print(section.upper())
        for rule in rules:
            flag = "STALE" if rule.id in stale else "     "
            waive = "waivable" if rule.bypassable else "no waiver"
            print("  %s %-26s %-5s %-9s reviewed %s"
                  % (flag, rule.id, rule.severity, waive, rule.last_reviewed))
    print("")
    print("Review window is %d days." % days)
    return EXIT_OK


def main(argv=None):
    parser = argparse.ArgumentParser(prog="validator", description="Splatt ops validator")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="run the checks")
    run.add_argument("--standing", action="store_true", help="standing checks only")
    run.add_argument("--json", action="store_true", help="machine readable output")
    run.add_argument("--notify", action="store_true", help="send the ops Telegram messages")
    run.add_argument("--no-todoist", action="store_true", help="skip Todoist checks")
    run.add_argument("--run-id", dest="run_id", default="", help="scope the ledger to a run")
    run.add_argument(
        "--trigger",
        default="",
        help="what started this run, shown in the ops start message "
             "(manual, daemon, agent, cron). Defaults to manual.",
    )
    run.set_defaults(func=cmd_run)

    beat = sub.add_parser(
        "heartbeat",
        help="check whether the ops channel has gone quiet (silence_is_a_fault)",
    )
    beat.set_defaults(func=cmd_heartbeat)

    doctor = sub.add_parser("doctor", help="check config and connectivity")
    doctor.set_defaults(func=cmd_doctor)

    bypass = sub.add_parser("bypass", help="waive a bypassable rule")
    bypass.add_argument("rule")
    bypass.add_argument("--reason", required=True)
    bypass.add_argument("--subject", default="")
    bypass.set_defaults(func=cmd_bypass)

    rules = sub.add_parser("rules", help="list rules and when they were last reviewed")
    rules.set_defaults(func=cmd_rules)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_BROKEN

    try:
        conf = settings()
    except ConfigError as exc:
        print("Configuration problem: %s" % exc, file=sys.stderr)
        return EXIT_BROKEN

    log = setup("validator", log_dir=conf.repo_root / "logs",
                max_bytes=int(conf.get("daemon.log_max_bytes", 10485760)),
                backup_count=int(conf.get("daemon.log_backup_count", 5)))
    try:
        return args.func(args, conf, log)
    except ConfigError as exc:
        log.error("configuration problem: %s", exc)
        return EXIT_BROKEN


if __name__ == "__main__":
    sys.exit(main())
