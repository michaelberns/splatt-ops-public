"""
Alert wording: the Telegram messages the system sends, built as plain strings.

What it does
    Turns a sync plan, a reverse (dashboard) plan or a set of daily counts
    into the text of Telegram messages. It does no network I/O, following
    the same split as core/sync.py: planning and wording live in core/,
    sending lives in daemon/ and core/notify.py. Because every message is
    just a returned string, tests can read the exact wording.

Rules built into the wording
    - Only changes a person would care about produce a message. Fields in
      WORTH_SAYING (wording, notes, due date, priority, labels, section)
      do; bookkeeping fields such as `project_name` do not.
    - A pass may send at most FLOOD single-task messages. Beyond that,
      `batched()` sends one roll-up message with a count per message type,
      so a pass that touches every task (for example the first run of a
      new sync) does not send dozens of notifications in a row.
    - The daily summary (`daily()`) puts the numbers that matter for the
      day on one screen: open, finished today, overdue, waiting on a
      decision, and tasks with no client.
    - Activity on a client marked sensitive (the `critical` field on the
      client record) is reported with a distinct, prominent heading.
"""

from __future__ import annotations

# How many single-task messages a pass may send before switching to one
# roll-up message. Twelve is roughly one phone screen.
FLOOD = 12

# Task fields whose changes are worth a message. `project_name` is left
# out on purpose: it is bookkeeping, not news.
WORTH_SAYING = ("content", "description", "due_date", "priority",
                "labels", "section_name")

READABLE = {
    "content": "wording",
    "description": "notes",
    "due_date": "due date",
    "priority": "priority",
    "labels": "labels",
    "section_name": "section",
}

KINDS = {
    "email_sent": "email sent",
    "quote_issued": "quote issued",
    "follow_up": "follow up",
    "site_visit": "site visit",
    "phone_call": "phone call",
    "task_completed": "done",
}


def _clip(text, limit=200):
    return str(text or "").strip()[:limit]


def kind(task, critical=False):
    """Classify a finished task as a kind of work.

    Order of precedence:
      1. `critical=True` (the client record's `critical` flag) gives
         "dispute_action", the kind used for sensitive accounts.
      2. A label: "email", "quote" or "follow-up".
      3. Words in the content or description: visit/site, then call/phone/ring.
      4. Otherwise "task_completed".

    >>> kind({"labels": ["quote"], "content": "call the client"})   # 'quote_issued'
    >>> kind({"content": "Site visit at the bottling plant"})       # 'site_visit'
    """
    if critical:
        return "dispute_action"
    labels = [str(l).lower() for l in (task.get("labels") or [])]
    if "email" in labels:
        return "email_sent"
    if "quote" in labels:
        return "quote_issued"
    if "follow-up" in labels:
        return "follow_up"
    text = "%s %s" % (task.get("content") or "", task.get("description") or "")
    text = text.lower()
    if any(word in text for word in ("visit", "site", "on-site")):
        return "site_visit"
    if any(word in text for word in ("call", "phone", "ring")):
        return "phone_call"
    return "task_completed"


# single-task messages

def new_task(client, task):
    """A new task, with its client ("General" if none), due date and labels."""
    lines = ["\U0001f4cb <b>New task</b>",
             "<b>%s</b>: %s" % (client or "General", _clip(task.get("content")))]
    if _clip(task.get("description")):
        lines.append(_clip(task.get("description")))
    due = task.get("due")
    if isinstance(due, dict) and due.get("date"):
        lines.append("Due %s" % due["date"])
    if task.get("labels"):
        lines.append(", ".join(str(l) for l in task["labels"]))
    return "\n".join(lines)


def changed(client, content, fields):
    """A task update, naming only the changed fields worth hearing about.

    Returns "" when none of `fields` is in WORTH_SAYING, and the caller
    then sends nothing. Messages with no real news train people to ignore
    the channel.
    """
    worth = [f for f in fields if f in WORTH_SAYING]
    if not worth:
        return ""
    named = ", ".join(READABLE.get(f, f) for f in worth)
    return "✏️ <b>Task updated</b> (%s)\n<b>%s</b>: %s" % (
        named, client or "General", _clip(content))


def completed(client, content, kind_name="task_completed", at=""):
    """A finished task, labelled with its kind. Sensitive-account work is
    handed to `dispute()` instead."""
    if kind_name == "dispute_action":
        return dispute(client, content)
    lines = ["✅ <b>Completed</b> (%s)" % KINDS.get(kind_name, kind_name),
             "<b>%s</b>: %s" % (client or "General", _clip(content))]
    if at:
        lines.append(str(at)[:16])
    return "\n".join(lines)


def dispute(client, content):
    """A finished task on a client flagged sensitive (`clients.critical`).

    Uses a distinct heading so it stands out from routine completions. On
    such accounts the agent only reports and never drafts, so the operator
    needs to see every action taken.
    """
    return "\U0001f6a8 <b>Activity on a sensitive account</b>\n<b>%s</b>: %s" % (
        client or "General", _clip(content))


def dashboard_edit(content, pushed=True):
    """An edit made in the dashboard, and whether it reached Todoist."""
    return "✏️ <b>Edit pushed to Todoist</b>\n%s\n%s" % (
        _clip(content),
        "Todoist updated" if pushed else "Held in the dashboard, no Todoist task")


def dashboard_event(intent_name, content, reason="", at="", closed=True):
    """An archive, complete or delete made in the dashboard.

    Says which action it was, the reason given, and whether a Todoist task
    was closed or the change was made in the database only (for records
    with no Todoist task).
    """
    intro = {
        "archive": "\U0001f4e5 <b>Archived from the dashboard</b>",
        "complete": "✅ <b>Completed from the dashboard</b>",
        "delete": "\U0001f5d1️ <b>Deleted from the dashboard</b>",
    }.get(intent_name, "\U0001f4e5 <b>Archived from the dashboard</b>")
    lines = [intro, _clip(content)]
    if reason:
        lines.append(_clip(reason))
    if at:
        lines.append(str(at)[:16])
    lines.append("Todoist task closed" if closed
                 else "No Todoist task, settled in the database only")
    return "\n".join(lines)


# the whole board, once a day

def daily(active, completed_today, overdue, needing_decision=0, unlinked=0):
    """The once-a-day summary of the whole board.

    The last two lines ask the operator to do something, so they appear
    only when their count is non-zero. When nothing is overdue or waiting
    on a decision, the message says "Nothing is late." instead.

    >>> daily(active=40, completed_today=3, overdue=0).splitlines()[1:]
    ['Open tasks: 40', 'Finished today: 3', 'Overdue: 0', 'Nothing is late.']
    """
    lines = ["\U0001f4ca <b>Daily summary</b>",
             "Open tasks: %d" % active,
             "Finished today: %d" % completed_today,
             "Overdue: %d" % overdue]
    if needing_decision:
        lines.append("Waiting on a decision from you: %d" % needing_decision)
    if unlinked:
        lines.append("With no client on them: %d" % unlinked)
    if not overdue and not needing_decision:
        lines.append("Nothing is late.")
    return "\n".join(lines)


# everything one pass would say

def for_sync(item, records=None, names=None):
    """Every message a forward (Todoist to PocketBase) pass would send, in order.

    `item` is the sync plan with `creates`, `updates` and `completions`.
    `records` are the existing assignment records, and `names` maps a
    client record id to a client name. A task with no client is shown as
    "General".
    """
    names = names or {}
    by_id = {r["id"]: r for r in (records or [])}
    out = []

    for entry in item.get("creates", []):
        client = names.get(str(entry["payload"].get("client") or ""), "")
        out.append(new_task(client, entry["task"]))

    for entry in item.get("updates", []):
        record = by_id.get(entry["id"], {})
        client = names.get(str(record.get("client") or ""), "")
        text = changed(client, record.get("content") or
                       (entry["task"].get("content") or ""),
                       list(entry["payload"].keys()))
        if text:
            out.append(text)

    for entry in item.get("completions", []):
        record = by_id.get(entry["id"], {})
        client_id = str(record.get("client") or "")
        client = names.get(client_id, "")
        out.append(completed(client, entry.get("content") or "",
                             kind(record, critical=False)))

    return out


def for_reverse(item):
    """Every message a reverse (dashboard to Todoist) pass would send, in order."""
    out = [dashboard_edit(entry["content"]) for entry in item.get("edits", [])]
    out += [dashboard_edit(entry["content"], pushed=False)
            for entry in item.get("orphans", [])]
    for entry in item.get("archives", []):
        out.append(dashboard_event(entry["intent"], entry["content"],
                                   entry.get("reason", ""),
                                   closed=bool(entry.get("todoist_id"))))
    return out


def batched(messages, limit=FLOOD):
    """Return the messages unchanged, or one roll-up if there are too many.

    Empty messages are dropped first. If more than `limit` remain, the
    result is a single message giving the total and a count per first
    line (message type), most common first, such as a line reading
    "Task updated (due date)  x86". A burst of notifications tends to get
    the channel muted, which would hide the alerts that matter.
    """
    messages = [m for m in messages if m]
    if len(messages) <= limit:
        return messages
    counts = {}
    for message in messages:
        head = message.split("\n")[0]
        counts[head] = counts.get(head, 0) + 1
    lines = ["\U0001f4e6 <b>%d changes in one pass</b>" % len(messages),
             "Too many to send one by one, so here is the shape of it.", ""]
    for head, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        lines.append("%s  x%d" % (head, count))
    return ["\n".join(lines)]
