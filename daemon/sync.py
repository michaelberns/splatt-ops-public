"""
Forward sync: pull the Todoist board into the PocketBase `assignments`
collection.

    python -m daemon sync              work out the plan and print it
    python -m daemon sync --write      apply it
    python -m daemon sync --json       the plan as data

What it does
    Each open task in the Todoist project has one `assignments` record in
    PocketBase, matched on `todoist_id`. A pass creates records for new
    tasks, copies changed fields across, links a record to a client and to
    a job when the task text identifies one, and marks a record completed
    once its task has left Todoist.

    All the deciding is in core/sync.py (with core/matching.py and
    core/jobmatch.py for the links), and none of it touches the network.
    This file fetches, prints, and writes what the plan says. The rules
    themselves are not in here.

How a pass runs
    1. Check PocketBase is reachable and, with --write, that a backup from
       today exists (tools/backup.py). Otherwise exit 2 without writing.
    2. Read everything first: tasks, sections, assignments, clients,
       suppliers and jobs. The plan is built from one consistent picture
       rather than from a database that changes underneath it.
    3. Build the plan. core.sync.plan raises `sync.Refused` when Todoist
       returns fewer than half as many tasks as there are open records,
       so a bad read cannot mark most of the board completed.
    4. Without --write, print the plan and stop.
    5. With --write, export the before-state to backups/sync-<run id>.json,
       then apply the plan through `RecordingClient`, so every write is
       recorded in the write ledger (core/ledger.py).
    6. Re-read the records and report anything that did not land.
    7. Announce the changes on the Telegram general channel.

Order of writes
    Creates, then field updates, then client links, then job links, then
    completions. Job links come after client links because a job is only
    chosen from within one client's jobs, so a task that gained its client
    in this pass can gain its job in the same pass. Completions go last
    because a record that is about to be completed is still worth updating
    first; if a pass stops halfway, what landed is the harmless part.

Nothing here deletes a record.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from core import alerts, jobmatch, matching, sync
from core.ledger import RecordingClient, WriteLedger, ledger_path
from core.logging_setup import RUN_ID
from core.notify import from_settings as notifier_from_settings
from core.pb import PocketBaseClient
from core.todoist import TodoistClient

from tools import backup as backup_tool

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_BROKEN = 2


def internal_client_id(clients):
    """The id of the client record for Splatt's own internal work, or "".

    That record is the client named `core.sync.INTERNAL_NAME`. Supplier
    tasks that belong to no customer are linked to it. When it does not
    exist, `core.sync.link` leaves those tasks unlinked rather than
    filing them against the wrong client.
    """
    for row in clients:
        if (row.get("name") or "") == sync.INTERNAL_NAME:
            return row["id"]
    return ""


def gather(todoist, pb):
    """Every read a pass needs, taken before anything is written.

    Returns the open tasks, a section id to name map, the project name,
    and the assignments, clients, suppliers and jobs collections.
    """
    sections = {s["id"]: s.get("name", "") for s in todoist.sections_list()}
    return {
        "tasks": todoist.tasks(),
        "sections": sections,
        "project_name": todoist.project_name(),
        "records": pb.list_all("assignments"),
        "clients": pb.list_all("clients"),
        "suppliers": pb.list_all("suppliers"),
        "jobs": pb.list_all("jobs"),
    }


def build(seen):
    """Turn what was read into a plan. Returns (plan, matching index).

    Raises `sync.Refused` when the read looks wrong (see core/sync.py).
    """
    index = matching.Index(seen["clients"], seen["suppliers"])
    jobs = jobmatch.JobIndex(seen.get("jobs") or (), seen["clients"])
    return sync.plan(
        seen["tasks"],
        seen["records"],
        seen["sections"],
        seen["project_name"],
        index,
        internal_client_id(seen["clients"]),
        jobs,
    ), index


def export(item, path):
    """Write the before-state of everything the plan will change to `path`.

    Written before the first write, so a bad pass can be read back and
    undone by hand. It holds the planned creates, the before and after of
    each update, the client and job links, the completions and any
    duplicate records the plan found.
    """
    payload = {
        "run_id": RUN_ID,
        "taken": datetime.now(timezone.utc).isoformat(),
        "why": "Assignments about to be changed by daemon sync",
        "creates": [i["payload"] for i in item["creates"]],
        "updates": [{"id": i["id"], "before": i["before"], "after": i["payload"]}
                    for i in item["updates"]],
        "links": [{"id": i["id"], "after": i["payload"]} for i in item["links"]],
        "job_links": [{"id": i["id"], "after": i["payload"], "title": i.get("title", "")}
                      for i in item.get("job_links") or []],
        "completions": item["completions"],
        "duplicates": item["duplicates"],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str))
    return path


def apply(client, item, log):
    """Apply the plan through `client` and return (counts, failures).

    `client` is a RecordingClient, so each write is also recorded in the
    ledger. A failing write is collected rather than raised, so one task
    with a bad field does not stop the rest of the board syncing, and the
    counts say exactly how much of the plan happened.
    """
    done = {"created": 0, "updated": 0, "linked": 0, "filed": 0, "completed": 0}
    failures = []

    def attempt(label, fn):
        try:
            fn()
            return True
        except Exception as exc:
            failures.append("%s: %s" % (label, exc))
            log.warning("%s failed: %s", label, exc)
            return False

    for entry in item["creates"]:
        title = (entry["payload"].get("content") or "")[:60]
        if attempt("create %s" % title,
                   lambda e=entry: client.create("assignments", e["payload"])):
            done["created"] += 1

    for entry in item["updates"]:
        if attempt("update %s" % entry["id"],
                   lambda e=entry: client.update("assignments", e["id"], e["payload"])):
            done["updated"] += 1

    for entry in item["links"]:
        if attempt("link %s" % entry["id"],
                   lambda e=entry: client.update("assignments", e["id"], e["payload"])):
            done["linked"] += 1

    for entry in item.get("job_links") or []:
        if attempt("file %s under a project" % entry["id"],
                   lambda e=entry: client.update("assignments", e["id"], e["payload"])):
            done["filed"] += 1

    for entry in item["completions"]:
        if attempt("complete %s" % entry["id"],
                   lambda e=entry: client.update("assignments", e["id"],
                                                 {"status": "completed"})):
            done["completed"] += 1

    return done, failures


def verify(pb, item, done):
    """Re-read the assignments and list every planned change that is not
    in the database.

    Each updated or linked field is compared with the value that was
    sent, and every record reported completed must read back as
    completed. A successful write call only claims the change; reading
    the record back is what confirms it. Returns a list of complaints,
    empty when everything landed.
    """
    records = pb.list_all("assignments")
    best, _ = sync.canonical(records)
    complaints = []

    checked = item["updates"] + item["links"] + (item.get("job_links") or [])
    for entry in checked:
        row = next((r for r in records if r["id"] == entry["id"]), None)
        if row is None:
            complaints.append("record %s is gone after being updated" % entry["id"])
            continue
        for field, value in entry["payload"].items():
            if str(row.get(field) or "") != str(value or ""):
                complaints.append(
                    "%s.%s should be %r and reads back %r"
                    % (entry["id"], field, str(value)[:40], str(row.get(field))[:40]))

    still_open = [entry for entry in item["completions"]
                  if (best.get(str(entry["todoist_id"])) or {}).get("status") != "completed"]
    if done["completed"] and still_open:
        complaints.append("%d records were reported completed and are still open"
                          % len(still_open))

    return complaints


def announce(conf, item, seen, log, quiet=False):
    """Describe the pass on the Telegram general channel, sent silently.

    Sent after the writes, so no message describes a change that did not
    land. `core.alerts.batched` turns a large batch into one summary
    message, so a pass that touches every record does not send one
    message per task. Returns the messages sent.
    """
    if quiet:
        return []
    names = {c["id"]: c.get("name") or "" for c in seen["clients"]}
    messages = alerts.batched(alerts.for_sync(item, seen["records"], names))
    if not messages:
        return []
    notifier = notifier_from_settings(conf, channel="general")
    for message in messages:
        if not notifier.send(message, silent=True):
            log.warning("telegram would not take it: %s", notifier.last_error)
            break
    notifier.close()
    return messages


def cmd_sync(args, conf, log):
    """One forward sync pass; the steps are in the module docstring.

    Returns EXIT_OK, EXIT_PARTIAL when some writes failed or did not read
    back, or EXIT_BROKEN when it could not run.
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

    seen = gather(todoist, pb)
    try:
        item, index = build(seen)
    except sync.Refused as exc:
        # The plan refused (see build). Nothing has been written.
        log.error("%s", exc)
        print(str(exc))
        todoist.close()
        pb.close()
        return EXIT_BROKEN

    if args.json:
        print(json.dumps({
            "run_id": RUN_ID,
            "write": args.write,
            "seen": item["seen"],
            "held": item["held"],
            "creates": [i["payload"] for i in item["creates"]],
            "updates": [{"id": i["id"], "before": i["before"], "after": i["payload"]}
                        for i in item["updates"]],
            "links": [{"id": i["id"], "after": i["payload"]} for i in item["links"]],
            "job_links": [{"id": i["id"], "after": i["payload"], "title": i.get("title", "")}
                          for i in item.get("job_links") or []],
            "unplaced_jobs": item.get("unplaced_jobs") or [],
            "completions": item["completions"],
            "duplicates": item["duplicates"],
        }, indent=2, default=str))
    else:
        print(sync.describe(item))
        if index.collisions:
            print("Names two records both claim, so nothing was matched on them:")
            for key, held in sorted(index.collisions.items()):
                print("  %-24s %s" % (key, ", ".join(sorted({e[2] for e in held}))))
            print("")

    if not args.write:
        print("Nothing was written. Pass --write to apply this.")
        todoist.close()
        pb.close()
        return EXIT_OK

    out = export(item, backups / ("sync-%s.json" % RUN_ID))
    print("Exported the before state of everything that changes to %s" % out)

    ledger = WriteLedger(ledger_path(conf), run_id=RUN_ID)
    client = RecordingClient(pb, ledger, source="daemon.sync")

    done, failures = apply(client, item, log)
    print("")
    print("Created %d, updated %d, linked %d, filed under a project %d, completed %d."
          % (done["created"], done["updated"], done["linked"], done["filed"],
             done["completed"]))
    left = len(item.get("unplaced_jobs") or [])
    if left:
        print("%d task%s still without a project link. The validator lists them."
              % (left, "" if left == 1 else "s"))

    complaints = verify(pb, item, done)
    for line in failures:
        print("  failed: %s" % line)
    for line in complaints:
        print("  DID NOT LAND: %s" % line)

    said = announce(conf, item, seen, log, quiet=args.quiet)
    if said:
        print("Sent %d message%s to Telegram."
              % (len(said), "" if len(said) == 1 else "s"))

    todoist.close()
    pb.close()
    return EXIT_PARTIAL if (failures or complaints) else EXIT_OK
