"""
Requirement types: the building blocks that gates and money rules list under `requires:`.

A gate in the rules file says what must be true for a job in a given
state; this module says how to find out. For example gate_invoiced
requires three things:

    requires:
      - type: related_record_exists       # an invoice record for this job
        collection: invoices
        match: { job: "$job.id" }
      - type: xero_record_exists          # the invoice exists in Xero
      - type: file_in_project_folder      # the PDF is filed
        patterns: ["*INV*.pdf"]

Keeping the "what" in YAML and the "how" here means a new gate can be
added by editing the rules file, as long as it uses existing types.

Every evaluator takes (spec, subject, ctx) and returns (ok, detail):

    ok = True    the requirement is met
    ok = False   it is not met; detail says what is missing
    ok = None    it could not be checked; the engine reports a SKIP

`subject` is the record the gate is about, normally a job. An unknown
requirement type raises UnknownRequirement, which the engine turns into an
ERROR result, never a pass.
"""

from __future__ import annotations

import re

REGISTRY = {}


def requirement(name):
    """Decorator: register an evaluator under the `type:` name used in the YAML."""
    def wrap(fn):
        REGISTRY[name] = fn
        return fn
    return wrap


class UnknownRequirement(Exception):
    pass


def evaluate(spec, subject, ctx, gate_results=None):
    """Run one requirement spec. The combinators and gate_passed also
    receive the cache of gate outcomes computed so far in this run."""
    kind = spec.get("type")
    if kind not in REGISTRY:
        raise UnknownRequirement(
            "requirement type '%s' is not implemented. Rules cannot ask for "
            "something the validator cannot check." % kind
        )
    if kind in ("any_of", "all_of", "gate_passed"):
        return REGISTRY[kind](spec, subject, ctx, gate_results)
    return REGISTRY[kind](spec, subject, ctx)


def _resolve(token, subject, ctx):
    """Turn a '$job.<field>' token into that field's value on the subject.

    With subject {"id": "j1"}, "$job.id" becomes "j1". A value that does
    not start with "$" (for example "accepted") is returned unchanged.
    """
    if isinstance(token, str) and token.startswith("$"):
        _, _, field = token[1:].partition(".")
        return subject.get(field or "id")
    return token


def _empty(value):
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False


# field level
@requirement("field_not_empty")
def field_not_empty(spec, subject, ctx):
    field = spec["field"]
    value = subject.get(field)
    if _empty(value):
        return False, "%s is empty" % field
    return True, "%s is set" % field


@requirement("field_true")
def field_true(spec, subject, ctx):
    field = spec["field"]
    if bool(subject.get(field)):
        return True, "%s is true" % field
    return False, "%s is not true" % field


@requirement("field_greater_than")
def field_greater_than(spec, subject, ctx):
    field = spec["field"]
    threshold = float(spec.get("value", 0))
    try:
        value = float(subject.get(field) or 0)
    except (TypeError, ValueError):
        return False, "%s is not a number" % field
    if value > threshold:
        return True, "%s is %s" % (field, value)
    return False, "%s is %s, needs to be above %s" % (field, value, threshold)


@requirement("field_matches")
def field_matches(spec, subject, ctx):
    field = spec["field"]
    pattern = spec["pattern"]
    value = str(subject.get(field) or "")
    if re.search(pattern, value):
        return True, "%s matches %s" % (field, pattern)
    return False, "%s is '%s' which does not match %s" % (field, value, pattern)


# related records
_WHERE = re.compile(r"^\s*(\w+)\s*(=|!=)\s*'?([^']*)'?\s*$")


def _where_ok(record, where):
    """Evaluate a one-comparison filter such as "status = 'accepted'" or
    "archived != true" against a record.

    Deliberately minimal: one field, `=` or `!=`, one value. Anything more
    complex belongs in a named check where it can be tested. A filter
    that does not parse matches every record.
    """
    if not where:
        return True
    match = _WHERE.match(str(where))
    if not match:
        return True
    field, operator, wanted = match.groups()
    value = record.get(field)
    if wanted in ("true", "false"):
        wanted_value = wanted == "true"
        held = bool(value)
    else:
        wanted_value = wanted
        held = "" if value is None else str(value)
    if operator == "=":
        return held == wanted_value
    return held != wanted_value


@requirement("related_record_exists")
def related_record_exists(spec, subject, ctx):
    """At least one record in `collection` matches every `match` field
    (with $job tokens resolved) and passes the optional `where` filter."""
    collection = spec["collection"]
    match = spec.get("match") or {}
    where = spec.get("where", "")
    candidates = ctx.records(collection)
    hits = []
    for record in candidates:
        ok = True
        for field, token in match.items():
            wanted = _resolve(token, subject, ctx)
            held = record.get(field)
            if isinstance(held, list):
                if wanted not in held:
                    ok = False
                    break
            elif held != wanted:
                ok = False
                break
        if ok and _where_ok(record, where):
            hits.append(record)
    if hits:
        return True, "%d matching %s record(s)" % (len(hits), collection)
    detail = "no %s record matches %s" % (collection, match)
    if where:
        detail += " with %s" % where
    return False, detail


@requirement("interaction_exists")
def interaction_exists(spec, subject, ctx):
    """An interaction linked to this job, matching `filter`, dated on or
    after `since`. `since: transition_time` uses the job's `updated`
    timestamp as the moment of the status change."""
    where = spec.get("filter", "")
    since = spec.get("since")
    from ..context import parse_time

    cutoff = None
    if since and since != "transition_time":
        cutoff = parse_time(since)
    elif since == "transition_time":
        cutoff = parse_time(subject.get("updated"))

    for record in ctx.related("interactions", "job", subject.get("id")):
        if not _where_ok(record, where):
            continue
        if cutoff:
            when = parse_time(record.get("date") or record.get("created"))
            if when and when < cutoff:
                continue
        return True, "interaction found: %s" % (record.get("subject") or record.get("id"))
    return False, "no interaction matching %s since the status change" % (where or "any")


# files
@requirement("file_in_project_folder")
def file_in_project_folder(spec, subject, ctx):
    """A file matching one of `patterns` exists in the job's client folder.

    `modified_after: job.start_date` limits the search to files changed
    after that job field. If the projects folder is not reachable, or the
    client has no folder, the requirement fails with that reason rather
    than searching elsewhere.
    """
    patterns = spec.get("patterns") or []
    after = spec.get("modified_after")
    if after and after.startswith("job."):
        after = subject.get(after.split(".", 1)[1])

    client_id = subject.get("client")
    client = ctx.record("clients", client_id) if client_id else None
    folder = ctx.folder_for_client(client) if client else None
    if not ctx.project_root or not ctx.project_root.exists():
        return False, "project folder not reachable, cannot confirm the document is filed"
    if client and not folder:
        return False, "no project folder found for client '%s'" % (client or {}).get("name")

    hits = ctx.find_files(patterns, within=folder, modified_after=after)
    if hits:
        return True, "found %s" % ", ".join(p.name for p in hits[:3])
    where = folder.name if folder else "the projects folder"
    return False, "no file matching %s in %s" % (patterns, where)


@requirement("project_md_changelog_entry")
def project_md_changelog_entry(spec, subject, ctx):
    """A markdown file in the client's folder records the job's current status.

    Passes when a .md file under the client folder mentions the status
    (case-insensitive) and was modified no more than a day before the
    job's `updated` timestamp. The one-day window allows for a person
    writing the file shortly before the sync writes the record.

    Each kind of failure gets its own message, because each needs a
    different fix:

      no folder / no .md file           create the project file
      file mentions the status but is   add a Change Log row; the text is
      older than the record             right, the timestamp is not
      recent file without the status    write the status into it
      old file without the status       both of the above
      job has no status                 fix the record, not the file

    A file can already say the status and still be stale when the record
    changed for another reason (for example a corrected job value), which
    moves `updated` past the file's modification time.
    """
    from ..context import parse_time

    client_id = subject.get("client")
    client = ctx.record("clients", client_id) if client_id else None
    folder = ctx.folder_for_client(client) if client else None
    if not folder:
        return False, "no project folder to check for a change log entry"
    since = parse_time(subject.get("updated"))
    status = str(subject.get("status") or "").lower()

    seen = 0
    stale = []
    stale_that_already_say_it = []
    fresh_without_it = []

    for path, name, mtime in ctx.project_files():
        if not str(path).startswith(str(folder)):
            continue
        if not name.upper().endswith(".MD"):
            continue
        seen += 1
        try:
            text = path.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            continue
        says_it = bool(status) and status in text

        if since and mtime < since.timestamp() - 86400:
            stale.append(name)
            if says_it:
                stale_that_already_say_it.append(name)
            continue
        if says_it:
            return True, "%s records the status" % name
        fresh_without_it.append(name)

    if not seen:
        return False, "no markdown file in %s to record this" % folder.name

    if not status:
        # An empty status can never be found in any text, so the problem
        # is the blank field on the record, not the file.
        return False, "the record has no status, so there is nothing to record"

    if stale_that_already_say_it:
        # The file is right but older than the record's last change.
        return False, (
            "%s already says '%s' but was last touched before the record "
            "changed, so nothing was written down when it changed. Add a "
            "Change Log row." % (stale_that_already_say_it[0], status)
        )

    if fresh_without_it:
        return False, (
            "%s was updated in time but does not say '%s'"
            % (", ".join(fresh_without_it[:3]), status)
        )

    if stale:
        return False, (
            "%s not touched since the record changed, and does not say '%s'"
            % (", ".join(stale[:3]), status)
        )
    return False, "no project markdown records this status change"


# todoist
@requirement("open_todoist_task_matching")
def open_todoist_task_matching(spec, subject, ctx):
    """An open Todoist task matches `pattern` (a regex, case-insensitive)
    and names this job's client, or the first 20 characters of the job
    title, in its content or description."""
    pattern = spec.get("pattern", "")
    if not ctx.todoist:
        return False, "Todoist not configured, cannot confirm a task is guarding this"
    client_id = subject.get("client")
    client = ctx.record("clients", client_id) if client_id else None
    client_name = str((client or {}).get("name", "")).lower()
    title = str(subject.get("title") or "").lower()
    regex = re.compile(pattern, re.IGNORECASE)
    for task in ctx.todoist_tasks():
        content = "%s %s" % (task.get("content") or "", task.get("description") or "")
        if not regex.search(content):
            continue
        lowered = content.lower()
        if client_name and client_name in lowered:
            return True, "task %s: %s" % (task.get("id"), (task.get("content") or "")[:60])
        if title and title[:20] in lowered:
            return True, "task %s: %s" % (task.get("id"), (task.get("content") or "")[:60])
    return False, "no open task matching %s for this job" % pattern


# money
@requirement("reconciles")
def reconciles(spec, subject, ctx):
    """Two numeric fields on the subject agree within `tolerance_percent`
    of the right-hand one.

    For example with left paid_amount 9,900, right value 10,000 and a 2
    percent tolerance, the gap is 1.0 percent and the requirement is met.
    """
    left_field = str(spec["left"]).split(".")[-1]
    right_field = str(spec["right"]).split(".")[-1]
    tolerance = float(spec.get("tolerance_percent", 0))
    try:
        left = float(subject.get(left_field) or 0)
        right = float(subject.get(right_field) or 0)
    except (TypeError, ValueError):
        return False, "%s or %s is not a number" % (left_field, right_field)
    if right == 0:
        return False, "%s is zero, so nothing can reconcile against it" % right_field
    difference = abs(left - right) / abs(right) * 100
    if difference <= tolerance:
        return True, "%s and %s agree within %.1f percent" % (left_field, right_field, difference)
    return False, "%s is %.2f but %s is %.2f, a gap of %.1f percent" % (
        left_field, left, right_field, right, difference
    )


@requirement("xero_record_exists")
def xero_record_exists(spec, subject, ctx):
    """Not yet connected: the validator process has no Xero access.

    Returns None ("could not check") rather than False, so an invoiced job
    is not failed for the wrong reason. The engine reports the rule as a
    SKIP, which appears under "Not checked" and is never counted as a pass.
    """
    return None, "Xero is not wired into the validator yet, this leg was not checked"


# combinators
@requirement("any_of")
def any_of(spec, subject, ctx, gate_results=None):
    """Met when at least one of `options` is met. Options that could not
    be checked are listed in the detail but do not satisfy it."""
    details = []
    for option in spec.get("options") or []:
        ok, detail = evaluate(option, subject, ctx, gate_results)
        if ok:
            return True, detail
        if ok is None:
            details.append("unchecked: %s" % detail)
        else:
            details.append(detail)
    return False, "none of: %s" % ", ".join(details)


@requirement("all_of")
def all_of(spec, subject, ctx, gate_results=None):
    """Met unless one of `options` fails. Options that could not be
    checked do not fail it."""
    details = []
    for option in spec.get("options") or []:
        ok, detail = evaluate(option, subject, ctx, gate_results)
        if ok is False:
            return False, detail
        details.append(detail)
    return True, ", ".join(details)


@requirement("gate_passed")
def gate_passed(spec, subject, ctx, gate_results=None):
    """Another gate must pass for this job too.

    If that gate already ran for this job in this run, its cached outcome
    is used. Usually it has not, because gates run against the status a
    job is in and, for example, a won job is no longer quoted. In that case
    the engine's gate_resolver evaluates the other gate now, so a won job
    is still required to have a quote.
    """
    wanted = spec.get("gate")
    key = (wanted, subject.get("id"))
    if gate_results and key in gate_results:
        return (True, "%s passed" % wanted) if gate_results[key] else (
            False, "%s failed, so this gate cannot pass either" % wanted
        )

    resolver = getattr(ctx, "gate_resolver", None)
    if not resolver:
        return None, "%s could not be evaluated for this job" % wanted
    ok, detail = resolver(wanted, subject)
    if ok is None:
        return None, "%s could not be evaluated: %s" % (wanted, detail)
    if ok:
        return True, "%s passed: %s" % (wanted, detail)
    return False, "%s failed, so this gate cannot pass either: %s" % (wanted, detail)
