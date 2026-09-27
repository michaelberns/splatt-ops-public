"""
Triggers pass: let finishing a task move a job, and manage quote chases.

    python -m daemon triggers              work out the plan and print it
    python -m daemon triggers --write      apply it
    python -m daemon triggers --json       the plan as data

What it does
    A task can carry a trigger: a line in its Todoist description, mirrored
    into the `on_complete_job_status` field of its assignment, that names
    the status its job moves to when the task is completed (for example
    "Send the quote" moves the job to quoted). This pass applies four
    rules, defined in core/triggers.py:

      R0  add a missing trigger, either by mirroring a line already in the
          description or from a strict title pattern for the job's status
      R1  move a job forward when a task with a trigger is completed
      R2  create one chase task for a job that has sat in quoted for
          triggers.quoted_chase_after_days (7) with no reply
      R3  close that chase task once its job has left quoted

    All the deciding is in core/triggers.py and none of it touches the
    network. This file fetches, prints, and writes what the plan says. The
    rules themselves are not in here.

    Like sync and reverse, it writes nothing without --write, and with
    --write it refuses to run without a backup from today. Moving job
    statuses and creating tasks should be read as a plan before they are
    allowed to happen unattended.

Order within a pass
    Reads first, all of them, before any write, so the plan is built from
    one consistent picture.

    Then R0, R1, R2, R3, in that order:

      R0 only adds a trigger. R1 works from the snapshot read before R0
      wrote anything, so a trigger added this pass cannot fire until a
      later pass. That gives the operator a chance to see (in Telegram
      and the client history) a trigger added by a title pattern before
      it moves a real job.

      R1 moves job statuses. R3 then sees the moved status and closes any
      chase for that job in the same pass.

      R2 creates chases after R1, so it never creates one for a job that
      R1 has just moved out of quoted.

The one exception to "never complete a task"
    The overdue engine never completes or deletes a task. R3 is the single
    exception in the daemon: it completes a Todoist task, and only a task
    that R2 created and marked with `auto_kind`. A task the operator wrote
    with the same title is never touched. core/triggers.py documents the
    rule where it is implemented.

Ledger
    PocketBase writes go through `RecordingClient`. Todoist writes are
    recorded against the ledger's TODOIST_COLLECTION by hand. Either way
    the validator's writes_landed rule can re-read all of it. Every action
    is also written to the client's history as an interaction.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from core import interactions as interactions_mod
from core import triggers
from core.ledger import TODOIST_COLLECTION, RecordingClient, WriteLedger, ledger_path
from core.logging_setup import RUN_ID
from core.notify import from_settings as notifier_from_settings
from core.pb import PocketBaseClient
from core.todoist import TodoistClient

from tools import backup as backup_tool

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_BROKEN = 2


def gather(todoist, pb):
    """Every read a pass needs, taken before anything is written.

    `interaction_keys` holds the source_key of every existing interaction.
    Each action writes an interaction with its own key, so the plan uses
    this set to skip an action already taken on an earlier pass.
    """
    return {
        "tasks": todoist.tasks(),
        "assignments": pb.list_all("assignments"),
        "jobs": pb.list_all("jobs"),
        "clients": pb.list_all("clients"),
        "contacts": pb.list_all("contacts"),
        "quotes": pb.list_all("quotes"),
        "invoices": pb.list_all("invoices"),
        "task_groups": pb.list_all("task_groups"),
        "interaction_keys": {
            str(row.get("source_key") or "")
            for row in pb.list_all("interactions")
            if row.get("source_key")
        },
    }


def build(seen, conf=None, today=None):
    """Turn what was read into a plan. Pure (no I/O), so it can be printed
    or tested before anything runs."""
    return triggers.plan(seen, triggers.Rules(conf), today=today)


def _now():
    return datetime.now(timezone.utc)


def _stamp():
    return _now().strftime("%Y-%m-%d %H:%M:%S.000Z")


def _log_interaction(client, action, log, extra_summary=""):
    """Record one action in the client's history as an interaction.

    Every automatic change appears in the client's history as a plain
    sentence. The record is keyed on (triggers.SOURCE, source_id), so a
    re-run finds the one it wrote before instead of writing a second.

    A job with no client still gets an interaction, linked to the job
    alone. The validator's no_orphan_interactions rule then reports it,
    which is correct: the missing client is the real problem.

    Returns None on success, or a failure line. A failure here never
    undoes the change that was already made, because that would leave a
    job half moved; it is reported instead.
    """
    summary = action["summary"]
    if extra_summary:
        summary = "%s %s" % (summary, extra_summary)
    try:
        interactions_mod.save(
            client,
            triggers.SOURCE,
            action["source_id"],
            type="note",
            client=action.get("client") or None,
            job=action.get("job") or None,
            date=_stamp(),
            subject=action["subject"][:250],
            summary=summary,
        )
        return None
    except Exception as exc:
        if log:
            log.warning("could not log the interaction for %s: %s",
                        action["source_id"], exc)
        return "interaction for %s: %s" % (action["source_id"], exc)


def apply(client, todoist, item, log=None, ledger=None):
    """Apply the plan and return (counts, failures).

    `client` is a RecordingClient. Todoist writes are recorded in `ledger`
    by note_todoist. Every failure is collected rather than raised, so one
    bad task does not stop the rest of the pass and the report always
    covers the whole plan. When a Todoist step fails, the PocketBase step
    for the same action is skipped, so the two never disagree.
    """
    done = {"triggers_added": 0, "jobs_moved": 0, "chases_created": 0,
            "chases_closed": 0}
    failures = []

    def attempt(label, fn):
        try:
            return fn(), True
        except Exception as exc:
            failures.append("%s: %s" % (label, exc))
            if log:
                log.warning("%s failed: %s", label, exc)
            return None, False

    def note_todoist(task_id, kind, detail):
        if ledger and task_id:
            ledger.record("update", TODOIST_COLLECTION, task_id, [kind],
                          "daemon.triggers", detail)

    # R0: add the trigger line in Todoist (if missing), then the field.
    for action in item["r0"]:
        label = "add trigger to %s" % action["content"][:50]
        if action["write_line"]:
            _, ok = attempt(label + " in Todoist", lambda a=action: todoist.update_task(
                a["todoist_id"], description=a["description_after"]))
            if not ok:
                continue
            note_todoist(action["todoist_id"], "trigger line",
                         "on complete: job to %s" % action["status"])
        _, ok = attempt(label, lambda a=action: client.update(
            "assignments", a["assignment"],
            {triggers.PB_STATUS_FIELD: a["status"]}))
        if not ok:
            continue
        done["triggers_added"] += 1
        problem = _log_interaction(client, action, log)
        if problem:
            failures.append(problem)

    # R1: move the job forward and stamp who and when.
    for action in item["r1"]:
        label = "move %s to %s" % (action["job_title"][:40], action["to"])
        _, ok = attempt(label, lambda a=action: client.update("jobs", a["job"], {
            "status": a["to"],
            triggers.JOB_CHANGED_AT: _stamp(),
            triggers.JOB_CHANGED_BY: a["changed_by"],
        }))
        if not ok:
            continue
        done["jobs_moved"] += 1
        problem = _log_interaction(client, action, log)
        if problem:
            failures.append(problem)

    # R2: create the chase task in Todoist, then its assignment record.
    for action in item["r2"]:
        label = "create %s" % action["title"][:50]
        created, ok = attempt(label, lambda a=action: todoist.add_task(
            a["title"], description=a["description"], due_string=a["due"],
            section=a["section"]))
        if not ok:
            continue
        todoist_id = str((created or {}).get("id") or "")
        note_todoist(todoist_id, "chase task", action["title"][:80])
        _, ok = attempt(label + " in PocketBase", lambda a=action: client.create(
            "assignments", {
                "todoist_id": todoist_id,
                "content": a["title"],
                "description": a["description"],
                "status": "open",
                "source": "ai_suggested",
                triggers.PB_AUTO_FIELD: triggers.QUOTED_CHASE,
                "job": a["job"],
                "client": a["client"],
            }))
        if not ok:
            continue
        done["chases_created"] += 1
        problem = _log_interaction(
            client, action, log,
            extra_summary="Todoist task %s." % (todoist_id or "unknown"))
        if problem:
            failures.append(problem)

    # R3: close the chase. The comment goes on first, so the reason is on
    # the task before it closes.
    for action in item["r3"]:
        label = "close %s" % action["content"][:50]
        if action["todoist_id"]:
            todoist.add_comment(action["todoist_id"], action["comment"])
            _, ok = attempt(label + " in Todoist",
                            lambda a=action: todoist.complete_task(a["todoist_id"]))
            if not ok:
                continue
            note_todoist(action["todoist_id"], "completed", action["archived_reason"])
        _, ok = attempt(label, lambda a=action: client.update(
            "assignments", a["assignment"], {
                "status": "completed",
                "archived": True,
                "archived_reason": a["archived_reason"],
                "archived_at": _stamp(),
            }))
        if not ok:
            continue
        done["chases_closed"] += 1
        problem = _log_interaction(client, action, log)
        if problem:
            failures.append(problem)

    return done, failures


def message(item, done):
    """The Telegram message for a pass that did something, or "".

    Empty when nothing happened. The pass runs every minute inside the
    loop, so a "nothing to do" message would be sent 1440 times a day.
    """
    parts = []
    if done["jobs_moved"]:
        parts.append("%d project%s moved on"
                     % (done["jobs_moved"], "" if done["jobs_moved"] == 1 else "s"))
    if done["triggers_added"]:
        parts.append("%d trigger%s added"
                     % (done["triggers_added"],
                        "" if done["triggers_added"] == 1 else "s"))
    if done["chases_created"]:
        parts.append("%d chase task%s created"
                     % (done["chases_created"],
                        "" if done["chases_created"] == 1 else "s"))
    if done["chases_closed"]:
        parts.append("%d chase task%s closed"
                     % (done["chases_closed"],
                        "" if done["chases_closed"] == 1 else "s"))
    if not parts:
        return ""

    lines = ["✅ <b>Task triggers</b>", ", ".join(parts) + "."]
    for action in item["r1"][:8]:
        lines.append("- %s: %s to %s"
                     % (action["job_title"][:60], action["from"], action["to"]))
    for action in item["r0"][:8]:
        lines.append("- trigger added: %s (to %s)"
                     % (action["content"][:60], action["status"]))
    for action in item["r2"][:8]:
        lines.append("- chase created: %s" % action["title"][:60])
    for action in item["r3"][:8]:
        lines.append("- chase closed: %s" % action["content"][:60])
    if item["needs_decision"]:
        lines.append("")
        lines.append("%d need a decision, they are in the run output."
                     % len(item["needs_decision"]))
    return "\n".join(lines)


def announce(conf, item, done, log, quiet=False):
    """Send the pass's message to the Telegram general channel, silently.
    Returns the text sent, or "" when nothing was sent."""
    text = message(item, done)
    if quiet or not text:
        return ""
    notifier = notifier_from_settings(conf, channel="general")
    if not notifier.send(text, silent=True):
        if log:
            log.warning("telegram would not take it: %s", notifier.last_error)
    notifier.close()
    return text


def cmd_triggers(args, conf, log):
    """One triggers pass; the steps are in the module docstring.

    Returns EXIT_OK, EXIT_PARTIAL when some actions failed, or
    EXIT_BROKEN when it could not run.
    """
    td = conf.todoist()
    todoist = TodoistClient(td["token"], td["project_id"], td["sections"],
                            td["timeout"])
    pb = PocketBaseClient(**conf.pocketbase())

    if not pb.health():
        log.error("PocketBase is not reachable")
        todoist.close()
        return EXIT_BROKEN
    pb.auth_admin()

    write = bool(getattr(args, "write", False))
    backups = conf.repo_root / "backups"
    if write and not backup_tool.taken_today(backups):
        print("Refusing to write without a backup from today.")
        print(backup_tool.how_to_take_one())
        todoist.close()
        pb.close()
        return EXIT_BROKEN

    seen = gather(todoist, pb)
    item = build(seen, conf)

    if getattr(args, "json", False):
        print(json.dumps({
            "run_id": RUN_ID,
            "write": write,
            "today": item["today"],
            "r0": item["r0"],
            "r1": item["r1"],
            "r2": item["r2"],
            "r3": item["r3"],
            "needs_decision": item["needs_decision"],
            "skipped": item["skipped"],
        }, indent=2, default=str))
    else:
        print(triggers.describe(item))

    if not write:
        if triggers.actions(item):
            print("Nothing was written. Pass --write to apply this.")
        todoist.close()
        pb.close()
        return EXIT_OK

    ledger = WriteLedger(ledger_path(conf), run_id=RUN_ID)
    client = RecordingClient(pb, ledger, source="daemon.triggers")
    done, failures = apply(client, todoist, item, log=log, ledger=ledger)

    print("")
    print("Triggers added %d, projects moved %d, chases created %d, "
          "chases closed %d."
          % (done["triggers_added"], done["jobs_moved"],
             done["chases_created"], done["chases_closed"]))
    for line in failures:
        print("  failed: %s" % line)

    said = announce(conf, item, done, log, quiet=getattr(args, "quiet", False))
    if said:
        print("Sent one message to Telegram.")

    todoist.close()
    pb.close()
    return EXIT_PARTIAL if failures else EXIT_OK
