"""
Prune: delete sync log records older than the keep window.

    python -m daemon prune             say what it would delete, delete nothing
    python -m daemon prune --write     delete it
    python -m daemon prune --json      the plan as data

What it does
    The `sync_log` collection is an event log. This command removes
    records older than `daemon.sync_log_keep_days` (30 by default), at
    most `daemon.sync_log_max_deletes` (5000 by default) per run. The loop
    runs it with --write once a day. It is the only command in the daemon
    that deletes anything, so it deletes nothing without --write, and with
    --write every delete goes through `RecordingClient` into the write
    ledger.

How a run works
    1. Find the collection: `sync_log`, or the misspelled `sync_1og`
       (see CANONICAL and TYPO below).
    2. Ask the collection which date field it has, `created` or
       `occurred_at`. If it has neither, stop and report a refusal
       (exit 2). Records that cannot be aged cannot be pruned safely.
    3. Ask PocketBase for the records older than the cutoff, filtered and
       sorted oldest first on the server, page by page up to the cap.
    4. With --write, delete them one at a time. Every failure is counted
       and the first few are listed. Twenty failures with nothing deleted
       stops the run, because that points at a systematic problem such as
       permissions.
    5. Report what was found, deleted and failed, and whether the cap was
       reached. A capped run carries on from the oldest remaining record
       next time.

Why it is written this way
    - Filtering and sorting on the server means each page holds only
      records that should go, oldest first, so a run cannot look at a
      page of recent records and wrongly conclude there is nothing to do.
    - Reading every page up to the cap lets the prune keep up with a
      collection that gains more than one page of records a day.
    - The date field is looked up, not assumed. PocketBase only has a
      `created` field on a collection that defines one (the sync_log in
      pb_migrations/ has only `occurred_at`), and a filter or sort on a
      missing field fails with HTTP 400.
    - Failures are counted and reported, so a run that deleted nothing
      can never look like a clean run.
    - The cap stops an unattended run from turning a bad filter into an
      empty collection.

The text log files in logs/ are separate from this collection; they are
rotated by core/logging_setup.py.

Exit codes: 0 clean, 1 some deletes failed, 2 could not run or refused.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from core.ledger import RecordingClient, WriteLedger, ledger_path
from core.logging_setup import RUN_ID
from core.pb import PocketBaseClient

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_BROKEN = 2

# The collection name this repo declares, and a misspelling of it (the
# letter l replaced by the digit 1, hard to see in most fonts) that some
# databases were created with. Both are looked for, because a prune that
# only knew the correct name would find nothing in such a database and
# report a clean run every day. The report says which name was found.
CANONICAL = "sync_log"
TYPO = "sync_1og"

# Most records deleted in one run, unless settings say otherwise. A
# ceiling keeps a bad filter in an unattended run from emptying the
# collection. Hitting the cap is reported, and the next run carries on
# where this one stopped.
DEFAULT_MAX_DELETES = 5000


def cutoff_iso(keep_days, now=None):
    """The moment before which a record is old enough to delete, in UTC.

    Formatted the way PocketBase wants it in a filter, with a space
    between the date and the time rather than a T:

    >>> cutoff_iso(30, datetime(2026, 8, 10, 12, tzinfo=timezone.utc))
    '2026-07-11 12:00:00'
    """
    moment = (now or datetime.now(timezone.utc)) - timedelta(days=int(keep_days))
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def which_collection(pb, names=(CANONICAL, TYPO)):
    """The first of the candidate names that exists, or None.

    Checked by listing one record rather than by reading the collection
    list, because that also proves the collection can be read with these
    credentials. A collection that exists but cannot be read is treated
    the same as a missing one, and is found here rather than halfway
    through a delete loop.
    """
    for name in names:
        try:
            pb.list(name, per_page=1)
            return name
        except Exception:
            continue
    return None


# The fields that can hold a record's age, best first.
#
# `created` is filled in by PocketBase itself on a collection that defines
# it, so it is never blank. `occurred_at` is written by whatever logged
# the event, so it can be. The sync_log in the initial schema has only
# `occurred_at`, which is why which_date_field asks rather than assumes.
DATE_FIELDS = ("created", "occurred_at")


def which_date_field(pb, collection, candidates=DATE_FIELDS):
    """The field to sort and filter on, or None if there is not one.

    None is a real answer and the caller has to handle it. A collection
    with no date field cannot be pruned by age, and guessing at that point
    means either deleting everything or deleting nothing while reporting
    success.
    """
    present = pb.field_names(collection)
    if not present:
        # The schema could not be read, so nothing is known. Use the first
        # candidate: a wrong guess fails loudly at the query (HTTP 400)
        # rather than quietly at the delete.
        return candidates[0]
    for name in candidates:
        if name in present:
            return name
    return None


def old_records(pb, collection, cutoff, limit, field="created"):
    """The oldest records older than the cutoff, up to `limit`.

    Filtered and sorted on the server, 200 per page, so every page holds
    only records that should go, oldest first. Stops at the last page or
    at `limit`, whichever comes first.

    `field` is passed in because which date field the collection has is
    a question about the database, answered by which_date_field.
    """
    found = []
    page = 1
    while len(found) < limit:
        data = pb.list(
            collection,
            filter_str='%s < "%s"' % (field, cutoff),
            sort=field,
            page=page,
            per_page=200,
        )
        items = data.get("items", [])
        if not items:
            break
        found.extend(items)
        if page >= data.get("totalPages", 1):
            break
        page += 1
    return found[:limit]


def prune(pb, collection, cutoff, limit, write, log=None, field=None):
    """Find the old records, delete them when `write` is True, and report.

    Returns a dict (collection, cutoff, field, found, deleted, failed,
    failures, capped, wrote, refused) rather than printing, so the caller
    chooses the output format and tests can read the numbers.
    """
    if field is None:
        field = which_date_field(pb, collection)
    if field is None:
        # Nothing to measure age with. Reported as a refusal, because the
        # alternatives are deleting by guesswork or reporting a clean run
        # that did nothing.
        if log:
            log.error("prune: %s has no date field, tried %s",
                      collection, ", ".join(DATE_FIELDS))
        return {
            "collection": collection,
            "cutoff": cutoff,
            "field": None,
            "found": 0,
            "deleted": 0,
            "failed": 0,
            "failures": ["%s has no date field to measure age with, tried %s"
                         % (collection, ", ".join(DATE_FIELDS))],
            "capped": False,
            "wrote": False,
            "refused": True,
        }

    doomed = old_records(pb, collection, cutoff, limit, field=field)
    result = {
        "collection": collection,
        "cutoff": cutoff,
        "field": field,
        "refused": False,
        "found": len(doomed),
        "deleted": 0,
        "failed": 0,
        "failures": [],
        "capped": len(doomed) >= limit,
        "wrote": bool(write),
    }

    if not write:
        return result

    for record in doomed:
        record_id = record.get("id", "")
        try:
            pb.delete(collection, record_id)
            result["deleted"] += 1
        except Exception as exc:
            result["failed"] += 1
            # Keep only the first few messages; a server refusing every
            # delete would otherwise produce hundreds of identical lines.
            if len(result["failures"]) < 5:
                result["failures"].append("%s: %s" % (record_id, exc))
            # Twenty failures with nothing deleted points at something
            # systematic (permissions, a server gone away), so stop here
            # rather than fail thousands more times.
            if result["failed"] >= 20 and result["deleted"] == 0:
                result["failures"].append(
                    "stopped after 20 failures with nothing deleted")
                break

    if log:
        log.info("prune: %s deleted=%d failed=%d",
                 collection, result["deleted"], result["failed"])
    return result


def cmd_prune(args, conf, log):
    """The prune command. With --write, deletes go through RecordingClient
    so each one is in the ledger. Returns an exit code (see the module
    docstring)."""
    pb = PocketBaseClient(**conf.pocketbase())
    if not pb.health():
        log.error("PocketBase is not reachable")
        return EXIT_BROKEN
    pb.auth_admin()

    collection = which_collection(pb)
    if not collection:
        print("No sync log collection found. Looked for %s and %s."
              % (CANONICAL, TYPO))
        pb.close()
        return EXIT_BROKEN

    keep_days = int(conf.get("daemon.sync_log_keep_days", 30))
    limit = int(conf.get("daemon.sync_log_max_deletes", DEFAULT_MAX_DELETES))
    cutoff = cutoff_iso(keep_days)

    client = pb
    if args.write:
        ledger = WriteLedger(ledger_path(conf), run_id=RUN_ID)
        client = RecordingClient(pb, ledger, source="prune")

    result = prune(client, collection, cutoff, limit, args.write, log)
    result["keep_days"] = keep_days

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _print_human(result)

    pb.close()
    if result.get("refused"):
        return EXIT_BROKEN
    return EXIT_PARTIAL if result["failed"] else EXIT_OK


def _print_human(result):
    """The prune report for a terminal."""
    print("SYNC LOG PRUNE")
    print("==============")
    print()
    print("Collection      %s" % result["collection"])
    if result["collection"] == TYPO:
        print("                This is the misspelled collection from the")
        print("                original setup, not the one this repo declares.")
        print("                It is the live one, so it is the one pruned.")

    if result.get("refused"):
        print()
        print("This collection has no date field, so there is no way to tell")
        print("which records are old. Nothing was deleted and nothing is")
        print("claimed. Looked for: %s." % ", ".join(DATE_FIELDS))
        print()
        print("Fix the collection rather than this command. A prune that")
        print("guesses at ages is worse than one that stops.")
        return

    print("Measured by     %s" % result["field"])
    print("Keeping         %d days" % result["keep_days"])
    print("Older than      %s UTC" % result["cutoff"])
    print("Old records     %d" % result["found"])
    print()

    if not result["found"]:
        print("Nothing is old enough to delete.")
        return

    if not result["wrote"]:
        print("Nothing was deleted. Run again with --write to apply this.")
        if result["capped"]:
            print("There are at least this many. The count stops at the cap.")
        return

    print("Deleted         %d" % result["deleted"])
    if result["failed"]:
        print("Failed          %d" % result["failed"])
        for failure in result["failures"]:
            print("    %s" % failure)
    if result["capped"]:
        print()
        print("The cap was reached, so there is more to go. Run it again.")
