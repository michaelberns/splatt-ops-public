"""
Logging setup shared by every process (daemon, validator, tools).

What it does
    `setup(name)` returns a logger with two handlers:
      1. a console handler, and
      2. a size-capped rotating file handler, `logs/<name>.log` in the repo
         by default (10 MB per file, five old files kept).
    Every line carries the run id, for example

        2026-08-08 09:15:00 INFO    [3f9a1c2e] daemon.sync: 4 tasks updated

    so one cycle can be pulled out of a busy file with grep.

The run id
    `RUN_ID` is taken from the SPLATT_RUN_ID environment variable, or
    generated once per process. The write ledger (core/ledger.py) stamps
    its claims with the same id, which is how the validator finds the
    writes that belong to one run.

Design choices
    - The rotating file is on by default. The daemon loop runs every
      minute indefinitely, so a log that is only rotated when a caller
      asks for it would eventually fill the disk. Opting out means passing
      `log_dir=NO_FILE` explicitly.
    - The run-id filter is attached to each handler, not to the logger
      (see the comment in `setup`).
    - A log folder that cannot be created does not stop the process; it
      falls back to console-only logging.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import uuid
from pathlib import Path

RUN_ID = os.environ.get("SPLATT_RUN_ID") or uuid.uuid4().hex[:8]

REPO_ROOT = Path(__file__).resolve().parent.parent

# Default for `log_dir`, meaning "the repo's logs folder". A sentinel
# object rather than the path itself, so that None can keep a distinct
# meaning (no file) and the default cannot be passed by accident.
DEFAULT = object()

# Pass this as `log_dir` to get console-only logging. A named constant so
# the opt-out reads as deliberate at the call site.
NO_FILE = None


class RunIdFilter(logging.Filter):
    """Adds `run_id` to each record so the formatter can print it."""

    def filter(self, record):
        record.run_id = RUN_ID
        return True


def setup(name="splatt", log_dir=DEFAULT, level=logging.INFO,
          max_bytes=10 * 1024 * 1024, backup_count=5):
    """Configure and return the logger called `name`.

    Calling it again for a logger that already has handlers returns that
    logger unchanged, so repeated calls do not duplicate output.
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(level)
    # The run-id filter goes on each handler, not on the logger. A filter
    # on a logger only sees records created at that logger; a record from
    # a child logger (for example `daemon.sync` propagating to the
    # `daemon` handlers) would skip it, the formatter would then fail on
    # the missing `run_id`, and the line would be dropped. Each handler
    # gets its own filter so neither depends on the other having run.
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(run_id)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.addFilter(RunIdFilter())
    logger.addHandler(console)

    if log_dir is DEFAULT:
        log_dir = REPO_ROOT / "logs"

    if log_dir:
        path = Path(log_dir).expanduser()
        try:
            path.mkdir(parents=True, exist_ok=True)
            rotating = logging.handlers.RotatingFileHandler(
                path / ("%s.log" % name), maxBytes=max_bytes,
                backupCount=backup_count
            )
        except OSError:
            # Read-only checkout or no permission. Keep running with the
            # console handler rather than refusing to start.
            return logger
        rotating.setFormatter(fmt)
        rotating.addFilter(RunIdFilter())
        logger.addHandler(rotating)

    return logger
