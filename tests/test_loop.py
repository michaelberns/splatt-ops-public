"""
Tests for daemon/loop.py.

The loop is a scheduler, so these tests cover scheduling only. Every pass
is replaced by a recorder; whether a pass itself is correct is tested in
that pass's own test file (test_sync.py, test_reverse_daemon.py, ...).

Behaviour pinned down here:

  a backup is taken before anything writes, once a day, and a failed one
    is retried on the next pass
  reverse runs before sync on every pass, and triggers straight after sync
  a pass that raises does not stop the loop
  a failed pass waits the backoff instead of the interval
  GIVE_UP_AFTER failed passes in a row stop the loop with EXIT_GAVE_UP
  prune runs once a day, not every pass
  the validator runs automatically when the ops channel is quiet, and a
    BLOCKED verdict is not a failed pass
  the silence watchdog runs once a day and never fails a pass
  the overdue sweep runs once a day when due and is marked attempted
  a dry run writes nothing: no backup, triggers, prune, sweep or validator
  the pid lock stops a second loop, and a stale lock does not block a start
  a stop signal ends the wait immediately
  the heartbeat records every pass, including failed ones
  a clean pass logs one line, not its whole output
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from daemon import loop as loop_module
from validator import cli as validator_cli_module
from daemon.loop import (
    EXIT_BROKEN,
    EXIT_GAVE_UP,
    EXIT_OK,
    GIVE_UP_AFTER,
    Lock,
    PassArgs,
    Stopper,
    ValidateArgs,
    check_ops_silence,
    cmd_loop,
    cmd_validate_for_loop,
    heartbeat,
    mark_sweep_attempted,
    run_pass,
    sweep_due,
    validation_due,
)


class FakeLog:
    def __init__(self):
        self.lines = []

    def _add(self, level, fmt, *args):
        try:
            self.lines.append((level, fmt % args if args else fmt))
        except TypeError:
            self.lines.append((level, fmt))

    def info(self, fmt, *args):
        self._add("info", fmt, *args)

    def warning(self, fmt, *args):
        self._add("warning", fmt, *args)

    def error(self, fmt, *args):
        self._add("error", fmt, *args)

    def exception(self, fmt, *args, **kwargs):
        self._add("error", fmt, *args)

    def text(self, level=None):
        return "\n".join(m for lv, m in self.lines if level is None or lv == level)


class FakeConf:
    def __init__(self, repo_root, interval=0, backoff=0):
        self.repo_root = Path(repo_root)
        self.values = {
            "daemon.poll_interval_seconds": interval,
            "daemon.error_backoff_seconds": backoff,
        }

    def get(self, dotted, default=None):
        return self.values.get(dotted, default)


class Args:
    def __init__(self, write=False, once=True, verbose=False):
        self.write = write
        self.once = once
        self.verbose = verbose


@pytest.fixture
def wiring(tmp_path, monkeypatch):
    """Replace every pass with a recorder that logs (name, args.write).

    Every pass is replaced, because an unreplaced pass would run for real
    and the validator pass talks to PocketBase and Telegram.

    ValidateArgs has no .write, so the recorder falls back to True for the
    validator pass, meaning "ran in a writing loop".

    The sweep is replaced too, and sweep_due is pinned to False unless a
    test passes sweep_is_due=True, because the real sweep_due reads the
    clock and would make results depend on the time of day.
    """
    calls = []

    def recorder(name, code=EXIT_OK, raises=None, says=""):
        def fn(args, conf, log):
            calls.append((name, getattr(args, "write", True)))
            if says:
                print(says)
            if raises:
                raise raises
            return code
        return fn

    state = {"calls": calls, "recorder": recorder}

    def install(reverse=None, sync=None, prune=None, backup=None,
                validate=None, triggers=None, sweep=None,
                sweep_is_due=False):
        monkeypatch.setattr(loop_module.backup_module, "cmd_backup",
                            backup or recorder("backup"))
        monkeypatch.setattr(loop_module.reverse_module, "cmd_reverse",
                            reverse or recorder("reverse"))
        monkeypatch.setattr(loop_module.sync_module, "cmd_sync",
                            sync or recorder("sync"))
        monkeypatch.setattr(loop_module.triggers_module, "cmd_triggers",
                            triggers or recorder("triggers"))
        monkeypatch.setattr(loop_module.prune_module, "cmd_prune",
                            prune or recorder("prune"))
        monkeypatch.setattr(loop_module.validator_cli, "cmd_run",
                            validate or recorder("validate"))
        monkeypatch.setattr(loop_module, "cmd_sweep_for_loop",
                            sweep or recorder("sweep"))
        monkeypatch.setattr(loop_module, "sweep_due",
                            lambda conf, now=None: sweep_is_due)

    state["install"] = install
    install()
    return state


def heartbeat_file(tmp_path):
    return json.loads((tmp_path / "state" / "loop-heartbeat.json").read_text())


# order
def test_reverse_runs_before_sync(tmp_path, wiring):
    """Reverse runs before sync, so a dashboard edit reaches Todoist
    before the forward pass reads Todoist back."""
    cmd_loop(Args(), FakeConf(tmp_path), FakeLog())
    assert [name for name, _ in wiring["calls"]] == ["reverse", "sync"]


def test_triggers_runs_after_sync(tmp_path, wiring):
    """Triggers runs immediately after sync, so it sees an assignment
    that sync has just marked completed in the same pass."""
    cmd_loop(Args(write=True), FakeConf(tmp_path), FakeLog())
    names = [name for name, _ in wiring["calls"]]
    assert names.index("triggers") == names.index("sync") + 1, names


def test_a_dry_run_does_not_run_the_trigger_pass(tmp_path, wiring):
    """A dry run skips the triggers pass entirely."""
    cmd_loop(Args(write=False), FakeConf(tmp_path), FakeLog())
    assert "triggers" not in [name for name, _ in wiring["calls"]]


def test_a_dry_run_asks_every_pass_not_to_write(tmp_path, wiring):
    cmd_loop(Args(write=False), FakeConf(tmp_path), FakeLog())
    assert all(write is False for _, write in wiring["calls"])


def test_writing_is_passed_all_the_way_down(tmp_path, wiring):
    cmd_loop(Args(write=True), FakeConf(tmp_path), FakeLog())
    assert all(write is True for name, write in wiring["calls"] if name != "prune")


# a pass that goes wrong
def test_a_pass_that_raises_does_not_take_the_loop_down(tmp_path, wiring):
    wiring["install"](reverse=wiring["recorder"]("reverse", raises=RuntimeError("boom")))
    log = FakeLog()
    code = cmd_loop(Args(), FakeConf(tmp_path), log)

    assert ("sync", False) in wiring["calls"], "the loop stopped at the first failure"
    assert code == EXIT_OK
    assert "boom" in log.text()


def test_a_failed_pass_is_named_in_the_heartbeat(tmp_path, wiring):
    wiring["install"](sync=wiring["recorder"]("sync", raises=RuntimeError("boom")))
    cmd_loop(Args(), FakeConf(tmp_path), FakeLog())

    beat = heartbeat_file(tmp_path)
    failed = [p for p in beat["last"] if p["error"]]
    assert [p["pass"] for p in failed] == ["sync"]
    assert "boom" in failed[0]["error"]
    assert beat["failures_in_a_row"] == 1


def test_a_non_zero_exit_counts_as_a_failure_even_without_an_exception(tmp_path, wiring):
    """A pass that returns a non-zero exit code counts as failed, even
    though it raised nothing."""
    wiring["install"](sync=wiring["recorder"]("sync", code=2))
    cmd_loop(Args(), FakeConf(tmp_path), FakeLog())
    assert heartbeat_file(tmp_path)["failures_in_a_row"] == 1


def test_it_gives_up_after_enough_failures_in_a_row(tmp_path, wiring):
    """After GIVE_UP_AFTER failed passes in a row the loop returns
    EXIT_GAVE_UP."""
    wiring["install"](sync=wiring["recorder"]("sync", code=2))
    code = cmd_loop(Args(once=False), FakeConf(tmp_path), FakeLog())

    assert code == EXIT_GAVE_UP
    assert heartbeat_file(tmp_path)["failures_in_a_row"] == GIVE_UP_AFTER


def test_one_good_pass_clears_the_failure_count(tmp_path, wiring, monkeypatch):
    """One clean pass resets failures_in_a_row to zero, so occasional
    failures never add up to the give-up limit."""
    codes = iter([2, 2, 0])

    def sometimes(args, conf, log):
        return next(codes, 0)

    waits = {"n": 0}

    def stop_after_three(self, seconds):
        waits["n"] += 1
        if waits["n"] >= 3:
            self.event.set()

    monkeypatch.setattr(Stopper, "sleep", stop_after_three)
    wiring["install"](sync=sometimes)
    cmd_loop(Args(once=False), FakeConf(tmp_path), FakeLog())
    assert heartbeat_file(tmp_path)["failures_in_a_row"] == 0


def test_a_failure_waits_the_backoff_not_the_interval(tmp_path, wiring, monkeypatch):
    waits = []
    monkeypatch.setattr(Stopper, "sleep", lambda self, seconds: waits.append(seconds))
    wiring["install"](sync=wiring["recorder"]("sync", code=2))

    conf = FakeConf(tmp_path, interval=60, backoff=300)
    # Stop after the first wait so the test does not run to the give up limit.
    original = Stopper.sleep

    def stop_after_one(self, seconds):
        waits.append(seconds)
        self.event.set()

    monkeypatch.setattr(Stopper, "sleep", stop_after_one)
    cmd_loop(Args(once=False), conf, FakeLog())
    assert waits == [300]


def test_a_clean_pass_waits_the_interval(tmp_path, wiring, monkeypatch):
    waits = []

    def stop_after_one(self, seconds):
        waits.append(seconds)
        self.event.set()

    monkeypatch.setattr(Stopper, "sleep", stop_after_one)
    cmd_loop(Args(once=False), FakeConf(tmp_path, interval=60, backoff=300), FakeLog())
    assert waits == [60]


# backup
#
# The writing passes refuse to run without a snapshot from today, so the
# loop has to take one itself, first, and retry until it succeeds.
def snapshot_for(tmp_path, when):
    """Put a snapshot in the backups folder stamped for a given day."""
    folder = tmp_path / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ("snapshot-%s-120000.json" % when.strftime("%Y%m%d"))
    path.write_text(json.dumps({"counts": {}, "collections": {}}), encoding="utf-8")
    return path


def today_utc():
    return datetime.now(timezone.utc)


def test_the_backup_runs_before_anything_that_writes(tmp_path, wiring):
    """The backup is the first step, before reverse, sync and triggers,
    so the snapshot holds the state before this pass's writes."""
    cmd_loop(Args(write=True), FakeConf(tmp_path), FakeLog())
    names = [name for name, _ in wiring["calls"]]
    assert names[:4] == ["backup", "reverse", "sync", "triggers"], names


def test_a_day_with_no_snapshot_gets_one(tmp_path, wiring):
    cmd_loop(Args(write=True), FakeConf(tmp_path), FakeLog())
    assert ("backup", True) in wiring["calls"]


def test_a_day_that_already_has_a_snapshot_does_not_get_a_second(tmp_path, wiring):
    """An existing snapshot from today means no second backup, so a
    restart does not copy the database again."""
    snapshot_for(tmp_path, today_utc())
    cmd_loop(Args(write=True), FakeConf(tmp_path), FakeLog())
    assert "backup" not in [name for name, _ in wiring["calls"]]


def test_yesterdays_snapshot_does_not_count_for_today(tmp_path, wiring):
    """A snapshot from yesterday counts as absent, because the writing
    passes need one from today."""
    snapshot_for(tmp_path, today_utc() - timedelta(days=1))
    cmd_loop(Args(write=True), FakeConf(tmp_path), FakeLog())
    assert ("backup", True) in wiring["calls"]


def test_a_backup_that_fails_is_tried_again_next_pass(tmp_path, wiring, monkeypatch):
    """A failed backup is retried on every following pass until one
    exists."""
    wiring["install"](backup=wiring["recorder"]("backup", code=2))

    passes = {"n": 0}

    def stop_after_three(self, seconds):
        passes["n"] += 1
        if passes["n"] >= 3:
            self.event.set()

    monkeypatch.setattr(Stopper, "sleep", stop_after_three)
    cmd_loop(Args(write=True, once=False), FakeConf(tmp_path), FakeLog())

    tries = [name for name, _ in wiring["calls"]].count("backup")
    assert tries >= 3, "a failed backup was not retried, it was written off"


def test_a_failed_backup_is_named_in_the_heartbeat(tmp_path, wiring):
    """A backup that raised appears in the heartbeat with its error."""
    wiring["install"](backup=wiring["recorder"]("backup", raises=RuntimeError("no pb")))
    cmd_loop(Args(write=True), FakeConf(tmp_path), FakeLog())

    beat = heartbeat_file(tmp_path)
    failed = [p for p in beat["last"] if p["error"]]
    assert [p["pass"] for p in failed] == ["backup"]
    assert "no pb" in failed[0]["error"]


def test_a_dry_run_does_not_back_up(tmp_path, wiring):
    """A dry run takes no snapshot. It writes nothing that needs one, and
    a snapshot would change files on disk."""
    cmd_loop(Args(write=False), FakeConf(tmp_path), FakeLog())
    assert "backup" not in [name for name, _ in wiring["calls"]]


def test_the_loop_asks_the_folder_rather_than_remembering(tmp_path):
    """backup_due reads the backups folder on every call, so removing
    today's snapshot makes a backup due again."""
    conf = FakeConf(tmp_path)
    assert loop_module.backup_due(conf) is True

    path = snapshot_for(tmp_path, today_utc())
    assert loop_module.backup_due(conf) is False

    path.unlink()
    assert loop_module.backup_due(conf) is True


# prune
def test_prune_does_not_run_every_pass(tmp_path, wiring, monkeypatch):
    monkeypatch.setattr(Stopper, "sleep", lambda self, seconds: None)
    calls = wiring["calls"]

    passes = {"n": 0}

    def stop_after_three(self, seconds):
        passes["n"] += 1
        if passes["n"] >= 3:
            self.event.set()

    monkeypatch.setattr(Stopper, "sleep", stop_after_three)
    cmd_loop(Args(write=True, once=False), FakeConf(tmp_path), FakeLog())

    assert [name for name, _ in calls].count("sync") >= 3
    assert [name for name, _ in calls].count("prune") == 1


def test_a_dry_run_does_not_prune(tmp_path, wiring):
    """A dry run skips the prune."""
    cmd_loop(Args(write=False), FakeConf(tmp_path), FakeLog())
    assert "prune" not in [name for name, _ in wiring["calls"]]


def test_prune_always_writes_when_the_loop_writes(tmp_path, wiring):
    cmd_loop(Args(write=True), FakeConf(tmp_path), FakeLog())
    assert ("prune", True) in wiring["calls"]


# the lock
def test_a_second_loop_will_not_start(tmp_path, wiring):
    lock = Lock(tmp_path / "state" / "loop.pid")
    lock.path.parent.mkdir(parents=True, exist_ok=True)
    lock.path.write_text("%d\n" % os.getpid())

    log = FakeLog()
    code = cmd_loop(Args(), FakeConf(tmp_path), log)

    assert code == EXIT_BROKEN
    assert wiring["calls"] == [], "it started a pass anyway"


def test_a_lock_from_a_process_that_is_gone_does_not_block_a_start(tmp_path, wiring):
    """A lock file naming a pid that is not running (as after a power
    cut) is stale and does not stop the loop starting."""
    path = tmp_path / "state" / "loop.pid"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("999999\n")

    code = cmd_loop(Args(), FakeConf(tmp_path), FakeLog())
    assert code == EXIT_OK
    assert wiring["calls"], "it refused to start"


def test_a_lock_holding_nonsense_does_not_block_a_start(tmp_path, wiring):
    path = tmp_path / "state" / "loop.pid"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not a pid\n")

    assert cmd_loop(Args(), FakeConf(tmp_path), FakeLog()) == EXIT_OK


def test_the_lock_is_cleared_on_the_way_out(tmp_path, wiring):
    cmd_loop(Args(), FakeConf(tmp_path), FakeLog())
    assert not (tmp_path / "state" / "loop.pid").exists()


def test_the_lock_is_cleared_even_when_the_loop_gives_up(tmp_path, wiring):
    """The lock is released in a finally block, so a loop that gave up
    after repeated failures can be restarted."""
    wiring["install"](sync=wiring["recorder"]("sync", code=2))
    cmd_loop(Args(once=False), FakeConf(tmp_path), FakeLog())
    assert not (tmp_path / "state" / "loop.pid").exists()


def test_a_lock_is_not_removed_by_the_loop_that_did_not_take_it(tmp_path, wiring):
    path = tmp_path / "state" / "loop.pid"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("%d\n" % os.getpid())

    cmd_loop(Args(), FakeConf(tmp_path), FakeLog())
    assert path.exists(), "the loop that was turned away deleted the lock"


def test_releasing_a_lock_you_never_took_does_nothing(tmp_path):
    """Lock.release does nothing unless this Lock object took the lock.

    Tested on the Lock directly, because cmd_loop returns before its
    finally block when it is turned away. The pid in the file cannot tell
    two Lock objects in one process apart, so the `held` flag is what
    protects another holder's file.
    """
    path = tmp_path / "loop.pid"
    path.write_text("%d\n" % os.getpid())

    turned_away = Lock(path)
    assert turned_away.take() == os.getpid()
    turned_away.release()

    assert path.exists(), "it deleted a lock it never held"


def test_taking_a_lock_twice_is_not_a_conflict_with_yourself(tmp_path):
    lock = Lock(tmp_path / "loop.pid")
    assert lock.take() is None
    assert lock.take() is None, "the loop reported itself as a second loop"


# stopping
def test_stopping_does_not_wait_out_the_interval():
    """Setting the stop event ends a 30 second sleep at once."""
    stopper = Stopper()
    started = time.monotonic()
    threading.Timer(0.05, stopper.event.set).start()
    stopper.sleep(30)
    assert time.monotonic() - started < 5


def test_a_stop_ends_the_loop_rather_than_the_next_pass(tmp_path, wiring, monkeypatch):
    def stop_during_the_wait(self, seconds):
        self.event.set()

    monkeypatch.setattr(Stopper, "sleep", stop_during_the_wait)
    cmd_loop(Args(once=False), FakeConf(tmp_path), FakeLog())
    assert [name for name, _ in wiring["calls"]] == ["reverse", "sync"]


def test_the_handlers_are_put_back(tmp_path, wiring):
    """The previous SIGINT and SIGTERM handlers are restored when the
    loop exits, so code that runs afterwards (including the test runner)
    keeps its own."""
    import signal

    before = signal.getsignal(signal.SIGTERM)
    cmd_loop(Args(), FakeConf(tmp_path), FakeLog())
    assert signal.getsignal(signal.SIGTERM) is before


# the heartbeat
def test_the_heartbeat_says_the_loop_is_alive(tmp_path, wiring):
    """The heartbeat records the pid, the pass count, whether the loop
    is writing, the passes that ran and a timestamp."""
    cmd_loop(Args(), FakeConf(tmp_path), FakeLog())

    beat = heartbeat_file(tmp_path)
    assert beat["pid"] == os.getpid()
    assert beat["passes"] == 1
    assert beat["writing"] is False
    assert [p["pass"] for p in beat["last"]] == ["reverse", "sync"]
    assert beat["at"]


def test_the_heartbeat_is_written_even_when_the_pass_failed(tmp_path, wiring):
    """A failed pass still writes the heartbeat, with the failure
    counted."""
    wiring["install"](reverse=wiring["recorder"]("reverse", raises=RuntimeError("boom")))
    cmd_loop(Args(), FakeConf(tmp_path), FakeLog())
    assert heartbeat_file(tmp_path)["failures_in_a_row"] == 1


def test_the_heartbeat_is_replaced_whole(tmp_path):
    path = tmp_path / "beat.json"
    heartbeat(path, {"passes": 1})
    heartbeat(path, {"passes": 2})

    assert json.loads(path.read_text())["passes"] == 2
    assert list(tmp_path.glob("*.tmp")) == []


def test_a_heartbeat_that_fails_leaves_the_last_good_one_alone(monkeypatch, tmp_path):
    """The heartbeat is written to a .tmp file and renamed into place, so
    a write that fails leaves the previous heartbeat intact and readable.
    """
    path = tmp_path / "beat.json"
    heartbeat(path, {"passes": 1})

    def refuse(self, target):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", refuse)
    with pytest.raises(OSError):
        heartbeat(path, {"passes": 2})

    assert json.loads(path.read_text())["passes"] == 1


# the log
def test_a_clean_pass_does_not_put_the_whole_plan_in_the_log(tmp_path, wiring):
    """A clean pass logs one summary line ("sync exit 0"), not what the
    pass printed."""
    wiring["install"](sync=wiring["recorder"]("sync", says="a hundred lines of plan"))
    log = FakeLog()
    cmd_loop(Args(), FakeConf(tmp_path), log)

    assert "a hundred lines of plan" not in log.text()
    assert "sync exit 0" in log.text()


def test_a_failed_pass_puts_everything_it_said_in_the_log(tmp_path, wiring):
    wiring["install"](sync=wiring["recorder"]("sync", code=2, says="here is why"))
    log = FakeLog()
    cmd_loop(Args(), FakeConf(tmp_path), log)
    assert "here is why" in log.text()


def test_verbose_logs_a_clean_pass_too(tmp_path, wiring):
    """With --verbose, a clean pass's output is logged as well."""
    wiring["install"](sync=wiring["recorder"]("sync", says="a hundred lines of plan"))
    log = FakeLog()
    cmd_loop(Args(verbose=True), FakeConf(tmp_path), log)
    assert "a hundred lines of plan" in log.text()


def test_a_pass_does_not_print_to_the_terminal(tmp_path, wiring, capsys):
    """What a pass prints is captured, not written to the terminal."""
    wiring["install"](sync=wiring["recorder"]("sync", says="a hundred lines of plan"))
    cmd_loop(Args(), FakeConf(tmp_path), FakeLog())
    assert "a hundred lines of plan" not in capsys.readouterr().out


def test_run_pass_hands_back_what_the_pass_printed(tmp_path):
    """run_pass returns the captured output, so the loop can decide
    whether to log it."""
    def says_something(args, conf, log):
        print("something")
        return EXIT_OK

    out = run_pass("test", says_something, PassArgs(), FakeConf(tmp_path), FakeLog())
    assert out["output"].strip() == "something"
    assert out["clean"] is True


# the shape of the arguments handed down
def test_the_pass_arguments_are_only_what_the_commands_read(tmp_path):
    """PassArgs is a plain object, so reading a flag it does not define
    raises AttributeError instead of returning a default."""
    args = PassArgs()
    with pytest.raises(AttributeError):
        args.something_nobody_set


# the silence watchdog (check_ops_silence)
def silent_for(tmp_path, hours):
    """A validation heartbeat that says the ops channel went quiet."""
    when = datetime.now(timezone.utc) - timedelta(hours=hours)
    path = tmp_path / "state" / "validation-heartbeat.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"last_message_at": when.isoformat(),
                                "run_id": "old", "hook": "on_pass"}), encoding="utf-8")
    return path


class SilenceConf(FakeConf):
    """FakeConf that can also find the real rules file."""

    def __init__(self, repo_root, **kwargs):
        super().__init__(repo_root, **kwargs)
        self.values["validator.rules_file"] = "config/validation-rules.yaml"
        real = Path(__file__).resolve().parent.parent / "config" / "validation-rules.yaml"
        target = self.repo_root / "config" / "validation-rules.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(real.read_text(encoding="utf-8"), encoding="utf-8")


def test_a_quiet_validator_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(loop_module, "tell_ops_the_validator_is_quiet",
                        lambda conf, log, message: "sent")
    silent_for(tmp_path, 40)
    state = check_ops_silence(SilenceConf(tmp_path), FakeLog())
    assert state["tripped"] is True
    assert state["quiet_hours"] == 40.0


def test_a_validator_that_spoke_this_morning_is_left_alone(tmp_path, monkeypatch):
    told = []
    monkeypatch.setattr(loop_module, "tell_ops_the_validator_is_quiet",
                        lambda conf, log, message: told.append(message))
    silent_for(tmp_path, 2)
    state = check_ops_silence(SilenceConf(tmp_path), FakeLog())
    assert state["tripped"] is False
    assert told == []


def test_the_silence_check_cannot_take_the_loop_down(tmp_path, wiring):
    """With no rules file at all the watchdog cannot run, and the loop
    still exits cleanly and records checked=False in the heartbeat."""
    log = FakeLog()
    code = cmd_loop(Args(write=True), FakeConf(tmp_path), log)
    assert code == EXIT_OK
    beat = heartbeat_file(tmp_path)
    assert beat["ops_silence"]["checked"] is False


def test_a_tripped_check_is_not_a_failed_pass(tmp_path, wiring, monkeypatch):
    """A tripped watchdog is recorded in the heartbeat but does not
    increase failures_in_a_row."""
    monkeypatch.setattr(loop_module, "tell_ops_the_validator_is_quiet",
                        lambda conf, log, message: "sent")
    silent_for(tmp_path, 40)
    cmd_loop(Args(write=True), SilenceConf(tmp_path), FakeLog())

    beat = heartbeat_file(tmp_path)
    assert beat["ops_silence"]["tripped"] is True
    assert beat["failures_in_a_row"] == 0


def test_the_alert_does_not_reset_the_clock_it_measures(tmp_path, wiring, monkeypatch):
    """Sending the silence alert does not touch the validation heartbeat
    that the watchdog measures."""
    monkeypatch.setattr(loop_module, "tell_ops_the_validator_is_quiet",
                        lambda conf, log, message: "sent")
    beat_path = silent_for(tmp_path, 40)
    before = beat_path.read_text(encoding="utf-8")
    cmd_loop(Args(write=True), SilenceConf(tmp_path), FakeLog())
    assert beat_path.read_text(encoding="utf-8") == before


def test_it_is_asked_once_a_day_not_once_a_minute(tmp_path, wiring, monkeypatch):
    """The watchdog runs once per day, not on every pass."""
    asked = []
    monkeypatch.setattr(loop_module, "check_ops_silence",
                        lambda conf, log: asked.append(1) or {"tripped": False})

    passes = {"n": 0}

    def stop_after_three(self, seconds):
        passes["n"] += 1
        if passes["n"] >= 3:
            self.event.set()

    monkeypatch.setattr(Stopper, "sleep", stop_after_three)
    cmd_loop(Args(write=True, once=False), SilenceConf(tmp_path), FakeLog())
    assert len(asked) == 1


def test_a_dry_run_does_not_ask(tmp_path, wiring, monkeypatch):
    asked = []
    monkeypatch.setattr(loop_module, "check_ops_silence",
                        lambda conf, log: asked.append(1) or {"tripped": False})
    cmd_loop(Args(write=False), SilenceConf(tmp_path), FakeLog())
    assert asked == []


# validation
#
# The loop runs the validator itself when the ops channel has been quiet
# for validator.auto_run_interval_hours, so validation does not depend on
# someone typing the command.
def test_the_loop_runs_the_validator_itself(tmp_path, wiring):
    """A writing loop with no validation heartbeat runs the validator."""
    cmd_loop(Args(write=True), FakeConf(tmp_path), FakeLog())
    assert "validate" in [name for name, _ in wiring["calls"]]


def test_a_dry_run_does_not_validate(tmp_path, wiring):
    """A dry run skips the validator, which has no read-only mode (it
    sends Telegram messages and opens Todoist tasks)."""
    cmd_loop(Args(write=False), FakeConf(tmp_path), FakeLog())
    assert "validate" not in [name for name, _ in wiring["calls"]]


def test_a_validator_that_spoke_recently_is_not_run_again(tmp_path, wiring):
    """A validator heard from 2 hours ago is not run again."""
    silent_for(tmp_path, 2)
    cmd_loop(Args(write=True), FakeConf(tmp_path), FakeLog())
    assert "validate" not in [name for name, _ in wiring["calls"]]


def test_a_validator_that_has_gone_quiet_is_run(tmp_path, wiring):
    silent_for(tmp_path, 13)
    cmd_loop(Args(write=True), FakeConf(tmp_path), FakeLog())
    assert "validate" in [name for name, _ in wiring["calls"]]


def test_a_validator_that_has_never_spoken_is_run(tmp_path):
    """No validation heartbeat at all counts as due."""
    assert validation_due(FakeConf(tmp_path)) is True


def test_the_interval_can_be_turned_off(tmp_path):
    """An interval of 0 turns automatic runs off, however long the
    channel has been quiet."""
    conf = FakeConf(tmp_path)
    conf.values["validator.auto_run_interval_hours"] = 0
    silent_for(tmp_path, 500)
    assert validation_due(conf) is False


def test_it_asks_the_heartbeat_rather_than_remembering(tmp_path):
    """validation_due reads the heartbeat file on every call, so removing
    it makes a run due again, and a restart does not reset the schedule."""
    conf = FakeConf(tmp_path)
    path = silent_for(tmp_path, 2)
    assert validation_due(conf) is False

    path.unlink()
    assert validation_due(conf) is True


def test_validation_runs_before_the_silence_is_reported(tmp_path, wiring, monkeypatch):
    """The validator runs before the silence watchdog. After 40 quiet
    hours, a validator run that reports (refreshing the heartbeat) means
    the watchdog in the same pass does not trip.
    """
    told = []
    monkeypatch.setattr(loop_module, "tell_ops_the_validator_is_quiet",
                        lambda conf, log, message: told.append(message))

    conf = SilenceConf(tmp_path)
    silent_for(tmp_path, 40)

    def validate_and_speak(args, conf_, log):
        # A real run refreshes the heartbeat when Telegram accepts a send.
        silent_for(tmp_path, 0)
        return EXIT_OK

    wiring["install"](validate=validate_and_speak)
    cmd_loop(Args(write=True), conf, FakeLog())

    assert told == [], "it reported a silence it was about to end itself"
    assert heartbeat_file(tmp_path)["ops_silence"]["tripped"] is False


def test_a_blocked_verdict_is_not_a_failed_pass(tmp_path, monkeypatch):
    """cmd_validate_for_loop turns the validator's BLOCKED exit code (1)
    into EXIT_OK, because a BLOCKED verdict is a finding about the data,
    not a failed pass.
    """
    monkeypatch.setattr(loop_module.validator_cli, "cmd_run",
                        lambda args, conf, log: validator_cli_module.EXIT_BLOCKED)
    code = cmd_validate_for_loop(ValidateArgs(), FakeConf(tmp_path), FakeLog())
    assert code == EXIT_OK


def test_a_validator_that_could_not_run_is_a_failed_pass(tmp_path, monkeypatch):
    """Exit 2 (the validator could not run) is passed through as a
    failure."""
    monkeypatch.setattr(loop_module.validator_cli, "cmd_run",
                        lambda args, conf, log: 2)
    assert cmd_validate_for_loop(ValidateArgs(), FakeConf(tmp_path), FakeLog()) == 2


def test_a_clean_verdict_stays_clean(tmp_path, monkeypatch):
    monkeypatch.setattr(loop_module.validator_cli, "cmd_run",
                        lambda args, conf, log: EXIT_OK)
    assert cmd_validate_for_loop(ValidateArgs(), FakeConf(tmp_path), FakeLog()) == EXIT_OK


def test_a_blocked_verdict_does_not_back_the_loop_off(tmp_path, wiring):
    """Through the whole loop: a BLOCKED verdict leaves
    failures_in_a_row at zero."""
    wiring["install"](validate=lambda args, conf, log: validator_cli_module.EXIT_BLOCKED)
    cmd_loop(Args(write=True), FakeConf(tmp_path), FakeLog())
    assert heartbeat_file(tmp_path)["failures_in_a_row"] == 0


def test_a_validator_that_raises_does_not_take_the_loop_down(tmp_path, wiring):
    wiring["install"](validate=wiring["recorder"]("validate", raises=RuntimeError("no pb")))
    log = FakeLog()
    code = cmd_loop(Args(write=True), FakeConf(tmp_path), log)
    assert code == EXIT_OK
    assert "no pb" in log.text()


def test_the_validate_arguments_are_only_what_cmd_run_reads(tmp_path):
    """ValidateArgs is a plain object with only the flags cmd_run reads,
    so it has no .write and an unknown flag raises AttributeError."""
    args = ValidateArgs()
    with pytest.raises(AttributeError):
        args.something_nobody_set
    with pytest.raises(AttributeError):
        args.write


def test_an_automatic_run_notifies(tmp_path):
    """Automatic runs notify by default, so their results reach the ops
    channel."""
    assert ValidateArgs().notify is True


def test_an_automatic_run_says_it_was_the_daemon(tmp_path):
    """Automatic runs are recorded with trigger="daemon", so they can be
    told apart from runs started by a person."""
    assert ValidateArgs().trigger == "daemon"


# the sweep


def test_the_sweep_runs_when_due_and_is_marked_attempted(tmp_path, wiring):
    wiring["install"](sweep_is_due=True)
    cmd_loop(Args(write=True), FakeConf(tmp_path), FakeLog())
    assert ("sweep", True) in wiring["calls"]
    marker = tmp_path / "state" / "last-sweep-day.txt"
    today = datetime.now().astimezone().date().isoformat()
    assert marker.read_text(encoding="utf-8").strip() == today


def test_the_sweep_stays_out_of_a_pass_where_it_is_not_due(tmp_path, wiring):
    cmd_loop(Args(write=True), FakeConf(tmp_path), FakeLog())
    assert "sweep" not in [name for name, _ in wiring["calls"]]


def test_a_dry_loop_never_sweeps(tmp_path, wiring):
    """A dry run never sweeps, even when a sweep is due."""
    wiring["install"](sweep_is_due=True)
    cmd_loop(Args(write=False), FakeConf(tmp_path), FakeLog())
    assert "sweep" not in [name for name, _ in wiring["calls"]]


def test_a_failed_sweep_is_not_retried_into_the_working_day(tmp_path, wiring):
    """A sweep that raises is still marked attempted, so it is not
    retried later the same day, and the loop exits cleanly."""
    wiring["install"](
        sweep=wiring["recorder"]("sweep", raises=RuntimeError("no todoist")),
        sweep_is_due=True)
    code = cmd_loop(Args(write=True), FakeConf(tmp_path), FakeLog())
    assert code == EXIT_OK
    assert (tmp_path / "state" / "last-sweep-day.txt").exists()


def test_sweep_due_waits_for_the_morning(tmp_path):
    conf = FakeConf(tmp_path)
    night = datetime(2026, 9, 23, 4, 59, tzinfo=timezone.utc)
    assert sweep_due(conf, now=night) is False


def test_sweep_due_reads_the_marker_not_a_variable(tmp_path):
    """sweep_due reads the marker file: once today is marked the sweep is
    not due, and the next day it is due again."""
    conf = FakeConf(tmp_path)
    morning = datetime(2026, 9, 23, 5, 0, tzinfo=timezone.utc)
    assert sweep_due(conf, now=morning) is True
    mark_sweep_attempted(conf, now=morning)
    assert sweep_due(conf, now=morning) is False
    next_day = datetime(2026, 9, 24, 5, 0, tzinfo=timezone.utc)
    assert sweep_due(conf, now=next_day) is True
