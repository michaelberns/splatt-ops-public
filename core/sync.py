"""
Sync planning: work out what one pass over Todoist should write to PocketBase.

Terms
    The board is the operator's Todoist project. Its tasks are mirrored
    into the PocketBase `assignments` collection, one record per task,
    keyed by `todoist_id`. A board section is one of the project's seven
    columns (Today, Overdue, Upcoming, Waiting on client, Waiting on
    supplier, Stalled, Backlog), stored on the record as `section_name`.

Inputs
    The tasks Todoist returned, the existing `assignments` records, a map
    of section id to section name, the project name, and optionally a
    client `Index` (core/matching.py) and a project `JobIndex`
    (core/jobmatch.py).

Output
    `plan()` returns every write the pass would make, before any is sent:

      creates        full records for tasks PocketBase has never seen
      updates        Todoist-owned fields that changed on existing records
      links          blank client or supplier links that can now be filled
      job_links      blank project links that can now be filled
      unplaced_jobs  open tasks the project matcher would not place, with
                     the reason and the candidates (the validator reads it)
      completions    open records whose task is no longer in Todoist
      duplicates     extra records sharing one Todoist id (reported only)

    daemon/sync.py applies the plan, and `describe()` prints it for a dry
    run. Nothing in this module touches the network.

Rules
    1. Todoist owns these fields and only these: TODOIST_OWNS. An update
       carries only the owned fields whose value differs. `client`,
       `supplier` and `job` are not owned, so they are never in an update
       and PocketBase leaves them as they are.
    2. Links are fill-only. `link` and `link_job` write a client, supplier
       or project only where the record has none, and return nothing when
       the matcher is not sure. A link set by hand is never replaced by a
       guess from a title. Linking happens on every pass, so new tasks are
       filed as they arrive.
    3. The 50% floor. If Todoist returns fewer tasks than FLOOR (half) of
       the open records, the pass raises `Refused` and marks nothing
       completed. A bad or partial read is far more likely than half the
       board being finished at once, and completing everything missing
       would empty the board.
    4. Nothing is deleted. Duplicate records are reported and left alone;
       deleting is a separate, deliberate job.
    5. A record whose `source` is "dashboard" holds an edit waiting to go
       out to Todoist (see core/reverse.py). The pass skips it, so
       Todoist's older wording does not overwrite the edit.

Why diff the fields instead of sending a whole record
    A whole record would include `client` and `job`. Todoist has no such
    fields, so the value sent would be a blank or a guess, and it would
    overwrite any link set by hand. Sending only owned fields that
    changed means a field Todoist knows nothing about is never sent.

What a pass does not write
    It does not create `interactions` records. An interaction is contact
    with a person, and a task being created or edited is not that.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from core import jobmatch, matching

# The only fields a pass over Todoist may write. `client`, `supplier` and
# `job` must never be added: Todoist does not hold them, so a pass that
# wrote them would be writing a guess over a real value.
TODOIST_OWNS = (
    "content",
    "description",
    "priority",
    "labels",
    "due_date",
    "deadline",
    "section_name",
    "project_name",
)

# The 50% floor. If Todoist returns fewer tasks than this share of the
# open records, the pass refuses to mark the missing ones completed. A
# sudden drop is more likely a bad or partial read than real work, and
# acting on it would complete most of the board.
FLOOR = 0.5

# The internal client: Splatt's own work, such as admin, the website or
# buying from a supplier, where there is no customer on the other side.
# Every task carries a client so that none drops out of client views, and
# for these tasks the client is this record. daemon/sync.py finds it by
# INTERNAL_NAME.
#
# It is not "Splatt Engineering Group PTY", which is a separate
# Australian company with its own invoices. Keeping the two apart is why
# core/matching.py does not treat "group" as a legal ending.
INTERNAL_NAME = "Splatt Engineering (internal)"
INTERNAL_ALIASES = "Splatt Engineering, Splatt, Splatt Internal"
INTERNAL_NOTES = (
    "Splatt Engineering's own work. Admin, the website, Xero tidy ups, "
    "buying from suppliers, anything where there is no customer on the "
    "other side. Every task has to carry a client so that nothing falls "
    "off the board, and for these the client is Splatt itself.\n\n"
    "This is not Splatt Engineering Group PTY. That is the Australian "
    "company and a real counterparty with its own invoices."
)


class Refused(Exception):
    """Raised when a pass will not write because its input looks wrong (see FLOOR)."""


def due_date(task):
    """Todoist's due shape turned into what the PocketBase date field takes.

    Todoist gives a bare `2026-08-14` for an all-day task and a timestamp
    for a timed one. PocketBase wants a UTC datetime ending in Z either
    way. Returns "" for no date or a date that does not parse.

    A timestamp with a zone offset is converted to UTC, not truncated.
    Cutting the offset off and appending Z would file 09:30 in Auckland
    as 09:30 UTC, twelve hours out.

    >>> due_date({"due": {"date": "2026-08-14"}})
    '2026-08-14 00:00:00.000Z'
    >>> due_date({"due": {"date": "2026-08-14T09:30:00+12:00"}})
    '2026-08-13 21:30:00.000Z'
    """
    due = (task or {}).get("due") or {}
    raw = (due.get("date") or "").strip()
    if not raw:
        return ""
    if "T" not in raw and " " not in raw:
        try:
            date.fromisoformat(raw)
        except ValueError:
            return ""
        return "%s 00:00:00.000Z" % raw

    text = raw.replace(" ", "T")
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%d %H:%M:%S.") + "%03dZ" % (moment.microsecond // 1000)


def deadline(task):
    """Todoist's deadline, which is not the same thing as its due date.

    A due date is when the operator plans to work on a task, and the
    overdue engine moves it when that does not happen. A deadline is the
    date the work stops being worth doing, and nothing in this system
    moves it. Todoist keeps them as two fields, and so does PocketBase.

    Always midnight on a plain day. Todoist's deadline has no time of
    day, and inventing one would invite the timezone error `due_date`
    guards against. Returns "" when there is no deadline or it does not
    parse.
    """
    value = (task or {}).get("deadline") or {}
    raw = (value.get("date") or "").strip() if isinstance(value, dict) else ""
    if not raw:
        return ""
    try:
        date.fromisoformat(raw[:10])
    except ValueError:
        return ""
    return "%s 00:00:00.000Z" % raw[:10]


def priority(task):
    """The task's priority exactly as the Todoist API sends it (1 to 4).

    The API numbers priority the other way round from the app: 4 is the
    top priority, which the app shows as P1. The value is stored
    unconverted so the database agrees with the API, and converting it is
    left to whatever displays it. Anything outside 1 to 4 falls back to 1.
    """
    try:
        value = int((task or {}).get("priority") or 1)
    except (TypeError, ValueError):
        return 1
    return value if value in (1, 2, 3, 4) else 1


def labels(task):
    """Todoist's label list as the comma-separated string PocketBase stores."""
    return ",".join((task or {}).get("labels") or [])


def derive(task, section_names=None, project_name=""):
    """The fields Todoist knows about, and only those.

    `section_names` maps a Todoist section id to its name. A section id
    that is not in the map gives an empty name, and `changes` then keeps
    the existing name rather than blanking it.
    """
    section_names = section_names or {}
    return {
        "content": task.get("content") or "",
        "description": task.get("description") or "",
        "priority": priority(task),
        "labels": labels(task),
        "due_date": due_date(task),
        "deadline": deadline(task),
        "section_name": section_names.get(task.get("section_id") or "", ""),
        "project_name": project_name,
    }


def _same(left, right):
    """Compare the way PocketBase will store it, so 1 and '1' agree."""
    return str(left if left is not None else "") == str(right if right is not None else "")


def changes(existing, wanted):
    """Only the fields that actually differ.

    Only fields in TODOIST_OWNS are considered. A field not in the
    returned dict is not in the request body, and PocketBase leaves a
    field it was not sent unchanged.

    An empty `section_name` or `project_name` never blanks a stored one,
    because empty there means the id was not recognised, not that the
    task left its section. Other fields can be cleared: an empty
    description means the operator cleared it in Todoist.
    """
    out = {}
    for field in TODOIST_OWNS:
        if field not in wanted:
            continue
        new = wanted[field]
        old = existing.get(field)
        if _same(old, new):
            continue
        if not str(new or "").strip() and str(old or "").strip():
            # An unknown section or project id, not a real change.
            if field in ("section_name", "project_name"):
                continue
        out[field] = new
    return out


def link(content, index, internal_id, existing):
    """The client or supplier link for a task, or nothing.

    Returns a payload such as {"client": id}, or {} when:

      the record already has a client (a link someone set is a decision,
      and a name in a title is weaker evidence than a decision);
      the matcher cannot place the name (an unfiled task is visible, a
      wrongly filed one looks correct);
      two records claim the name equally (core/matching.py refuses ties).

    A supplier match also sets the internal client, because on a purchase
    from a supplier the customer is Splatt itself and every task needs a
    client. An existing supplier link is not overwritten.
    """
    if str(existing.get("client") or "").strip():
        return {}
    found = matching.match(content or "", index)
    if not found:
        return {}
    if found.kind == "client":
        return {"client": found.record_id}
    if not internal_id:
        # Without an internal client id the record would get a supplier
        # but no client, so it is left untouched instead.
        return {}
    out = {"supplier": found.record_id, "client": internal_id}
    if str(existing.get("supplier") or "").strip():
        del out["supplier"]
    return out


def link_job(content, client_id, job_index, existing):
    """The project link for a task, or the reason there is not one.

    Returns (payload, unplaced). When the matcher runs, exactly one of
    the two is filled, so a refusal is always visible to the caller and
    ends up in `unplaced_jobs`. Both are empty when there is no job index
    or the record already has a project.

    The refusals mirror `link`: a project already set is kept; a title
    that names none of the client's projects is not linked (core/jobmatch
    never uses "the client's only project" as a rule); two projects that
    match equally are both named and neither is linked.

    `client_id` is passed in rather than read off the record, because on
    the pass that fills a blank client this needs the client about to be
    written, not the blank that is there now.
    """
    if job_index is None:
        return {}, None
    if str(existing.get("job") or "").strip():
        return {}, None
    found, unplaced = job_index.match(content or "", client_id)
    if found is None:
        return {}, unplaced
    return {"job": found.job_id}, None


def new_record(task, section_names=None, project_name="", index=None,
               internal_id="", job_index=None):
    """The full record for a task PocketBase has never seen.

    On a create there is nothing to overwrite, so the client link is
    included with the owned fields, and the project link is chosen using
    the client this record is being created with. New tasks therefore
    arrive already filed when the matchers are sure.
    """
    content = task.get("content") or ""
    record = derive(task, section_names, project_name)
    record["todoist_id"] = str(task.get("id") or "")
    record["status"] = "open"
    record["source"] = "sync"
    if index is not None:
        record.update(link(content, index, internal_id, {}))
    if job_index is not None:
        payload, _ = link_job(content, record.get("client", ""), job_index, {})
        record.update(payload)
    return record


def canonical(records):
    """One record per Todoist id, plus the duplicates that were not chosen.

    Returns (best, extra): a dict of Todoist id to the chosen record, and
    the list of records not chosen. The oldest record (by `created`) wins,
    because it has been on the board longest, so it carries anything set
    by hand and is the one other records and notes refer to. Records with
    no Todoist id are skipped.
    """
    best, extra = {}, []
    for record in records:
        tid = str(record.get("todoist_id") or "").strip()
        if not tid:
            continue
        held = best.get(tid)
        if held is None:
            best[tid] = record
            continue
        if str(record.get("created") or "") < str(held.get("created") or ""):
            best[tid] = record
            extra.append(held)
        else:
            extra.append(record)
    return best, extra


def plan(tasks, records, section_names=None, project_name="", index=None,
         internal_id="", job_index=None, now=None):
    """Everything one pass would do, worked out before anything is sent.

    See the module docstring for the keys of the returned dict. Nothing
    here touches the network, so the whole plan can be printed and read
    before a single write happens. Raises `Refused` when the 50% floor
    is not met.

    `unplaced_jobs` lists every open task the project matcher examined
    and would not place, with the reason and the projects it considered.
    The validator reads it, so a refusal to guess is reported rather than
    looking the same as never having tried.
    """
    existing, duplicates = canonical(records)
    seen = set()
    creates, updates, links, job_links, unplaced_jobs = [], [], [], [], []

    for task in tasks:
        tid = str(task.get("id") or "")
        if not tid:
            continue
        seen.add(tid)
        wanted = derive(task, section_names, project_name)
        record = existing.get(tid)

        if record is None:
            creates.append({
                "task": task,
                "payload": new_record(task, section_names, project_name,
                                      index, internal_id, job_index),
            })
            continue

        if (record.get("source") or "") == "dashboard":
            # An edit is waiting to go out to Todoist (core/reverse.py).
            # Pulling Todoist's older wording in now would erase it.
            continue

        diff = changes(record, wanted)
        if diff:
            updates.append({"task": task, "id": record["id"], "payload": diff,
                            "before": {k: record.get(k) for k in diff}})

        if record.get("status") == "completed":
            continue

        content = task.get("content") or ""
        filled = {}
        if index is not None:
            filled = link(content, index, internal_id, record)
            if filled:
                links.append({"task": task, "id": record["id"], "payload": filled})

        if job_index is not None:
            # Use the client this record will have once this pass lands,
            # not the blank it has now, so a task gaining its first client
            # link can gain its project in the same pass.
            client_id = str(record.get("client") or "").strip() or filled.get("client", "")
            payload, unplaced = link_job(content, client_id, job_index, record)
            if payload:
                job_links.append({"task": task, "id": record["id"],
                                  "payload": payload,
                                  "title": job_index.title(payload["job"])})
            elif unplaced is not None:
                unplaced_jobs.append({
                    "id": record["id"],
                    "content": content,
                    "client": client_id,
                    "reason": unplaced.reason,
                    "candidates": list(unplaced.candidates),
                })

    open_records = [r for r in existing.values() if r.get("status") != "completed"]
    gone = [r for r in open_records if str(r.get("todoist_id")) not in seen]

    if open_records and len(seen) < len(open_records) * FLOOR:
        raise Refused(
            "Todoist returned %d tasks against %d open records. That is below "
            "the floor of %d, so this pass will not mark anything completed. "
            "Either a great many tasks were finished at once, which is worth "
            "seeing for yourself, or the read was bad."
            % (len(seen), len(open_records), int(len(open_records) * FLOOR))
        )

    return {
        "creates": creates,
        "updates": updates,
        "links": links,
        "job_links": job_links,
        "unplaced_jobs": unplaced_jobs,
        "completions": [{"id": r["id"], "content": r.get("content") or "",
                         "todoist_id": r.get("todoist_id")} for r in gone],
        "duplicates": [{"id": r["id"], "todoist_id": r.get("todoist_id"),
                        "content": r.get("content") or ""} for r in duplicates],
        "seen": len(seen),
        "held": len(existing),
        "at": (now or datetime.now(timezone.utc)).isoformat(),
    }


def describe(item):
    """The plan in words, for the pass that writes nothing."""
    lines = [
        "",
        "Todoist returned %d tasks. PocketBase holds %d." % (item["seen"], item["held"]),
        "",
    ]

    def block(title, rows, render):
        lines.append("%s: %d" % (title, len(rows)))
        for row in rows[:200]:
            lines.append("  %s" % render(row))
        if len(rows) > 200:
            lines.append("  and %d more" % (len(rows) - 200))
        lines.append("")

    block("New tasks to add", item["creates"],
          lambda r: (r["payload"].get("content") or "")[:88])

    def field_change(row):
        parts = []
        for key, value in sorted(row["payload"].items()):
            before = str(row["before"].get(key) or "")[:24]
            parts.append("%s %r to %r" % (key, before, str(value)[:24]))
        return "%s\n      %s" % ((row["task"].get("content") or "")[:78],
                                 "; ".join(parts))

    block("Tasks whose details changed", item["updates"], field_change)

    block("Tasks that would gain a client link", item["links"],
          lambda r: "%s\n      %s" % ((r["task"].get("content") or "")[:78],
                                      r["payload"]))

    block("Tasks that would gain a project link", item.get("job_links") or [],
          lambda r: "%s\n      under %s" % ((r["task"].get("content") or "")[:78],
                                            r.get("title") or r["payload"]["job"]))

    # Grouped by reason rather than one line per task. Many tasks whose
    # client has no live project are one thing to fix, not many, and
    # listing them all would bury the few ambiguous ones, which are
    # printed individually below.
    unplaced = item.get("unplaced_jobs") or []
    if unplaced:
        counts = {}
        for row in unplaced:
            counts[row["reason"]] = counts.get(row["reason"], 0) + 1
        lines.append("Tasks left with no project link: %d" % len(unplaced))
        for reason, count in sorted(counts.items()):
            lines.append("  %-3d %s" % (count, jobmatch.UNPLACED_REASONS.get(reason, reason)))
        for row in unplaced:
            if row["reason"] != jobmatch.AMBIGUOUS:
                continue
            lines.append("  ambiguous: %s" % row["content"][:70])
            lines.append("      %s" % "; ".join(row["candidates"][:4]))
        lines.append("  The validator lists these, they are not lost.")
        lines.append("")

    block("Records to mark completed, the task is gone from Todoist",
          item["completions"], lambda r: r["content"][:88])

    if item["duplicates"]:
        lines.append("Duplicate records sharing a Todoist id, left alone: %d"
                     % len(item["duplicates"]))
        for row in item["duplicates"][:20]:
            lines.append("  %s  %s" % (row["id"], row["content"][:70]))
        lines.append("  Deleting is not a sync's job. Check these and remove them by hand.")
        lines.append("")

    return "\n".join(lines)
