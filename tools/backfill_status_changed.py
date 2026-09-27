"""
Stamp jobs.status_changed_at on jobs that do not have it yet.

    python -m tools.backfill_status_changed              show what would change
    python -m tools.backfill_status_changed --write      apply it

A small, idempotent helper for adopting the system on an existing
database. The trigger engine's chase rule (core/triggers.py, R2) starts
its seven-day clock at `status_changed_at` and refuses to guess when the
field is empty. Jobs created before the field existed have it empty, so
without this helper the chase rule would ignore them until each one
happened to change status again.

What it writes
    For each job with an empty `status_changed_at`, the job's `updated`
    timestamp (or `created` when there is no `updated`), and
    `status_changed_by` = "backfill". Jobs with neither timestamp are
    reported and left alone rather than given an invented date.

Why `updated` and not `created`
    Neither is the true moment of the last status change. `created` is
    when the job was opened, which on a long project is months before the
    quote went out, and would make every older quoted job due a chase at
    once. `updated` is the last change to the record, which for a job
    sitting untouched in "quoted" is usually the day it was quoted. It is
    the closer of the two, and it errs late rather than early.

Why "backfill" in status_changed_by
    So nobody later reads the stamp as a record made at the time of the
    change. It is a best estimate made afterwards, and the field says so.

Safe to re-run
    It only fills blanks, so running it twice, or after the trigger engine
    has stamped a job properly, changes nothing. Every write goes through
    the write ledger (core/ledger.py) like any other write.
"""

from __future__ import annotations

import sys

from core.config import settings
from core.ledger import RecordingClient, WriteLedger, ledger_path
from core.logging_setup import setup
from core.pb import PocketBaseClient

log = setup("backfill_status_changed")

#: What goes in status_changed_by, marking the stamp as an estimate made
#: afterwards rather than a record made when the status changed.
SOURCE = "backfill"

FIELD_AT = "status_changed_at"
FIELD_BY = "status_changed_by"


def _empty(value):
    return value is None or str(value).strip() == ""


def plan(jobs):
    """Which jobs need a stamp and what it would be.

    A pure function, so the decision can be tested without a database.
    Returns {"todo": rows to write, "already": jobs that have a stamp,
    "no_source": jobs with no timestamp to copy}.
    """
    todo, already, no_source = [], [], []
    for job in jobs or []:
        if not _empty(job.get(FIELD_AT)):
            already.append(job)
            continue
        stamp = job.get("updated") or job.get("created") or ""
        if _empty(stamp):
            # No timestamp to copy. An invented date would start a chase
            # clock nobody set, so the job is reported and left alone.
            no_source.append(job)
            continue
        todo.append({
            "id": job.get("id"),
            "title": job.get("title") or job.get("id"),
            "status": job.get("status") or "",
            "from": "updated" if job.get("updated") else "created",
            "stamp": str(stamp),
        })
    return {"todo": todo, "already": already, "no_source": no_source}


def describe(item, write):
    """The plan as printed text. Says plainly when nothing was written."""
    lines = [
        "",
        "Jobs already stamped: %d" % len(item["already"]),
        "Jobs to stamp: %d" % len(item["todo"]),
        "",
    ]
    for row in item["todo"][:200]:
        lines.append("  %-12s %-40s %s (from %s)"
                     % (row["status"], row["title"][:40], row["stamp"][:19],
                        row["from"]))
    if len(item["todo"]) > 200:
        lines.append("  and %d more" % (len(item["todo"]) - 200))
    if item["no_source"]:
        lines.append("")
        lines.append("Jobs with no timestamp to copy, left alone: %d"
                     % len(item["no_source"]))
        for job in item["no_source"][:20]:
            lines.append("  %s" % (job.get("title") or job.get("id")))
    lines.append("")
    if not write:
        lines.append("Nothing was written. Run again with --write to apply this.")
    return "\n".join(lines)


def apply(client, item):
    """Write each stamp. Returns (number written, list of failure messages).

    `client` is normally a RecordingClient, so every write is ledgered.
    """
    done, failures = 0, []
    for row in item["todo"]:
        try:
            client.update("jobs", row["id"],
                          {FIELD_AT: row["stamp"], FIELD_BY: SOURCE})
            done += 1
        except Exception as exc:
            failures.append("%s: %s" % (row["title"], exc))
            log.warning("could not stamp %s: %s", row["id"], exc)
    return done, failures


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    write = "--write" in argv

    conf = settings()
    pb = PocketBaseClient(**conf.pocketbase())
    if not pb.health():
        print("PocketBase is not reachable. Nothing was read and nothing changed.")
        return 2
    pb.auth_admin()

    if FIELD_AT not in set(pb.field_names("jobs") or []):
        print("jobs.%s does not exist yet. Run this first:" % FIELD_AT)
        print("  python -m tools.migrate --write")
        pb.close()
        return 2

    item = plan(pb.list_all("jobs"))
    print(describe(item, write))

    if not write:
        pb.close()
        return 0

    ledger = WriteLedger(ledger_path(conf))
    client = RecordingClient(pb, ledger, source="tools.backfill_status_changed")
    done, failures = apply(client, item)

    print("Stamped %d job%s." % (done, "" if done == 1 else "s"))
    for line in failures:
        print("  failed: %s" % line)
    pb.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
