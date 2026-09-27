"""
The loop: runs every daemon pass on a timer, so the system keeps working
when nobody is typing commands.

    python -m daemon loop            run the passes on a timer, write nothing
    python -m daemon loop --write    run them for real (the permanent process)
    python -m daemon loop --once     one pass, then stop
    python -m daemon loop --verbose  log the full output of every pass

What it does
    The loop is a scheduler and nothing else. Each step calls the same
    `cmd_*` function that the matching command line command calls (for
    example `daemon.sync.cmd_sync`), so there is only one copy of each
    pass's logic. A pass that behaves differently here than on the command
    line points at a bug in this file.

Schedule
    A pass starts every `daemon.poll_interval_seconds` (60 by default).
    After a failed pass the wait is `daemon.error_backoff_seconds` instead
    (300 by default). Within one pass the steps run in this order:

    Step  What               When it runs                          Dry run
    ----  -----------------  ------------------------------------  -----------
    1     backup             no snapshot from today (UTC) in       skipped
                             backups/ yet; retried every pass
                             until one exists
    2     reverse sync       every pass                            plan only
    3     forward sync       every pass                            plan only
    4     triggers           every pass                            skipped
    5     prune              first pass of each UTC day            skipped
    6     overdue sweep      first pass at or after 05:00 local,   skipped
                             once per local day
    7     validator run      ops channel quiet for                 skipped
                             validator.auto_run_interval_hours
                             (12 by default)
    8     silence watchdog   first pass of each UTC day            skipped
    9     Telegram outbox    every pass                            skipped
    10    heartbeat file     every pass, failed ones included      written

    Steps 1 to 7 go through `run_pass`, and any of them failing makes the
    whole pass a failed pass. Steps 8 and 9 never count as a failure.

    "Once a day" is remembered in different places, on purpose:
      backup      asks backups/ whether today's snapshot exists
      sweep       state/last-sweep-day.txt (the local date last attempted)
      validator   state/validation-heartbeat.json (last accepted message)
      prune, silence watchdog   an in-memory date, so a restart runs them
                  once more, which is harmless
    The first three survive a restart, so a restart never repeats a
    backup, a sweep or a round of validator messages.

Why the steps are in this order
    Backup first. reverse, sync and triggers refuse to write without a
    snapshot from today, so the loop takes that snapshot itself before
    anything writes.

    Reverse before forward. A dashboard edit has to reach Todoist before
    the forward pass reads Todoist back; otherwise the forward pass reads
    the old text and writes it over the edit.

    Triggers straight after the forward pass. The forward pass is what
    marks an assignment completed when its Todoist task is ticked off, and
    a trigger fires on exactly that change, so it sees it in the same pass.

    Validator before the silence watchdog. Both read the same validation
    heartbeat. Running the validator first means a channel that had gone
    quiet has just heard from it, so the watchdog does not raise an alert
    about a condition the same pass is fixing.

    Sweep in the early morning. The overdue sweep moves and re-dates
    tasks, which is best done before the operator starts work. The rule is
    "at or after 05:00", not a time window, so a machine that was asleep
    at 05:00 still sweeps on its first pass after waking.

Safety
    - A dry run (no --write) only lets reverse and sync build and print
      their plans. Nothing that writes data runs at all.
    - Each pass builds fresh PocketBase and Todoist clients, so a
      PocketBase restart is recovered on the next pass.
    - A pid lock (state/loop.pid) stops a second loop from starting. A
      lock whose pid is no longer running, for example after a power cut,
      is treated as stale and taken over.
    - SIGINT and SIGTERM stop the loop immediately. The wait between
      passes is on a threading.Event that the signal handler sets.
    - After GIVE_UP_AFTER failed passes in a row the loop exits rather
      than keep failing quietly.

Output
    Each pass's printed output is captured. A clean pass writes one line
    to the log, a failed pass writes everything it printed, and --verbose
    writes everything. The full record of what changed is the write
    ledger and the sync log collection, not the text log.
    state/loop-heartbeat.json is rewritten every pass with the pid, the
    pass count, the consecutive failure count, the silence watchdog and
    outbox results, and the exit code and error of each step.

Exit codes
    0  it was asked to stop (a signal, or --once finished) and it stopped
    1  it stopped because GIVE_UP_AFTER passes in a row failed
    2  it could not start (another loop holds the lock)
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import threading
from datetime import datetime, timezone
from pathlib import Path

from core.config import ConfigError

from tools import backup as backup_module
# Imported at module level, like the other passes, so a test can replace
# validator_cli.cmd_run. The real one talks to PocketBase and Telegram.
from validator import cli as validator_cli

from . import prune as prune_module
from . import reverse as reverse_module
from . import sync as sync_module
from . import triggers as triggers_module

EXIT_OK = 0
EXIT_GAVE_UP = 1
EXIT_BROKEN = 2

# Consecutive failed passes before the loop exits with EXIT_GAVE_UP. A
# stopped process is easy to see from outside; a loop that keeps running
# and failing is not.
GIVE_UP_AFTER = 20


class PassArgs:
    """The argument object the `cmd_*` passes expect from argparse.

    They read args.write, args.json and args.quiet and nothing else. This
    is a plain class rather than an argparse.Namespace, so a pass that
    starts reading a flag that is not set here raises AttributeError
    instead of quietly getting a default.
    """

    def __init__(self, write=False, quiet=False, json=False):
        self.write = write
        self.quiet = quiet
        self.json = json


class ValidateArgs:
    """The arguments validator.cli.cmd_run reads from argparse.

    Separate from PassArgs because cmd_run reads a different set of flags
    (it has no .write, and it needs .notify). A plain class for the same
    reason as PassArgs: an unset flag raises instead of defaulting.

    notify defaults to True because the point of the automatic run is
    that the ops channel hears the result. trigger="daemon" records in the
    validator's report that the loop, not a person, started the run.
    """

    def __init__(self, standing=False, json=False, notify=True,
                 no_todoist=False, run_id="", trigger="daemon"):
        self.standing = standing
        self.json = json
        self.notify = notify
        self.no_todoist = no_todoist
        self.run_id = run_id
        self.trigger = trigger


class Lock:
    """A pid file that stops two loops running at once.

    The file holds the pid of the loop that took it. A file whose pid is
    not a running process (left behind by a crash or a power cut) is
    treated as stale and overwritten, so it never blocks a restart.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.held = False

    def owner(self):
        """The pid in the file if that process is alive, otherwise None.

        The current process's own pid counts as alive. Whether this object
        took the lock is tracked separately by `held`, which is what lets a
        test play the part of a second loop from inside one process.
        """
        if not self.path.exists():
            return None
        try:
            pid = int(self.path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            return None
        try:
            # Signal 0 checks that the process exists without affecting it.
            os.kill(pid, 0)
        except OSError:
            return None
        return pid

    def take(self):
        """Take the lock. Returns None on success (or if this object
        already holds it), otherwise the pid of the live owner."""
        if self.held:
            return None
        running = self.owner()
        if running:
            return running
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("%d\n" % os.getpid(), encoding="utf-8")
        self.held = True
        return None

    def release(self):
        """Remove the file, but only if this object took the lock and the
        file still names this process."""
        if not self.held:
            return
        try:
            if self.path.exists() and self.path.read_text(encoding="utf-8").strip() \
                    == str(os.getpid()):
                self.path.unlink()
        except OSError:
            pass
        self.held = False


class Stopper:
    """Turns SIGINT and SIGTERM into a threading.Event the sleep can wait on."""

    def __init__(self):
        self.event = threading.Event()
        self.reason = ""
        self._previous = {}

    def install(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._previous[sig] = signal.signal(sig, self._handle)
            except (ValueError, OSError):
                # signal.signal only works on the main thread, which some
                # test runners are not. The loop then stops only through
                # --once or the failure limit.
                pass

    def restore(self):
        for sig, handler in self._previous.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass
        self._previous = {}

    def _handle(self, sig, frame):
        self.reason = signal.Signals(sig).name
        self.event.set()

    @property
    def stopped(self):
        return self.event.is_set()

    def sleep(self, seconds):
        """Wait, but wake immediately if asked to stop."""
        self.event.wait(seconds)


def heartbeat(path, state):
    """Write the loop's current state to `path` as JSON.

    Written every pass, failed passes included, so a loop that is running
    but failing can be told apart from one that has stopped. The JSON goes
    to a .tmp file that is then renamed over the old one, so a reader
    never sees a half-written file, and a write that fails leaves the
    previous heartbeat in place.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)
    return path


def run_pass(name, fn, args, conf, log, verbose=False):
    """Run one pass and return what happened.

    The pass's printed output is captured rather than shown, because the
    `cmd_*` functions are written for a person at a terminal and at one
    pass a minute that output would swamp the log. The output is logged
    only when the pass was not clean, or with verbose=True.

    An exception is caught and logged, so one broken pass cannot stop the
    loop. Returns a dict with pass, code, error, seconds, output and
    clean, where clean means exit code 0 and no exception.
    """
    buffer = io.StringIO()
    started = datetime.now(timezone.utc)
    code = EXIT_BROKEN
    error = ""
    try:
        with contextlib.redirect_stdout(buffer):
            code = fn(args, conf, log)
    except ConfigError as exc:
        error = "configuration problem: %s" % exc
        log.error("%s pass could not run, %s", name, exc)
    except Exception as exc:
        error = "%s: %s" % (type(exc).__name__, exc)
        log.exception("%s pass raised, the loop carries on: %s", name, exc)

    output = buffer.getvalue()
    took = (datetime.now(timezone.utc) - started).total_seconds()
    clean = code == EXIT_OK and not error

    if verbose or not clean:
        for line in output.splitlines():
            log.info("%s | %s", name, line)

    if error:
        log.error("%s failed after %.1fs, %s", name, took, error)
    else:
        log.info("%s exit %d in %.1fs", name, code, took)

    return {
        "pass": name,
        "code": code,
        "error": error,
        "seconds": round(took, 1),
        "output": output,
        "clean": clean,
    }


def backup_due(conf):
    """True when backups/ has no snapshot from today (UTC) yet.

    Checked against the folder every time rather than remembered in a
    variable. A restart therefore does not take a second snapshot, and a
    failed attempt is retried on the next pass instead of leaving the
    writing passes refusing to run for the rest of the day.
    """
    return not backup_module.taken_today(conf.repo_root / "backups")


def validation_due(conf):
    """True when the ops channel has been quiet long enough for an
    automatic validator run.

    "Quiet" is the number of hours since a validator message was last
    accepted by Telegram, read from the validation heartbeat
    (state/validation-heartbeat.json) by
    validator.notifications.quiet_for_hours. The run is due once that
    reaches `validator.auto_run_interval_hours` (12 by default). A value
    of 0 or less turns automatic runs off.

    No heartbeat at all (None) means the validator has never reported,
    which is the worst case, so it counts as due.

    Reading the heartbeat rather than remembering the last run has two
    effects. A restart does not cause an extra run, which matters because
    each run sends several Telegram messages. And a run whose Telegram
    send failed does not update the heartbeat, so it is tried again on a
    later pass. Measuring quiet time also ties this schedule directly to
    the silence_is_a_fault rule the watchdog checks.
    """
    from validator import notifications as notifications_module

    hours = notifications_module.quiet_for_hours(conf)
    if hours is None:
        return True
    interval = float(conf.get("validator.auto_run_interval_hours", 12))
    if interval <= 0:
        return False
    return hours >= interval


def sweep_due(conf, now=None):
    """True when it is 05:00 or later local time and today's overdue sweep
    has not been attempted yet.

    The local date of the last attempt is kept in
    state/last-sweep-day.txt, so a restart does not sweep twice and a new
    day sweeps even if the process never restarted.

    The file records an attempt, not a success. A sweep that failed is not
    retried later the same day, because moving tasks while the operator is
    working is what an early morning sweep avoids. The failure is still in
    the log and in the loop heartbeat.

    The rule is "at or after 05:00" rather than a time window, so a
    machine that was asleep at 05:00 still sweeps on its first pass after
    waking, however late in the morning that is.
    """
    now = now or datetime.now().astimezone()
    if now.hour < 5:
        return False
    marker = conf.repo_root / "state" / "last-sweep-day.txt"
    try:
        return marker.read_text(encoding="utf-8").strip() != now.date().isoformat()
    except OSError:
        return True


def mark_sweep_attempted(conf, now=None):
    """Write today's local date to the marker file that sweep_due reads."""
    now = now or datetime.now().astimezone()
    marker = conf.repo_root / "state" / "last-sweep-day.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(now.date().isoformat() + "\n", encoding="utf-8")


class SweepArgs:
    """The arguments cli.cmd_sweep reads from argparse: dry_run, json, quiet.

    A plain class for the same reason as PassArgs. dry_run=False because
    the loop only runs the sweep with --write. json=True because run_pass
    captures the output anyway and JSON is the more useful form in a log.
    quiet=False so the sweep's own Telegram alerts go out exactly as they
    do when the sweep is run by hand.
    """

    def __init__(self):
        self.dry_run = False
        self.json = True
        self.quiet = False


def cmd_sweep_for_loop(args, conf, log):
    """The overdue sweep, run through daemon.cli.cmd_sweep.

    Imported inside the function because daemon.cli imports this module
    to register the loop command, so a top-level import here would be
    circular.
    """
    from . import cli as cli_module

    return cli_module.cmd_sweep(args, conf, log)


def cmd_validate_for_loop(args, conf, log):
    """Run the validator and report whether it ran, not what it found.

    validator.cli.cmd_run returns 0 for a clean verdict, 1 for BLOCKED (it
    ran and found problems in the data) and 2 when it could not run. The
    loop treats any non-zero code as a failed pass, and failed passes lead
    to backoff and, after GIVE_UP_AFTER in a row, to the loop stopping.

    A BLOCKED verdict is a finding about the data, which the loop cannot
    fix, so it is returned here as EXIT_OK. The verdict is not lost: it is
    still sent to Telegram, still written to the validator's report, and
    still sets the exit code of bin/splatt-validate when a person runs it.
    Exit 2 stays a failure, because a validator that cannot run is a
    problem with the daemon's environment.
    """
    code = validator_cli.cmd_run(args, conf, log)
    if code == validator_cli.EXIT_BLOCKED:
        log.info("validation ran and the verdict is BLOCKED, "
                 "which is a finding rather than a failed pass")
        return EXIT_OK
    return code


def tell_ops_the_validator_is_quiet(conf, log, message):
    """Best-effort Telegram alert to the ops channel when the silence
    watchdog trips.

    Returns a short status for the heartbeat: "sent", "queued after
    failure, ..." or "failed, ...".

    Sent through core.notify directly, not through the validator's
    dispatcher. The dispatcher refreshes the validation heartbeat on every
    accepted send, and that heartbeat is the clock the watchdog measures,
    so sending the alert through it would clear the fault without anything
    being fixed.

    If the send fails, the message is queued in the Telegram outbox
    (core/outbox.py) so a later pass delivers it. The trip is recorded in
    the loop heartbeat either way.
    """
    try:
        from core import notify as notify_mod

        notifier = notify_mod.from_settings(conf, "ops")
        text = (
            "🚨 The validator has gone quiet\n"
            "%s\n"
            "Nothing has been repaired and nothing has been checked. "
            "Run bin/splatt-validate to find out whether it still works."
            % message
        )
        if notifier.send(text):
            return "sent"
        error = getattr(notifier, "last_error", "unknown error")
        # Queue the alert so a later pass delivers it once Telegram is
        # reachable again.
        try:
            from core import outbox as outbox_module

            outbox_module.enqueue(conf.repo_root, text, channel="ops",
                                  msg_type="alert", source="loop.ops_silence")
            return "queued after failure, %s" % error
        except Exception:
            return "failed, %s" % error
    except Exception as exc:
        log.error("could not tell the ops channel about the silence: %s", exc)
        return "failed, %s: %s" % (type(exc).__name__, exc)


def check_ops_silence(conf, log):
    """The silence watchdog: check the validator's silence_is_a_fault rule.

    The rule lives in config/validation-rules.yaml. It trips when the ops
    channel has had no validator message for longer than max_quiet_hours
    (24 there), which means the validator has stopped running or stopped
    reaching Telegram. The loop asks once a day because it is the only
    part of the system that runs without being started by a person. When
    the rule trips, the ops channel is told through
    tell_ops_the_validator_is_quiet.

    Returns a state dict that the loop stores in its heartbeat under
    `ops_silence`.

    Never counted as a failed pass, and every exception is caught. A
    tripped watchdog means the validator has stopped, not that this pass
    went wrong, and counting it would push the loop into backoff and
    eventually stop the sync over a reporting problem.
    """
    at = datetime.now(timezone.utc).isoformat()
    try:
        from validator.notifications import quiet_for_hours, silence_check
        from validator.rules import RuleSet

        ruleset = RuleSet(conf.repo_root / conf.get("validator.rules_file"))
        tripped, message = silence_check(conf, ruleset.notifications)
        hours = quiet_for_hours(conf)
    except Exception as exc:
        log.error("silence check could not run, %s: %s", type(exc).__name__, exc)
        return {"at": at, "checked": False, "error": "%s: %s" % (type(exc).__name__, exc)}

    state = {
        "at": at,
        "checked": True,
        "tripped": bool(tripped),
        "quiet_hours": None if hours is None else round(hours, 1),
        "message": message,
    }
    if not tripped:
        log.info("ops silence check, %s", message)
        return state

    log.error("SILENCE IS A FAULT, %s", message)
    state["told_ops"] = tell_ops_the_validator_is_quiet(conf, log, message)
    return state


def one_cycle(conf, log, write, verbose=False, results=None):
    """Steps 1 to 4 of the schedule: backup if due, reverse, forward sync,
    triggers. Returns the list of run_pass results.

    Backup and triggers run only when `write` is True. A dry run that took
    a snapshot would change files on disk, and a dry triggers pass would
    print the same plan every minute without acting on it.
    """
    args = PassArgs(write=write, quiet=not write)
    out = results if results is not None else []
    if write and backup_due(conf):
        out.append(run_pass("backup", backup_module.cmd_backup, args, conf, log, verbose))
    out.append(run_pass("reverse", reverse_module.cmd_reverse, args, conf, log, verbose))
    out.append(run_pass("sync", sync_module.cmd_sync, args, conf, log, verbose))
    if write:
        out.append(run_pass("triggers", triggers_module.cmd_triggers, args,
                            conf, log, verbose))
    return out


def deliver_outbox(conf, log):
    """Send any Telegram messages queued in state/telegram-outbox.

    Messages are queued there when a direct send failed, or by a process
    that cannot reach Telegram itself (see core/outbox.py). Runs on every
    writing pass and catches every exception, so a Telegram outage can
    never stop the sync. Returns the delivery summary for the heartbeat,
    or None when there was nothing to deliver.
    """
    try:
        from core import outbox as outbox_module

        summary = outbox_module.deliver(conf, log)
        if summary["delivered"] or summary["queued"]:
            return summary
        return None
    except Exception as exc:
        log.error("outbox delivery could not run, %s: %s",
                  type(exc).__name__, exc)
        return {"error": "%s: %s" % (type(exc).__name__, exc)}


def cmd_loop(args, conf, log):
    """Run the schedule in the module docstring until stopped.

    Returns EXIT_OK when asked to stop, EXIT_GAVE_UP after GIVE_UP_AFTER
    failed passes in a row, or EXIT_BROKEN if another loop holds the lock.
    """
    interval = int(conf.get("daemon.poll_interval_seconds", 60))
    backoff = int(conf.get("daemon.error_backoff_seconds", 300))
    state_dir = conf.repo_root / "state"

    lock = Lock(state_dir / "loop.pid")
    running = lock.take()
    if running:
        log.error("another loop is already running as pid %d", running)
        print("A loop is already running as pid %d. Nothing was started." % running)
        print("If that is wrong, stop it or remove %s" % (state_dir / "loop.pid"))
        return EXIT_BROKEN

    stopper = Stopper()
    stopper.install()

    beat = state_dir / "loop-heartbeat.json"
    passes = 0
    failures_in_a_row = 0
    last_prune_day = None
    last_silence_day = None
    silence = None
    outbox_state = None
    code = EXIT_OK

    log.info("loop starting, every %ds, %s", interval,
             "writing" if args.write else "dry run, nothing will be written")
    if not args.write:
        print("Dry run. Every pass works out what it would do and does none of it.")
        print("Pass --write when you want it to act.")

    try:
        while True:
            passes += 1
            # Steps 1 to 4: backup, reverse, sync, triggers.
            results = one_cycle(conf, log, args.write, args.verbose)

            # Step 5, prune: once per UTC day, only when writing. A dry
            # run prune would print the same plan every day and do nothing.
            today = datetime.now(timezone.utc).date()
            if args.write and last_prune_day != today:
                results.append(run_pass("prune", prune_module.cmd_prune,
                                        PassArgs(write=True), conf, log, args.verbose))
                last_prune_day = today

            # Step 6, overdue sweep: first pass at or after 05:00 local,
            # once a day (see sweep_due). Marked as attempted whatever the
            # outcome, so a failed sweep is reported rather than retried
            # during the working day.
            if args.write and sweep_due(conf):
                results.append(run_pass("sweep", cmd_sweep_for_loop,
                                        SweepArgs(), conf, log, args.verbose))
                mark_sweep_attempted(conf)

            # Step 7, validator run: when the ops channel has been quiet
            # for the configured interval (see validation_due). It runs
            # before the silence watchdog because both read the same
            # heartbeat: a validator run that has just reported means the
            # watchdog below correctly finds the channel no longer quiet.
            if args.write and validation_due(conf):
                results.append(run_pass("validate", cmd_validate_for_loop,
                                        ValidateArgs(trigger="daemon"),
                                        conf, log, args.verbose))

            # Step 8, silence watchdog: once per UTC day. Checking every
            # pass would repeat the same alert every minute.
            if args.write and last_silence_day != today:
                silence = check_ops_silence(conf, log)
                last_silence_day = today

            # Step 9, Telegram outbox: every writing pass. Never counted
            # as a failed pass, for the same reason as the watchdog.
            if args.write:
                outbox_state = deliver_outbox(conf, log)

            if all(r["clean"] for r in results):
                failures_in_a_row = 0
            else:
                failures_in_a_row += 1

            # Step 10, heartbeat.
            heartbeat(beat, {
                "at": datetime.now(timezone.utc).isoformat(),
                "pid": os.getpid(),
                "passes": passes,
                "writing": bool(args.write),
                "interval_seconds": interval,
                "failures_in_a_row": failures_in_a_row,
                "ops_silence": silence,
                "telegram_outbox": outbox_state,
                "last": [{k: r[k] for k in ("pass", "code", "error", "seconds")}
                         for r in results],
            })

            if failures_in_a_row >= GIVE_UP_AFTER:
                log.error("%d passes in a row failed, stopping", failures_in_a_row)
                print("%d passes in a row failed. Stopping the loop."
                      % failures_in_a_row)
                code = EXIT_GAVE_UP
                break

            if args.once or stopper.stopped:
                break

            wait = backoff if failures_in_a_row else interval
            if failures_in_a_row:
                log.warning("pass failed, waiting %ds instead of %ds", wait, interval)
            stopper.sleep(wait)
            if stopper.stopped:
                break
    finally:
        stopper.restore()
        lock.release()

    if stopper.reason:
        log.info("stopped on %s after %d passes", stopper.reason, passes)
        print("Stopped on %s after %d passes." % (stopper.reason, passes))
    elif args.once:
        log.info("one pass done")
    else:
        log.info("loop finished after %d passes", passes)

    return code
