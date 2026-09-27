"""
Command line for the daemon: `python -m daemon <command>`.

The daemon keeps the Todoist board (one Todoist project, split into seven
sections such as Today, Overdue and Waiting on Client) and the PocketBase
`assignments` collection in step, and it runs the housekeeping nobody
should have to remember. Every pass is also a command of its own, so it
can be run once by hand, and its plan read, before it runs on a timer.

Commands
    python -m daemon sweep [--dry-run] [--quiet] [--json]
        The overdue sweep (daemon/overdue.py). Acts unless given --dry-run.
    python -m daemon sync [--write] [--json] [--quiet]
        Forward sync, Todoist to PocketBase (daemon/sync.py).
    python -m daemon reverse [--write] [--json] [--quiet]
        Reverse sync, dashboard edits in PocketBase to Todoist
        (daemon/reverse.py).
    python -m daemon triggers [--write] [--json] [--quiet]
        Lets a finished task move a job's status, and creates or closes
        quote chase tasks (daemon/triggers.py).
    python -m daemon prune [--write] [--json]
        Deletes sync log records older than the keep window
        (daemon/prune.py).
    python -m daemon loop [--write] [--once] [--verbose]
        All of the passes on a timer (daemon/loop.py). The permanent
        process is `python -m daemon loop --write`.
    python -m daemon summary [--send]
        The daily numbers, printed, and sent to Telegram with --send
        (daemon/summary.py).

    --json prints the plan as data, --quiet sends nothing to Telegram.

Which commands write by default
    sync, reverse, triggers, prune and loop write nothing unless given
    --write. They cover the whole board, so the safe default is to print
    the plan and stop. The sweep is the exception: it is a single,
    deliberate command, so it acts unless given --dry-run. On a new
    installation the first thing to run is `python -m daemon sweep
    --dry-run`, and the end of a dry run says how to apply it.

Safety rules shared by the writing passes
    - sync, reverse and triggers refuse to write unless tools/backup.py
      has a snapshot from today (UTC) in backups/. The loop takes that
      snapshot itself before its first write of the day.
    - sync and reverse export the before-state of every record they are
      about to change to backups/<pass>-<run id>.json before the first
      write, so a bad pass can be undone by hand.
    - PocketBase writes go through `core.ledger.RecordingClient`, which
      appends each one to the write ledger (core/ledger.py). The
      validator later re-reads every ledger entry to confirm it landed.
      Todoist writes are recorded in the same ledger under the
      `todoist_task` collection.
    - No pass deletes a business record. The only delete in the daemon is
      prune, and it only removes old rows from the sync log collection.

Exit codes
    0  the command ran and everything it attempted landed
    1  the command ran but some actions failed
    2  the command could not run at all (bad configuration, PocketBase
       unreachable, no backup from today, or an unexpected exception)
    The loop uses the same three numbers with its own meanings, listed in
    daemon/loop.py.
"""

from __future__ import annotations

import argparse
import json
import sys

from core.config import ConfigError, settings
from core.ledger import WriteLedger, ledger_path
from core.logging_setup import RUN_ID, setup
from core.notify import from_settings as notifier_from_settings
from core.todoist import TodoistClient

from . import loop as loop_module
from . import prune as prune_module
from . import reverse as reverse_module
from . import summary as summary_module
from . import sync as sync_module
from . import triggers as triggers_module
from .overdue import OverdueEngine

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_BROKEN = 2


def cmd_sweep(args, conf, log):
    """Run the overdue sweep once and print what it did.

    Without --dry-run every action is recorded in the write ledger. Returns
    EXIT_PARTIAL when any single action failed.
    """
    td_conf = conf.todoist()
    todoist = TodoistClient(
        td_conf["token"], td_conf["project_id"], td_conf["sections"], td_conf["timeout"]
    )

    notifier = None
    if not args.quiet:
        notifier = notifier_from_settings(conf, channel="ops", dry_run=args.dry_run)

    ledger = None
    if not args.dry_run:
        ledger = WriteLedger(ledger_path(conf), run_id=RUN_ID)

    # Known limitation: no `pb=` is passed, so the engine neither reads
    # nor writes the PocketBase mirror of each task's real due date (see
    # core/realdue.py). It works from the pinned line in the Todoist
    # description alone. tools/fill_real_due_mirror.py fills the mirror.
    engine = OverdueEngine(
        todoist,
        conf,
        notifier=notifier,
        ledger=ledger,
        dry_run=args.dry_run,
        logger=log,
    )
    engine.run()

    if args.json:
        print(json.dumps({
            "run_id": RUN_ID,
            "dry_run": args.dry_run,
            "summary": engine.summary(),
            "actions": [a.to_dict() for a in engine.actions],
            "needs_decision": [
                {"task_id": t.get("id"), "content": t.get("content"),
                 "days_overdue": d, "pushes": p}
                for t, d, p in engine.needs_decision
            ],
            "failures": engine.failures,
        }, indent=2))
    else:
        _print_human(engine, args.dry_run)

    todoist.close()
    return EXIT_PARTIAL if engine.failures else EXIT_OK


def _print_human(engine, dry_run):
    """The sweep report for a terminal: actions grouped by task, then the
    tasks that need a decision, then failures, then the summary line."""
    heading = "OVERDUE SWEEP, DRY RUN" if dry_run else "OVERDUE SWEEP"
    print(heading)
    print("=" * len(heading))
    print()

    if not engine.actions:
        print("Nothing was overdue.")
        return

    by_task = {}
    for action in engine.actions:
        by_task.setdefault(action.task_id, []).append(action)

    for task_id, actions in by_task.items():
        label = next((a.content for a in actions if a.content), task_id)
        print("  %s" % label[:90])
        for action in actions:
            print("      %s, %s" % (action.kind, action.detail))
    print()

    if engine.needs_decision:
        print("These need a decision from you. The system has stopped pushing them.")
        for task, days, pushes in engine.needs_decision:
            # %s rather than %d, so a value that is not an int cannot raise
            # TypeError here. The sweep has already written by this point,
            # and this list is the part of the output a person has to see.
            # The third item is the engine's reason text (see
            # OverdueEngine._apply), exported under the JSON key "pushes".
            print("  %s (%s days overdue, %s pushes)"
                  % ((task.get("content") or "")[:90], days, pushes))
        print()

    if engine.failures:
        print("Actions that failed:")
        for failure in engine.failures:
            print("  %s" % failure)
        print()

    print(engine.summary())
    if dry_run:
        print()
        print("Nothing was changed. Run again without --dry-run to apply this.")


def main(argv=None):
    """Parse the command line, load settings and run the chosen command.

    Returns the exit code. A configuration error or any exception that
    escapes a command becomes EXIT_BROKEN, logged with its traceback.
    """
    parser = argparse.ArgumentParser(prog="daemon", description="Splatt ops daemon")
    sub = parser.add_subparsers(dest="command", required=True)

    sweep = sub.add_parser("sweep", help="handle overdue tasks")
    sweep.add_argument("--dry-run", action="store_true",
                       help="report what would happen and change nothing")
    sweep.add_argument("--quiet", action="store_true", help="do not send Telegram")
    sweep.add_argument("--json", action="store_true", help="machine readable output")
    sweep.set_defaults(func=cmd_sweep)

    sync_cmd = sub.add_parser("sync", help="pull Todoist into PocketBase")
    sync_cmd.add_argument("--write", action="store_true",
                          help="apply the plan, otherwise nothing is written")
    sync_cmd.add_argument("--json", action="store_true",
                          help="the plan as data, for comparing runs")
    sync_cmd.add_argument("--quiet", action="store_true", help="do not send Telegram")
    sync_cmd.set_defaults(func=sync_module.cmd_sync)

    reverse_cmd = sub.add_parser("reverse",
                                 help="push dashboard edits and archives to Todoist")
    reverse_cmd.add_argument("--write", action="store_true",
                             help="apply the plan, otherwise nothing is sent")
    reverse_cmd.add_argument("--json", action="store_true", help="the plan as data")
    reverse_cmd.add_argument("--quiet", action="store_true",
                             help="do not send Telegram")
    reverse_cmd.set_defaults(func=reverse_module.cmd_reverse)

    triggers_cmd = sub.add_parser(
        "triggers", help="let finished tasks move job statuses, and chase quotes")
    triggers_cmd.add_argument("--write", action="store_true",
                              help="apply the plan, otherwise nothing is written")
    triggers_cmd.add_argument("--json", action="store_true",
                              help="the plan as data")
    triggers_cmd.add_argument("--quiet", action="store_true",
                              help="do not send Telegram")
    triggers_cmd.set_defaults(func=triggers_module.cmd_triggers)

    prune_cmd = sub.add_parser("prune", help="delete sync log records past their keep window")
    prune_cmd.add_argument("--write", action="store_true",
                           help="delete them, otherwise nothing is removed")
    prune_cmd.add_argument("--json", action="store_true", help="the plan as data")
    prune_cmd.set_defaults(func=prune_module.cmd_prune)

    loop_cmd = sub.add_parser("loop", help="run reverse and sync on a timer")
    loop_cmd.add_argument("--write", action="store_true",
                          help="apply each pass, otherwise nothing is written")
    loop_cmd.add_argument("--once", action="store_true",
                          help="one pass, then stop")
    loop_cmd.add_argument("--verbose", action="store_true",
                          help="log everything each pass printed, not just failures")
    loop_cmd.set_defaults(func=loop_module.cmd_loop)

    summary_cmd = sub.add_parser("summary", help="the daily summary")
    summary_cmd.add_argument("--send", action="store_true",
                             help="put it on Telegram, otherwise it only prints")
    summary_cmd.set_defaults(func=summary_module.cmd_summary)

    args = parser.parse_args(argv)
    log = setup("daemon")

    try:
        conf = settings()
    except ConfigError as exc:
        log.error("config could not be loaded: %s", exc)
        return EXIT_BROKEN

    try:
        return args.func(args, conf, log)
    except ConfigError as exc:
        log.error("%s", exc)
        return EXIT_BROKEN
    except Exception as exc:
        log.exception("the command could not run: %s", exc)
        return EXIT_BROKEN


if __name__ == "__main__":
    sys.exit(main())
