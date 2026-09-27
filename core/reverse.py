"""
Reverse sync planning: work out what the dashboard's changes mean for Todoist.

Inputs
    PocketBase `assignments` records (the mirror of the Todoist board).

Output
    `plan()` returns {"edits", "archives", "orphans"}: the Todoist updates
    to send for dashboard edits, the tasks to close, and edited records
    that have no Todoist task. daemon/reverse.py sends them. Nothing here
    touches the network, the same split as core/sync.py.

How a decision gets from the dashboard to Todoist
    The dashboard never calls Todoist. It writes a marker onto the
    assignment record, and the daemon picks it up on its next pass.

      `source` = "dashboard"
          The operator edited the task in the dashboard (title,
          description, due date, priority or labels). The daemon pushes
          the edit to Todoist and sets `source` back to "sync". The
          forward sync (core/sync.py) skips such records, so Todoist's
          older wording cannot overwrite the edit first.

      `archived` = true
          The operator pressed archive, complete or delete. There is one
          flag for three buttons, so the button is encoded as a prefix on
          `archived_reason` ("DELETE:", "COMPLETE:", or none for archive).

Rules
    1. A field the dashboard holds is pushed, including an empty one:
       clearing a description, a due date or the labels clears them in
       Todoist. The title is the exception. Todoist rejects an empty
       title, and a task with no title cannot be found again.
    2. A priority outside 1 to 4 is left out rather than sent, because
       Todoist would reject the whole update.
    3. A record that is both edited and archived is only archived.
    4. A record already at status "completed" is not closed again, so
       one close is not repeated on every pass.
"""

from __future__ import annotations

# The prefixes the dashboard puts on archived_reason to say which button
# was pressed. No prefix means archive.
INTENTS = {"DELETE:": "delete", "COMPLETE:": "complete"}

# The fields pushed to Todoist on an edit, in the order they appear on screen.
PUSHED = ("content", "description", "due_date", "priority", "labels")


def intent(archived_reason):
    """Which button was pressed, and the reason with the prefix removed.

    >>> intent("COMPLETE: parts arrived")
    ('complete', 'parts arrived')
    """
    raw = str(archived_reason or "").strip()
    for prefix, name in INTENTS.items():
        if raw.startswith(prefix):
            return name, raw[len(prefix):].strip()
    return "archive", raw


def edit_payload(record):
    """The Todoist update for a dashboard edit, in Todoist's own shape.

    Empty means empty: a cleared description, due date or label list is
    pushed as cleared. The title is never cleared, because Todoist
    refuses an empty one and a task with no title cannot be found again.
    A priority outside 1 to 4 is left out, because Todoist would reject
    the whole update.
    """
    payload = {}

    content = str(record.get("content") or "").strip()
    if content:
        payload["content"] = content

    payload["description"] = str(record.get("description") or "")

    try:
        priority = int(record.get("priority") or 0)
    except (TypeError, ValueError):
        priority = 0
    if priority in (1, 2, 3, 4):
        payload["priority"] = priority

    labels = str(record.get("labels") or "").strip()
    payload["labels"] = [l.strip() for l in labels.split(",") if l.strip()]

    due = str(record.get("due_date") or "").strip()
    if due:
        # PocketBase holds "2026-08-14 09:30:00.000Z". Todoist wants a
        # plain date on the due_date key.
        payload["due_date"] = due.split(" ")[0].split("T")[0]
    else:
        # Todoist rejects due_date="". Its documented way to clear a due
        # date is due_string="no date".
        payload["due_string"] = "no date"

    return payload


def comment(intent_name, reason, archived_at=""):
    """What gets written on the Todoist task before it is closed.

    Without a comment, Todoist's completed view would show the task as
    done with nothing saying why it was closed. The comment is posted
    before the close so it stays attached to the closed task.
    """
    intro = {
        "archive": "Archived from the dashboard",
        "complete": "Completed from the dashboard",
        "delete": "Deleted from the dashboard",
    }[intent_name]
    lines = [intro]
    if reason:
        lines.append("Reason: %s" % reason)
    if archived_at:
        lines.append("At: %s" % str(archived_at)[:16])
    return "\n".join(lines)


def pending_edits(records):
    """Records the dashboard has staged an edit on.

    An archived record is left out even if it is also marked edited:
    the task is about to be closed, and the archive path posts its
    wording in the closing comment anyway.
    """
    return [r for r in records
            if str(r.get("source") or "") == "dashboard" and not r.get("archived")]


def pending_archives(records):
    """Records archived in the dashboard that Todoist has not been told about.

    A record already at status "completed" has been closed before.
    Leaving it out stops the pass repeating the same close on every run.
    """
    return [r for r in records
            if r.get("archived") and r.get("status") != "completed"]


def plan(records):
    """Everything a reverse pass would do, before any of it is sent.

    An edited record with no `todoist_id` goes to "orphans": there is no
    task to update, so the daemon clears its marker and sends nothing.
    An archived record with no `todoist_id` stays in "archives", because
    it still has to be settled locally.
    """
    edits, archives, orphans = [], [], []

    for record in pending_edits(records):
        item = {"id": record["id"], "todoist_id": str(record.get("todoist_id") or ""),
                "content": str(record.get("content") or "")[:80],
                "payload": edit_payload(record)}
        (edits if item["todoist_id"] else orphans).append(item)

    for record in pending_archives(records):
        name, reason = intent(record.get("archived_reason"))
        archives.append({
            "id": record["id"],
            "todoist_id": str(record.get("todoist_id") or ""),
            "content": str(record.get("content") or "")[:80],
            "intent": name,
            "reason": reason,
            "comment": comment(name, reason, record.get("archived_at")),
        })

    return {"edits": edits, "archives": archives, "orphans": orphans}


def describe(item):
    """The plan in words, for a dry run."""
    lines = [""]

    lines.append("Dashboard edits to push to Todoist: %d" % len(item["edits"]))
    for row in item["edits"]:
        lines.append("  %s" % row["content"])
        lines.append("      %s" % row["payload"])
    lines.append("")

    counts = {}
    for row in item["archives"]:
        counts[row["intent"]] = counts.get(row["intent"], 0) + 1
    lines.append("Tasks to close in Todoist: %d  %s"
                 % (len(item["archives"]),
                    ", ".join("%s %d" % (k, v) for k, v in sorted(counts.items()))))
    for row in item["archives"]:
        lines.append("  %-8s %s" % (row["intent"], row["content"]))
        if row["reason"]:
            lines.append("      %s" % row["reason"][:80])
    lines.append("")

    if item["orphans"]:
        lines.append("Edited records with no Todoist task, marker cleared and "
                     "nothing sent: %d" % len(item["orphans"]))
        for row in item["orphans"]:
            lines.append("  %s" % row["content"])
        lines.append("")

    return "\n".join(lines)
