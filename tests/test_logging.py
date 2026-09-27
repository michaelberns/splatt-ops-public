"""
Tests for core/logging_setup.py.

What is pinned down
    - Every logger gets a size-capped rotating log file by default, with
      no opt-in needed, and the file really rotates.
    - The long-running callers (the daemon loop) use that file, and
      bin/start does not also append the loop's console output to a
      second file that would grow without limit.
    - Every line carries the run id, including lines from child loggers
      and lines written when the console handler is absent.

Each test uses its own logger name. The logging module keeps loggers in
a global registry and setup() returns early for a logger that already
has handlers, so two tests sharing a name would interfere.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

import pytest

from core import logging_setup


REPO = Path(__file__).resolve().parent.parent
START = REPO / "bin" / "start"


@pytest.fixture
def name():
    """A logger name nothing else in the process has used."""
    made = "test_%s" % uuid.uuid4().hex[:10]
    yield made
    logger = logging.getLogger(made)
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


def files_of(logger):
    return [h for h in logger.handlers
            if isinstance(h, logging.handlers.RotatingFileHandler)]


# ==================================================================
# On by default
# ==================================================================

def test_a_logger_writes_to_a_rotating_file_without_being_asked(name, tmp_path):
    # No opt-in: calling setup() is enough to get a rotating file.
    logger = logging_setup.setup(name, log_dir=tmp_path)

    assert files_of(logger), "no rotating file handler was attached"


def test_the_default_is_the_repo_logs_folder(name):
    # The default is <repo>/logs, not the current working directory, so
    # logs land in the same place wherever bin/start is run from.
    logger = logging_setup.setup(name)

    handlers = files_of(logger)
    assert handlers
    assert Path(handlers[0].baseFilename).parent == REPO / "logs"


def test_the_file_is_named_after_the_logger(name):
    logger = logging_setup.setup(name)

    assert Path(files_of(logger)[0].baseFilename).name == "%s.log" % name


def test_opting_out_has_to_be_said_out_loud(name):
    # Passing NO_FILE is the explicit way to get console-only logging.
    logger = logging_setup.setup(name, log_dir=logging_setup.NO_FILE)

    assert not files_of(logger)


def test_a_folder_that_cannot_be_written_does_not_stop_the_run(name, tmp_path,
                                                               monkeypatch):
    # A log folder that cannot be created (read-only checkout, missing
    # permission) leaves the logger with its console handler instead of
    # stopping the process.
    def refuse(*args, **kwargs):
        raise OSError("read only file system")

    monkeypatch.setattr(Path, "mkdir", refuse)
    logger = logging_setup.setup(name, log_dir=tmp_path / "nope")

    assert not files_of(logger)
    assert logger.handlers, "the console handler should still be there"


# ==================================================================
# It actually rotates
# ==================================================================

def test_the_file_is_capped_and_old_ones_are_kept(name, tmp_path):
    # Writes enough lines to force several rollovers, then checks that
    # every file stays under the cap and only backup_count old files are
    # kept. Configuring maxBytes is not enough; it has to take effect.
    logger = logging_setup.setup(name, log_dir=tmp_path,
                                 max_bytes=2000, backup_count=2)
    for i in range(400):
        logger.info("a line long enough to make this add up, number %d", i)
    for handler in files_of(logger):
        handler.flush()

    written = sorted(p.name for p in tmp_path.iterdir())
    assert len(written) > 1, "nothing rotated, so nothing is capped"
    for path in tmp_path.iterdir():
        assert path.stat().st_size < 10000, "%s blew past the cap" % path.name
    # backup_count of 2 means the live file plus two, and no more.
    assert len(written) <= 3, written


def test_the_run_id_is_on_every_line(name, tmp_path):
    # The run id lets one cycle be pulled out of a busy file with grep.
    logger = logging_setup.setup(name, log_dir=tmp_path)
    logger.info("something happened")
    for handler in files_of(logger):
        handler.flush()

    body = (tmp_path / ("%s.log" % name)).read_text(encoding="utf-8")
    assert logging_setup.RUN_ID in body


# ==================================================================
# The callers that run forever
# ==================================================================

def test_the_daemon_asks_for_a_file_handler():
    # daemon/cli.py calls setup("daemon") with no log_dir. The default
    # must give the daemon, which runs indefinitely, a rotating file.
    logger = logging.getLogger("daemon")
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)

    from daemon import cli

    built = cli.setup("daemon")
    try:
        assert files_of(built), "the daemon logger has no rotating file"
    finally:
        for handler in list(built.handlers):
            handler.close()
            built.removeHandler(handler)


def test_the_loop_console_is_not_appended_to_a_file_that_never_rotates():
    # The loop already logs to a rotating file. Redirecting its console to
    # another file with >> would duplicate that output in a file that is
    # never rotated. Checked by reading bin/start, since a test cannot run
    # the real loop long enough to watch a file grow.
    body = START.read_text(encoding="utf-8")
    for line in body.splitlines():
        if "daemon loop" not in line:
            continue
        if line.strip().startswith("#"):
            continue
        assert ">>" not in line, (
            "bin/start appends the loop console to a file: %s" % line.strip())


# ==================================================================
# Where the run id filter goes
# ==================================================================

def test_a_line_from_a_child_logger_keeps_the_run_id(name, tmp_path):
    # A record from a child logger (for example daemon.sync reaching the
    # daemon handlers) bypasses filters attached to the parent logger. If
    # the run-id filter were on the logger, the formatter would fail on
    # the missing run_id and drop the line from both file and console.
    # With the filter on each handler, the line is written with its run id.
    logger = logging_setup.setup(name, log_dir=tmp_path)
    child = logging.getLogger("%s.child" % name)
    child.info("something a sub module said")
    for handler in files_of(logger):
        handler.flush()

    body = (tmp_path / ("%s.log" % name)).read_text(encoding="utf-8")
    assert "something a sub module said" in body, "the child's line was dropped"
    assert logging_setup.RUN_ID in body


def test_the_file_handler_does_not_lean_on_the_console_one(name, tmp_path):
    # Every handler needs its own run-id filter. All handlers share one
    # record object, so while the console handler (attached first) is
    # present its filter sets run_id for the file handler too, and a
    # missing file filter would go unnoticed. Removing the console handler
    # checks that the file handler works on its own.
    logger = logging_setup.setup(name, log_dir=tmp_path)
    for handler in list(logger.handlers):
        if type(handler) is logging.StreamHandler:
            logger.removeHandler(handler)
    assert files_of(logger), "the file handler went with it"

    logger.info("said with nothing else listening")
    for handler in files_of(logger):
        handler.flush()

    body = (tmp_path / ("%s.log" % name)).read_text(encoding="utf-8")
    assert "said with nothing else listening" in body, "the line was dropped"
    assert logging_setup.RUN_ID in body
