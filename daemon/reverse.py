"""
Reverse sync: push what was decided on the dashboard out to Todoist.

    python -m daemon reverse            work out the plan and print it
    python -m daemon reverse --write    apply it
    python -m daemon reverse --json     the plan as data

What it does
    The dashboard never calls Todoist. It leaves a marker on the
    PocketBase `assignments` record and this pass acts on it:

      source = "dashboard"   the task was edited on the dashboard. The
                             edited fields are sent to Todoist and
                             `source` is set back to "sync".
      archived = true        archive, complete or delete was pressed
                             (the button is encoded in archived_reason).
                             A comment is added to the Todoist task, the
                             task is closed, and the record is marked
                             completed.

    A dashboard edit on a record with no Todoist task (an "orphan") is
    just settled: `source` goes back to "sync" and nothing is sent.

    All the deciding is in core/reverse.py and none of it touches the
    network. This file fetches, prints, and sends what the plan says.

How a pass runs
    1. Check PocketBase is reachable and, with --write, that a backup from
       today exists. Otherwise exit 2 without writing.
    2. Read every assignment, all pages, so the plan is built from one
       complete picture.
    3. Without --write, print the plan and stop.
    4. With --write, export the before-state to
       backups/reverse-<run id>.json, then send edits, settle orphans and
       close archives. PocketBase writes go through `RecordingClient`, so
       each one is recorded in the write ledger.
    5. Re-read the records and report any marker still set.
    6. Announce what went out on the Telegram general channel.

Rules
    A marker is cleared only after Todoist has accepted the change. If the
    send fails the marker stays, and the next pass tries again.

    On an archive the comment is posted before the task is closed, so it
    is visible in Todoist's completed view.

    The delete button does not delete anything. The Todoist task is
    closed and the PocketBase record is marked completed, the same as
    complete, so the record stays readable afterwards. The dashboard asks
    for no confirmation, so the action has to be reversible.

    The forward pass (daemon/sync.py) skips records marked "dashboard",
    and the loop runs this pass first, so an edit reaches Todoist before
    Todoist's older text can be synced back over it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from core import alerts, reverse
from core.ledger import RecordingClient, WriteLedger, ledger_path
from core.logging_setup import RUN_ID
from core.notify import from_settings as notifier_from_settings
from core.pb import PocketBaseClient
from core.todoist import TodoistClient

from tools import backup as backup_tool

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_BROKEN = 2

# Written back on the record once Todoist has accepted the change. The
# forward pass skips records marked "dashboard", so setting `source` back
# to "sync" is what lets later Todoist changes flow in again.
SETTLED = {"source": "sync"}
CLOSED = {"status": "completed"}


def gather(pb):
    """Every assignment, all pages, read before anything is sent."""
    return pb.list_all("assignments")


def export(item, path):
    """Write the before-state of everything the plan will change to `path`.

    Written before the first send, so a bad pass can be read back and
    undone by hand. It holds the planned edits, archives and orphans.
    """
    payload = {
        "run_id": RUN_ID,
        "taken": datetime.now(timezone.utc).isoformat(),
        "why": "Dashboard decisions about to be pushed to Todoist",
        "edits": item["edits"],
        "archives": item["archives"],
        "orphans": item["orphans"],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str))
    return path


def apply(todoist, client, item, log):
    """Apply the plan and return (counts, failures).

    A marker is cleared only once Todoist has accepted the change. If a
    send fails, the record stays marked and the next pass tries again.
    Failures are collected rather than raised, so one bad record does not
    stop the rest of the pass.
    """
    done = {"edited": 0, "closed": 0, "settled": 0}
    failures = []

    for entry in item["edits"]:
        label = entry["content"] or entry["id"]
        try:
            todoist.update_task(entry["todoist_id"], **entry["payload"])
        except Exception as exc:
            failures.append("edit %s: %s" % (label, exc))
            log.warning("edit %s failed, marker left alone: %s", label, exc)
            continue
        try:
            client.update("assignments", entry["id"], SETTLED)
            done["edited"] += 1
        except Exception as exc:
            # Todoist has the edit but the marker did not clear, so the
            # next pass sends the same edit again, which is harmless.
            failures.append("clearing the marker on %s: %s" % (label, exc))

    for entry in item["orphans"]:
        label = entry["content"] or entry["id"]
        try:
            client.update("assignments", entry["id"], SETTLED)
            done["settled"] += 1
        except Exception as exc:
            failures.append("settling %s: %s" % (label, exc))

    for entry in item["archives"]:
        label = entry["content"] or entry["id"]
        if entry["todoist_id"]:
            # Best effort: a comment that fails to post does not stop the
            # task being closed.
            if not todoist.add_comment(entry["todoist_id"], entry["comment"]):
                log.info("no comment posted on %s, closing anyway", label)
            try:
                todoist.complete_task(entry["todoist_id"])
            except Exception as exc:
                failures.append("closing %s: %s" % (label, exc))
                log.warning("close %s failed, record left open: %s", label, exc)
                continue
        try:
            client.update("assignments", entry["id"], CLOSED)
            done["closed"] += 1
        except Exception as exc:
            failures.append("marking %s completed: %s" % (label, exc))

    return done, failures


def verify(pb, item):
    """Re-read the assignments and list anything that did not land.

    An edited or settled record must no longer be marked "dashboard", and
    an archived record must read back as completed. Returns a list of
    complaints, empty when everything landed.
    """
    records = {r["id"]: r for r in pb.list_all("assignments")}
    complaints = []

    for entry in item["edits"] + item["orphans"]:
        row = records.get(entry["id"])
        if row is None:
            complaints.append("record %s is gone after being edited" % entry["id"])
        elif str(row.get("source") or "") == "dashboard":
            complaints.append("%s is still marked as a dashboard edit" % entry["id"])

    for entry in item["archives"]:
        row = records.get(entry["id"])
        if row is None:
            complaints.append("record %s is gone after being closed" % entry["id"])
        elif row.get("status") != "completed":
            complaints.append("%s was reported closed and is still open" % entry["id"])

    return complaints


def announce(conf, item, log, quiet=False):
    """Describe what went out on the Telegram general channel.

    These messages describe something the operator did on the dashboard a
    minute ago, so they are sent with sound on. The forward pass sends its
    messages silently. Returns the messages sent.
    """
    if quiet:
        return []
    messages = alerts.batched(alerts.for_reverse(item))
    if not messages:
        return []
    notifier = notifier_from_settings(conf, channel="general")
    for message in messages:
        if not notifier.send(message):
            log.warning("telegram would not take it: %s", notifier.last_error)
            break
    notifier.close()
    return messages


def cmd_reverse(args, conf, log):
    """One reverse sync pass; the steps are in the module docstring.

    Returns EXIT_OK, EXIT_PARTIAL when some sends or writes failed or did
    not read back, or EXIT_BROKEN when it could not run.
    """
    td = conf.todoist()
    todoist = TodoistClient(td["token"], td["project_id"], td["sections"], td["timeout"])
    pb = PocketBaseClient(**conf.pocketbase())

    if not pb.health():
        log.error("PocketBase is not reachable")
        todoist.close()
        return EXIT_BROKEN
    pb.auth_admin()

    backups = conf.repo_root / "backups"
    if args.write and not backup_tool.taken_today(backups):
        print("Refusing to write without a backup from today.")
        print(backup_tool.how_to_take_one())
        todoist.close()
        pb.close()
        return EXIT_BROKEN

    item = reverse.plan(gather(pb))

    if args.json:
        print(json.dumps({"run_id": RUN_ID, "write": args.write, **item},
                         indent=2, default=str))
    else:
        print(reverse.describe(item))

    if not args.write:
        print("Nothing was sent. Pass --write to apply this.")
        todoist.close()
        pb.close()
        return EXIT_OK

    out = export(item, backups / ("reverse-%s.json" % RUN_ID))
    print("Exported the before state of everything that changes to %s" % out)

    ledger = WriteLedger(ledger_path(conf), run_id=RUN_ID)
    client = RecordingClient(pb, ledger, source="daemon.reverse")

    done, failures = apply(todoist, client, item, log)
    print("")
    print("Pushed %d edits, closed %d tasks, settled %d records with no Todoist task."
          % (done["edited"], done["closed"], done["settled"]))

    complaints = verify(pb, item)
    for line in failures:
        print("  failed: %s" % line)
    for line in complaints:
        print("  DID NOT LAND: %s" % line)

    said = announce(conf, item, log, quiet=args.quiet)
    if said:
        print("Sent %d message%s to Telegram."
              % (len(said), "" if len(said) == 1 else "s"))

    todoist.close()
    pb.close()
    return EXIT_PARTIAL if (failures or complaints) else EXIT_OK
