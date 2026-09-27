"""
Backup: a full JSON snapshot of every collection listed in core/schema.py.

Usage
    python3 -m tools.backup                 write a snapshot to backups/
    python3 -m tools.backup --out FILE      write it somewhere else
    python3 -m tools.backup --verify FILE   read one back and check it

What it writes
    backups/snapshot-YYYYMMDD-HHMMSS.json (UTC time), holding when it was
    taken, the run id, a record count per collection, and every record.
    The file is read back and verified straight after it is written.

Why it matters
    The passes that write to the database (daemon/sync.py, daemon/reverse.py
    and daemon/triggers.py) refuse to write unless a snapshot from today
    exists (`taken_today`). The daemon loop therefore takes one itself,
    once a day, before its first write; otherwise writing would stop at
    midnight until someone ran this by hand.

    The format is plain JSON with no compression, so a backup can be read
    in a text editor and does not need this code to restore it.

`cmd_backup` is the entry point the daemon loop calls. `main()` wraps the
same function in a command line, so there is only one snapshot code path.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path

from core import schema
from core.config import ConfigError, settings
from core.logging_setup import RUN_ID, setup
from core.pb import PocketBaseClient


def snapshot(pb, collections, log):
    data = {}
    for name in collections:
        try:
            rows = pb.list_all(name)
        except Exception as exc:
            log.error("could not read %s: %s", name, exc)
            raise
        data[name] = rows
        log.info("  %-28s %5d records", name, len(rows))
    return data


def write_snapshot(data, path, log):
    payload = {
        "taken_at": datetime.now(timezone.utc).isoformat(),
        "run_id": RUN_ID,
        "counts": {name: len(rows) for name, rows in data.items()},
        "collections": data,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1, ensure_ascii=False)
    log.info("snapshot written to %s", path)
    return path


def verify(path, log):
    """Read a snapshot back and check it holds what it claims to hold.

    Every collection must hold as many records as its count says, and every
    record must have an id. Prints a summary and returns 0 when sound, or
    logs the problems and returns 1.
    """
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)

    claimed = payload.get("counts") or {}
    actual = {name: len(rows) for name, rows in (payload.get("collections") or {}).items()}

    problems = []
    for name, count in claimed.items():
        if actual.get(name) != count:
            problems.append("%s claims %d records but holds %d"
                            % (name, count, actual.get(name, 0)))
    for name, rows in (payload.get("collections") or {}).items():
        for row in rows:
            if not row.get("id"):
                problems.append("%s has a record with no id" % name)
                break

    total = sum(actual.values())
    if problems:
        for line in problems:
            log.error("  %s", line)
        print("BACKUP IS NOT SOUND. Do not delete anything.")
        return 1

    print("Backup verified. %d records across %d collections, taken %s."
          % (total, len(actual), payload.get("taken_at")))
    for name in sorted(actual):
        print("  %-28s %5d" % (name, actual[name]))
    return 0


def latest(backup_dir):
    """The most recent snapshot, or None."""
    if not backup_dir.exists():
        return None
    files = sorted(backup_dir.glob("snapshot-*.json"))
    return files[-1] if files else None


def taken_today(backup_dir):
    """True when the newest snapshot in `backup_dir` is from today (UTC).
    Every pass that writes to the database checks this first."""
    newest = latest(backup_dir)
    if not newest:
        return False
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return stamp in newest.name


def how_to_take_one():
    """The command to take a backup, printed by passes that refuse without one.

    It names sys.executable rather than a bare "python3": the interpreter
    that is running now got far enough to make this check, so it can import
    everything the backup needs, while a bare python3 on PATH may be a
    different interpreter without httpx installed.

    >>> how_to_take_one()  # doctest: +SKIP
    'Run: /path/to/repo/.venv/bin/python -m tools.backup'
    """
    return "Run: %s -m tools.backup" % shlex.quote(sys.executable)


def cmd_backup(args, conf, log, out=""):
    """Take one snapshot and verify it. Returns 0, 1 (not sound) or 2
    (PocketBase unreachable).

    The signature matches the other daemon passes, fn(args, conf, log), so
    the loop calls it the same way it calls reverse, sync and prune and
    reuses the config it already loaded.

    `out` is a keyword argument rather than an attribute of `args` because
    the loop's `args` only carries write, quiet and json; reading a fourth
    attribute would work from the command line and fail inside the loop.

    `args` is not read at all. A backup behaves the same in a dry run and a
    real run: it only reads the database and adds a new file, and a dry run
    that skipped it would leave the passes after it refusing to write.
    """
    del args

    pb = PocketBaseClient(**conf.pocketbase())
    if not pb.health():
        log.error("PocketBase is not reachable, so no backup can be taken")
        print("PocketBase is not reachable, so no backup can be taken.")
        pb.close()
        return 2
    pb.auth_admin()

    names = sorted(schema.collections())
    log.info("backing up %d collections", len(names))
    data = snapshot(pb, names, log)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = Path(out) if out else (
        conf.repo_root / "backups" / ("snapshot-%s.json" % stamp))
    write_snapshot(data, path, log)
    pb.close()

    print("")
    return verify(path, log)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="backup")
    parser.add_argument("--verify", default="", help="verify an existing snapshot")
    parser.add_argument("--out", default="", help="where to write it")
    args = parser.parse_args(argv)

    log = setup("backup")
    if args.verify:
        return verify(Path(args.verify), log)

    try:
        conf = settings()
    except ConfigError as exc:
        log.error("config could not be loaded: %s", exc)
        return 2

    return cmd_backup(args, conf, log, out=args.out)


if __name__ == "__main__":
    sys.exit(main())
