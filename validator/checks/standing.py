"""
Standing checks: the named checks that rules refer to with `check:`.

Each function here is registered under a name with the @check decorator,
and a rule in config/validation-rules.yaml selects it by that name:

    - id: writes_landed
      check: reread_claimed_writes      # -> reread_claimed_writes() below

Every check has the signature check(rule, ctx) and returns a Result (or a
list of Results) from validator/result.py. `ctx` is the read-only Context
for the run. A check reports what it finds; it never repairs anything.

If a rule names a check that is not registered here, the engine returns
an ERROR for that rule when it runs, never a pass, and
tests/test_validator.py fails for the shipped rules file.

Groups, in file order
    1.  claimed writes landed (the write ledger)
    2.  schema drift between core/schema.py and the live PocketBase
    3.  Todoist and PocketBase agree
    4-7 orphan links (assignments, interactions, quotes, invoices, bills)
    8.  playbook runs are closed
    9.  promises in project files carry a Todoist task
    10. the board: overdue, real due dates, stalled
    11. task state triggers
    12. the rules file is reviewed
    13. freight and parts recharges
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from core import jobmatch
from core import realdue as realdue_mod
from core import schema
from core import triggers as triggers_mod
from core.ledger import TODOIST_COLLECTION
from core.todoist import TodoistClient

from .. import result
from ..context import parse_time

REGISTRY = {}

# A Todoist task id as it is written in project files: "6" followed by
# fifteen letters or digits, for example 6Xtask0000000001.
TASK_ID_PATTERN = re.compile(r"\b6[a-zA-Z0-9]{15}\b")


def check(name):
    """Decorator: register a check under the name used by `check:` in the YAML."""
    def wrap(fn):
        REGISTRY[name] = fn
        return fn
    return wrap


def _empty(value):
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False


def _confirm_todoist(ctx, task_id, action):
    """Whether a claimed Todoist write is visible in Todoist.

    Returns one of three answers:

      None   Todoist is not available to this run, so nothing is known.
             The claim is counted as skipped and mentioned in the notes,
             not reported as a failed write.
      True   the claim holds (for an update: the task is still open).
      False  the claim does not hold.

    The overdue engine (daemon/overdue.py) only moves, reschedules, labels
    or escalates tasks; it never completes or deletes one. So a task it
    claims to have updated must still be open afterwards. If it is not,
    either something else closed it during the run or the update went to
    a task id that never existed.
    """
    if not ctx.todoist:
        return None
    live = ctx.todoist_task_ids()
    present = str(task_id) in live
    # Nothing writes a Todoist delete claim today. It is handled so that a
    # future one is read the right way round.
    if action == "delete":
        return not present
    return present


# 1. Did the writes actually land
@check("reread_claimed_writes")
def reread_claimed_writes(rule, ctx):
    """Re-read every record this run claimed to write, and confirm it.

    The claims come from the write ledger (core/ledger.py): one line per
    create, update or delete made through the ops code path. For each:

      create / update   the record exists, and every field the write
                        claimed to set is present and non-empty
      delete            the record is gone
      Todoist task      checked against Todoist, see _confirm_todoist

    A write the code reported as done but PocketBase does not hold (for
    example because PocketBase silently dropped an unknown field) fails
    this rule.
    """
    if not ctx.ledger:
        return result.skipped(rule, "no write ledger for this run, nothing claimed")
    entries = ctx.ledger.entries(ctx.run_id) if ctx.run_id else ctx.ledger.entries()
    if not entries:
        return result.passed(rule, "no writes were claimed in this run")

    problems = []
    checked = 0
    skipped = 0
    for entry in entries:
        collection = entry.get("collection", "")
        record_id = entry.get("record_id", "")
        action = entry.get("action", "")

        # The overdue sweep records its Todoist moves in the same ledger.
        # Those records live in Todoist, not PocketBase, so they are
        # confirmed against Todoist; looking them up in PocketBase would
        # report every one of them as missing.
        if collection == TODOIST_COLLECTION and record_id:
            outcome = _confirm_todoist(ctx, record_id, action)
            if outcome is None:
                skipped += 1
            else:
                checked += 1
                if not outcome and action == "delete":
                    problems.append(
                        "todoist task %s was claimed deleted but is still open"
                        % record_id
                    )
                elif not outcome:
                    problems.append(
                        "todoist task %s was claimed %sd but is not in Todoist"
                        % (record_id, action)
                    )
            continue

        if action == "delete":
            if record_id and ctx.record(collection, record_id):
                problems.append(
                    "%s/%s was claimed deleted but is still present" % (collection, record_id)
                )
            checked += 1
            continue
        if not record_id:
            problems.append(
                "%s %s returned no record id, so the write cannot be confirmed"
                % (action, collection)
            )
            continue
        live = ctx.record(collection, record_id)
        checked += 1
        if not live:
            problems.append(
                "%s/%s was claimed %sd but does not exist in PocketBase"
                % (collection, record_id, action)
            )
            continue
        for field in entry.get("fields") or []:
            if field not in live:
                problems.append(
                    "%s/%s claimed to set '%s' but the field is not on the record"
                    % (collection, record_id, field)
                )
            elif _empty(live.get(field)):
                problems.append(
                    "%s/%s claimed to set '%s' but it came back empty"
                    % (collection, record_id, field)
                )

    if problems:
        return result.failed(
            rule,
            "%d of %d claimed writes did not land as claimed" % (len(problems), checked),
            problems[:40],
        )
    if skipped:
        ctx.notes.append(
            "%d Todoist claims could not be confirmed because Todoist was not "
            "available to this run" % skipped
        )
    return result.passed(rule, "all %d claimed writes confirmed" % checked)


# 2. Has the schema drifted
@check("compare_schema_to_live")
def compare_schema_to_live(rule, ctx):
    """Compare core/schema.py with the live PocketBase, in both directions.

    Reported:
      - a collection on the server that schema.py does not model (unless
        it is listed in schema.NOT_OPS as belonging to another app)
      - a collection in schema.py that is missing on the server
      - a field the code writes that the server does not have
      - a field on the server that schema.py does not know about

    PocketBase's automatic fields (id, created, updated, collectionId,
    collectionName) are ignored. tools/introspect_schema.py regenerates
    schema.py from the server.
    """
    try:
        live_raw = ctx.live_collections()
    except Exception as exc:
        return result.errored(rule, "could not read live collections: %s" % exc)

    live = {}
    for coll in live_raw:
        name = coll.get("name")
        if not name or name.startswith("_"):
            continue
        live[name] = {
            f.get("name") for f in (coll.get("fields") or coll.get("schema") or [])
            if f.get("name")
        }

    problems = []

    # Unmodelled collections are listed first: no code validates writes
    # into a collection that schema.py does not describe.
    not_ops = set(getattr(schema, "NOT_OPS", ()))
    uncovered = sorted(set(live) - set(schema.SCHEMA) - not_ops)
    for collection in uncovered:
        problems.append(
            "collection '%s' exists on the server but schema.py does not model it, "
            "so nothing validates writes into it" % collection
        )

    for collection, spec in schema.SCHEMA.items():
        if collection not in live:
            problems.append(
                "collection '%s' is in schema.py but not on the server" % collection
            )
            continue
        coded = set(spec["fields"].keys())
        actual = live[collection] - {"id", "created", "updated", "collectionId", "collectionName"}
        missing = sorted(coded - actual)
        extra = sorted(actual - coded)
        for field in missing:
            problems.append(
                "%s.%s is written by code but does not exist on the server"
                % (collection, field)
            )
        for field in extra:
            problems.append(
                "%s.%s exists on the server but schema.py does not know about it"
                % (collection, field)
            )

    if problems:
        return result.failed(
            rule,
            "%d schema differences between code and the live database" % len(problems),
            problems[:40],
        )
    return result.passed(
        rule,
        "schema.py matches all %d modelled collections, %d more are marked not ops"
        % (len(schema.SCHEMA), len(not_ops)),
    )


# 3. Do Todoist and PocketBase agree
@check("reconcile_todoist_assignments")
def reconcile_todoist_assignments(rule, ctx):
    """Every open Todoist task has an assignment record in PocketBase, and
    every open assignment points at a Todoist task that still exists.

    An "assignment" is the PocketBase mirror of one Todoist task, linked by
    `todoist_id`. Tasks created less than `tolerance.grace_seconds` before
    the run started are skipped, because the sync may not have copied them
    yet.
    """
    if not ctx.todoist:
        return result.skipped(rule, "Todoist not configured")
    try:
        tasks = ctx.todoist_tasks()
    except Exception as exc:
        return result.errored(rule, "could not read Todoist: %s" % exc)

    grace = int((rule.tolerance or {}).get("grace_seconds", 120))
    assignments = ctx.records("assignments")
    by_todoist = {}
    for record in assignments:
        key = str(record.get("todoist_id") or "").strip()
        if key:
            by_todoist[key] = record

    problems = []

    for task in tasks:
        task_id = str(task.get("id"))
        if task_id in by_todoist:
            continue
        created = parse_time(TodoistClient.created_at(task))
        if created and (ctx.started_at - created).total_seconds() < grace:
            continue
        problems.append(
            "Todoist task %s has no assignment record: %s"
            % (task_id, (task.get("content") or "")[:70])
        )

    live_ids = {str(t.get("id")) for t in tasks}
    for record in assignments:
        if record.get("archived"):
            continue
        if record.get("status") != "open":
            continue
        key = str(record.get("todoist_id") or "").strip()
        if not key:
            problems.append(
                "open assignment %s has no todoist_id, so nothing will ever remind you"
                % record.get("id")
            )
        elif key not in live_ids:
            problems.append(
                "open assignment %s points at Todoist task %s which no longer exists: %s"
                % (record.get("id"), key, (record.get("content") or "")[:60])
            )

    if problems:
        return result.failed(
            rule,
            "%d disagreements between Todoist and PocketBase" % len(problems),
            problems[:40],
        )
    return result.passed(
        rule, "%d Todoist tasks and %d assignments agree" % (len(tasks), len(assignments))
    )


# 4, 5, 6, 7. Orphan links
def _client_names(ctx):
    """Lower-cased client name and alias -> client id, from PocketBase only.
    Aliases shorter than three characters are left out, because they
    would match inside unrelated words."""
    lookup = {}
    for record in ctx.records("clients"):
        rid = record.get("id")
        name = (record.get("name") or "").strip()
        if name:
            lookup[name.lower()] = rid
        aliases = record.get("aliases") or ""
        if isinstance(aliases, str):
            aliases = [a for a in re.split(r"[,\n;]", aliases) if a.strip()]
        for alias in aliases or []:
            alias = str(alias).strip()
            if len(alias) >= 3:
                lookup[alias.lower()] = rid
    return lookup


@check("assignment_client_links")
def assignment_client_links(rule, ctx):
    """An open assignment whose text names a known client must be linked to it.

    For example, with a client "Clearwater Bottling" whose aliases include
    "ClearwaterOne", the open task "Send ClearwaterOne the quote" with an
    empty `client` field is reported.
    """
    lookup = _client_names(ctx)
    if not lookup:
        return result.errored(rule, "no clients in PocketBase, cannot match anything")

    problems = []
    open_count = 0
    for record in ctx.records("assignments"):
        if record.get("archived") or record.get("status") != "open":
            continue
        open_count += 1
        if record.get("client"):
            continue
        content = (record.get("content") or "").lower()
        for name, client_id in lookup.items():
            if name in content:
                problems.append(
                    "assignment %s mentions '%s' but has no client link: %s"
                    % (record.get("id"), name, (record.get("content") or "")[:60])
                )
                break

    if problems:
        return result.failed(
            rule,
            "%d of %d open assignments name a client but are not linked to one"
            % (len(problems), open_count),
            problems[:40],
        )
    return result.passed(rule, "all %d open assignments that name a client are linked" % open_count)


@check("assignment_job_links")
def assignment_job_links(rule, ctx):
    """Every open task is either filed under a project (job) or has a stated reason.

    The sync links a task to a job as soon as its title names one, using
    core/jobmatch.py. An open task that still has an empty `job` is in one
    of four situations:

      no client            counted, not failed. assignment_client_links
                           covers the missing client.
      no live project      counted, not failed. The client has no open
                           job to file it under.
      no evidence          reported. The client has live jobs but the
                           title names none of them, so either the title
                           needs fixing or the work needs its own job.
      two projects tied    reported with both names. A person can decide
                           in seconds; the matcher will not guess.

    A task the matcher can place but that is still unlinked is also
    reported, because it means the sync has not run since the title changed.
    """
    jobs = ctx.records("jobs")
    if not jobs:
        return result.errored(rule, "no jobs in PocketBase, nothing to file against")

    index = jobmatch.JobIndex(jobs, ctx.records("clients"))

    problems = []
    quiet = {jobmatch.NO_CLIENT: 0, jobmatch.NO_LIVE_PROJECT: 0}
    open_count = 0
    linked = 0

    for record in ctx.records("assignments"):
        if record.get("archived") or record.get("status") != "open":
            continue
        open_count += 1
        if not _empty(record.get("job")):
            linked += 1
            continue

        content = (record.get("content") or "")
        _, unplaced = index.match(content, record.get("client") or "")
        if unplaced is None:
            # The matcher can place it but the record has no link, so the
            # sync has not run since the title changed.
            problems.append(
                "assignment %s can be filed under a project but is not linked: %s"
                % (record.get("id"), content[:60]))
            continue

        if unplaced.reason in quiet:
            quiet[unplaced.reason] += 1
            continue

        problems.append(
            "assignment %s %s: %s"
            % (record.get("id"), jobmatch.explain(unplaced), content[:60]))

    notes = ("%d linked, %d have no client, %d have no live project to file under"
             % (linked, quiet[jobmatch.NO_CLIENT], quiet[jobmatch.NO_LIVE_PROJECT]))

    if problems:
        return result.failed(
            rule,
            "%d of %d open assignments have no project link and no good reason (%s)"
            % (len(problems), open_count, notes),
            problems[:40],
        )
    return result.passed(
        rule,
        "every one of %d open assignments is filed under a project or explained (%s)"
        % (open_count, notes))


def _relation_check(rule, ctx, collection, field, target, skip_archived=True, label=None):
    """Shared body of the orphan checks: every record in `collection` has
    a non-empty `field` that points at an existing record in `target`."""
    valid = {r.get("id") for r in ctx.records(target)}
    problems = []
    total = 0
    for record in ctx.records(collection):
        if skip_archived and record.get("archived"):
            continue
        total += 1
        value = record.get(field)
        if _empty(value):
            problems.append(
                "%s %s has no %s: %s"
                % (collection, record.get("id"), field, _label(record)[:60])
            )
        elif value not in valid:
            problems.append(
                "%s %s points at %s '%s' which does not exist"
                % (collection, record.get("id"), field, value)
            )
    name = label or "%s.%s" % (collection, field)
    if problems:
        return result.failed(
            rule, "%d of %d %s records are orphaned" % (len(problems), total, collection),
            problems[:40],
        )
    return result.passed(rule, "all %d %s records have a valid %s" % (total, collection, name))


def _label(record):
    """A short human-readable name for a record, for evidence lines."""
    for key in ("title", "content", "subject", "description", "invoice_number", "name"):
        if record.get(key):
            return str(record[key])
    return record.get("id", "")


@check("interaction_client_links")
def interaction_client_links(rule, ctx):
    """Every interaction links to a client or a supplier that exists.

    Either link counts. Much of the email traffic is with freight
    forwarders, couriers and equipment suppliers, which are suppliers,
    not clients. Requiring a client link would force those records onto
    an unrelated client.
    """
    clients = {r.get("id") for r in ctx.records("clients")}
    try:
        suppliers = {r.get("id") for r in ctx.records("suppliers")}
    except Exception:
        suppliers = set()

    problems = []
    total = 0
    for record in ctx.records("interactions"):
        total += 1
        client = record.get("client")
        supplier = record.get("supplier")
        if _empty(client) and _empty(supplier):
            problems.append(
                "interactions %s links to neither a client nor a supplier: %s"
                % (record.get("id"), _label(record)[:60])
            )
            continue
        if not _empty(client) and client not in clients:
            problems.append(
                "interactions %s points at client '%s' which does not exist"
                % (record.get("id"), client)
            )
        if not _empty(supplier) and supplier not in suppliers:
            problems.append(
                "interactions %s points at supplier '%s' which does not exist"
                % (record.get("id"), supplier)
            )

    if problems:
        return result.failed(
            rule,
            "%d of %d interaction records are orphaned" % (len(problems), total),
            problems[:40],
        )
    return result.passed(
        rule, "all %d interaction records link to a client or a supplier" % total
    )


@check("quote_job_links")
def quote_job_links(rule, ctx):
    return _relation_check(rule, ctx, "quotes", "job", "jobs")


@check("invoice_job_links")
def invoice_job_links(rule, ctx):
    return _relation_check(rule, ctx, "invoices", "job", "jobs", skip_archived=False)


@check("bill_job_links")
def bill_job_links(rule, ctx):
    return _relation_check(rule, ctx, "bills", "job", "jobs", skip_archived=False)


QUOTE_NUMBER_PATTERN = re.compile(r"\bQU-?\s?(\d{3,5})\b", re.IGNORECASE)


def _quote_number(quote):
    """The quote number from a quote's title or notes, or "none".

    The quotes collection has no number field, so the number is read from
    the text: "QU-8473 Capper parts" gives "QU-8473". Nothing is invented
    when no number is present.
    """
    for key in ("title", "notes"):
        m = QUOTE_NUMBER_PATTERN.search(str(quote.get(key) or ""))
        if m:
            return "QU-" + m.group(1)
    return "none"


@check("invoicing_job_has_an_invoice")
def invoicing_job_has_an_invoice(rule, ctx):
    """Every job in status invoicing has at least one invoice record.

    gate_invoicing only asks that a job has a client and a value, and
    gate_invoiced only applies once the job reaches invoiced. This rule
    covers the gap between them: a job sitting in invoicing with no
    invoice record against it.

    Read-only: it never creates an invoice or a bill, never changes a
    status and never contacts Xero.

    The number of bills linked to the job is shown on the evidence line
    but never failed on, because labour-only and service jobs have no
    supplier cost.

    The wording is "no invoice record found" rather than "not invoiced":
    the business issues invoices from two Xero organisations with
    overlapping numbers, so an invoice may exist in the other organisation
    without a record here yet.

    Only the invoicing status is in scope.
    """
    clients = {r.get("id"): r for r in ctx.records("clients")}

    invoiced_jobs = set()
    for inv in ctx.records("invoices"):
        if not _empty(inv.get("job")):
            invoiced_jobs.add(inv.get("job"))

    quotes_by_job = {}
    for q in ctx.records("quotes"):
        if q.get("archived") or _empty(q.get("job")):
            continue
        quotes_by_job.setdefault(q.get("job"), []).append(q)

    bills_by_job = {}
    for b in ctx.records("bills"):
        if _empty(b.get("job")):
            continue
        bills_by_job[b.get("job")] = bills_by_job.get(b.get("job"), 0) + 1

    problems = []
    scanned = 0
    for job in ctx.records("jobs"):
        if job.get("archived") or (job.get("status") or "").lower() != "invoicing":
            continue
        scanned += 1
        job_id = job.get("id")
        if job_id in invoiced_jobs:
            continue
        client = clients.get(job.get("client") or "", {})
        client_name = client.get("name") or "no client"
        quotes = quotes_by_job.get(job_id, [])
        quote_no = _quote_number(quotes[0]) if quotes else "none"
        value = job.get("value")
        value_text = "%s" % (value if value not in (None, "") else 0)
        problems.append(
            "job '%s' (%s) is in invoicing with no invoice record. "
            "value %s, quote %s, bills on file: %d"
            % (job.get("title") or job_id, client_name, value_text, quote_no,
               bills_by_job.get(job_id, 0))
        )

    if problems:
        return result.failed(
            rule,
            "%d of %d jobs in invoicing have no invoice record found" % (len(problems), scanned),
            problems[:40],
        )
    return result.passed(
        rule, "all %d jobs in invoicing have an invoice record" % scanned
    )


# 8. Playbook runs
#
# The playbook server (server/project-files-server.js) writes exactly three
# statuses to playbook_runs: 'open' when a run starts, then 'completed' or
# 'failed_validation' when it closes. The last two are terminal. Add a status
# here only after confirming the server writes it; tests/test_validator.py
# checks that each listed status appears in the server source.
TERMINAL_RUN_STATUSES = ("completed", "failed_validation")


@check("playbook_runs_closed")
def playbook_runs_closed(rule, ctx):
    """No playbook run is left open.

    A playbook run is one execution of a step-by-step procedure recorded
    by the playbook server. A run that is not in a terminal status is
    reported when either:

      - it started during this validation cycle (after ctx.started_at), or
      - it started a day or more before this cycle.

    A run that started shortly before this cycle and is less than a day
    old is not reported, because it may still be in progress.
    """
    try:
        runs = ctx.records("playbook_runs")
    except Exception as exc:
        return result.errored(rule, "could not read playbook_runs: %s" % exc)

    problems = []
    for run in runs:
        status = (run.get("status") or "").lower()
        if status in TERMINAL_RUN_STATUSES:
            continue
        started = parse_time(run.get("started_at") or run.get("created"))
        if started and started >= ctx.started_at:
            problems.append(
                "playbook run %s (%s) was opened during this cycle and is still %s"
                % (run.get("id"), run.get("playbook_id") or run.get("spec_id") or "?", status or "open")
            )
        elif started and (ctx.started_at - started).days >= 1:
            problems.append(
                "playbook run %s (%s) has been %s since %s and was never closed out"
                % (run.get("id"), run.get("playbook_id") or run.get("spec_id") or "?",
                   status or "open", started.date())
            )

    if problems:
        return result.failed(rule, "%d dangling playbook runs" % len(problems), problems[:40])
    return result.passed(rule, "no dangling playbook runs across %d records" % len(runs))


# 9. Promises
DEFAULT_PROMISE_SECTIONS = [
    "change log",
    "open items",
    "active client-side actions",
    "timeline",
    "supplier outreach tracker",
]

DEFAULT_PROMISE_WORDS = (
    r"\b(will send|will get|will follow up|will chase|will quote|will order|"
    r"will revert|will come back|promised|committed to|"
    r"by (mon|tue|wed|thu|fri|next week|end of week|eod|cob))\b"
)


@check("promise_task_guard")
def promise_task_guard(rule, ctx):
    """Every open promise in a project file carries a live Todoist task id.

    A "promise" is a line in a PROJECT markdown file that matches the
    promise wording (for example "will send", "by Friday") and sits under
    one of the headings listed in the rule's `options.sections`. Such a
    line must contain a Todoist task id (TASK_ID_PATTERN), and when
    Todoist is available that task must still be open:

        - We will send the revised quote by Friday (6Xtask0000000001)

    Two kinds of line are ignored:
      - lines outside the listed sections, such as a correspondence log,
        which records what was said in the past rather than open work
      - table rows whose direction column is IN, meaning the other party
        made the promise, not the business
    """
    if not ctx.project_root or not ctx.project_root.exists():
        return result.skipped(rule, "project folder not reachable")

    options = rule.raw.get("options") or {}
    sections = [s.lower() for s in (options.get("sections") or DEFAULT_PROMISE_SECTIONS)]
    promise_words = re.compile(
        options.get("promise_pattern") or DEFAULT_PROMISE_WORDS, re.IGNORECASE
    )
    inbound = re.compile(r"^\s*\|[^|]*\|\s*IN\s*\|")
    live_ids = ctx.todoist_task_ids() if ctx.todoist else None

    problems = []
    scanned = 0
    promises = 0
    for path, name, _mtime in ctx.project_files():
        if not name.lower().endswith(".md") or "project" not in name.lower():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        scanned += 1
        in_scope = False
        for number, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                heading = stripped.lstrip("# ").lower()
                in_scope = any(section in heading for section in sections)
                continue
            if not in_scope or not promise_words.search(line):
                continue
            if inbound.match(line):
                continue
            promises += 1
            found = TASK_ID_PATTERN.search(line)
            folder = path.parent.name
            if not found:
                problems.append(
                    "%s / %s line %d promises something with no Todoist task id: %s"
                    % (folder, path.name, number, stripped[:80])
                )
            elif live_ids is not None and found.group(0) not in live_ids:
                problems.append(
                    "%s / %s line %d cites task %s which no longer exists"
                    % (folder, path.name, number, found.group(0))
                )

    if problems:
        return result.failed(
            rule,
            "%d of %d open promises across %d project files are unguarded"
            % (len(problems), promises, scanned),
            problems[:40],
        )
    return result.passed(
        rule,
        "all %d open promises across %d project files carry a live task"
        % (promises, scanned),
    )


# 10. The board
#
# "The board" is the Todoist project with seven columns (sections): today,
# overdue, upcoming, waiting_client, waiting_supplier, stalled and backlog.
# The column a task is in is its state. These checks read the board only.
# None of them looks at a task's priority: priority is just a colour the
# operator may change by hand, and tests/test_no_priority_logic.py enforces
# that no validator code reads it.
#
# Two dates matter for a late task:
#   real due date  the date the work was actually due. Pinned once in the
#                  header of the task description as a line reading
#                  "Real due: 2026-07-12" after a pin marker (core/realdue.py),
#                  and mirrored in PocketBase (assignments.real_due).
#   parked date    the due date the overdue engine sets in Todoist, normally
#                  yesterday, so the task stays in overdue filters. It is
#                  rewritten on every sweep and says nothing about age.
def _section_name(ctx, task):
    """The name of the column a task is in, or "" if unknown.

    Both unknown cases are ignored by the checks: a task with no section
    sits loose at the top of the project, and a section id that is not in
    config/settings.yaml is a column this system does not manage.
    """
    section_id = (task or {}).get("section_id")
    if not section_id or not ctx.todoist:
        return ""
    return ctx.todoist.section_name_for(section_id) or ""


def _real_age(task, today):
    """Days since the task's real due date, or None if none is pinned.

    Read from the pinned line, never from the date Todoist displays,
    because that is the parked date and is always about one day old.
    """
    real = realdue_mod.parse((task or {}).get("description") or "")
    if not real or not real.date:
        return None
    return (today - real.date).days


@check("overdue_not_stalled")
def overdue_not_stalled(rule, ctx):
    """No task stays in Overdue past `overdue.stall_after_days` (default 28).

    Overdue holds work that is late but still live. Past the stall
    threshold, measured from the real due date, the overdue engine should
    have moved the task to Stalled. A task still in Overdue means the
    engine did not run or could not classify it.
    """
    if not ctx.todoist:
        return result.skipped(rule, "Todoist not configured")
    stall_after = 28
    if ctx.settings:
        stall_after = int(ctx.settings.get("overdue.stall_after_days", 28))

    try:
        tasks = ctx.todoist_tasks()
    except Exception as exc:
        return result.errored(rule, "could not read Todoist: %s" % exc)

    today = ctx.started_at.date()
    problems = []
    counted = 0
    for task in tasks:
        if _section_name(ctx, task) != "overdue":
            continue
        counted += 1
        age = _real_age(task, today)
        if age is None or age < stall_after:
            continue
        problems.append(
            "task %s is %d days past its real due date and is still in "
            "Overdue rather than Stalled: %s"
            % (task.get("id"), age, (task.get("content") or "")[:60])
        )

    if problems:
        return result.failed(
            rule,
            "%d tasks have been in Overdue past the %d day stall threshold"
            % (len(problems), stall_after),
            problems[:40],
        )
    return result.passed(
        rule, "all %d tasks in Overdue are inside the %d day threshold"
        % (counted, stall_after)
    )


@check("real_due_is_pinned")
def real_due_is_pinned(rule, ctx):
    """Every task in Overdue, Waiting (client or supplier) or Stalled has a
    real due date pinned in the header of its description.

    Without the pin, the next sweep can only see the parked date and would
    treat a months-old task as one day late.

    The header is the run of lines at the top of the description made of
    blank lines, the pin, and the lines this system writes (the
    "Project:" line, estimates and trigger lines). The pin may sit
    anywhere in the header; a pin further down, in the body, does not count.

    Today, Upcoming and Backlog are not checked. The overdue engine adds
    the pin the first time it moves a task's date, so a task added by hand
    today has none yet and does not need one.
    """
    if not ctx.todoist:
        return result.skipped(rule, "Todoist not configured")

    try:
        tasks = ctx.todoist_tasks()
    except Exception as exc:
        return result.errored(rule, "could not read Todoist: %s" % exc)

    watched = ("overdue", "waiting_client", "waiting_supplier", "stalled")
    problems = []
    checked = 0
    for task in tasks:
        column = _section_name(ctx, task)
        if column not in watched:
            continue
        checked += 1
        real = realdue_mod.parse(task.get("description") or "")
        if real and real.date:
            continue
        problems.append(
            "task %s is in %s with no real due date pinned: %s"
            % (task.get("id"), column, (task.get("content") or "")[:60])
        )

    if problems:
        return result.failed(
            rule,
            "%d tasks have lost their real due date" % len(problems),
            problems[:40],
        )
    return result.passed(
        rule, "all %d tasks in the managed columns carry a real due date"
        % checked
    )


@check("real_due_not_duplicated")
def real_due_not_duplicated(rule, ctx):
    """No task carries two pinned lines in its header.

    A real due date is written once and then read only. Two pins mean
    something wrote a new pin above an existing one, leaving the recorded
    date and a second (possibly guessed) date on the same task.
    core/realdue.apply() collapses a doubled header on its next write;
    this check reports that it happened.
    """
    if not ctx.todoist:
        return result.skipped(rule, "Todoist not configured")

    try:
        tasks = ctx.todoist_tasks()
    except Exception as exc:
        return result.errored(rule, "could not read Todoist: %s" % exc)

    problems = []
    for task in tasks:
        pins = realdue_mod.header_pins(task.get("description") or "")
        if len(pins) < 2:
            continue
        problems.append(
            "task %s carries %d pinned lines (%s): %s"
            % (task.get("id"), len(pins),
               ", ".join(pin.date.isoformat() for _, pin in pins),
               (task.get("content") or "")[:60])
        )

    if problems:
        return result.failed(
            rule, "%d tasks carry more than one pinned line" % len(problems),
            problems[:40],
        )
    return result.passed(
        rule, "all %d tasks carry at most one pinned line" % len(tasks)
    )


@check("real_due_mirror_agrees")
def real_due_mirror_agrees(rule, ctx):
    """Every pinned task has the same real due date stored in PocketBase.

    The PocketBase copy (assignments.real_due) is the durable one: a
    person can overwrite a Todoist description by accident, and the
    mirror is what survives. An empty mirror therefore protects nothing
    and is reported, as is a mirror holding a different date.

    Differences are reported, not repaired. When the two disagree,
    PocketBase wins at read time.
    """
    if not ctx.todoist:
        return result.skipped(rule, "Todoist not configured")
    if not ctx.pb:
        return result.skipped(rule, "PocketBase not configured")

    try:
        tasks = ctx.todoist_tasks()
    except Exception as exc:
        return result.errored(rule, "could not read Todoist: %s" % exc)

    try:
        records = ctx.records("assignments")
    except Exception as exc:
        return result.errored(rule, "could not read PocketBase: %s" % exc)

    mirrors = {}
    for record in records:
        todoist_id = str(record.get("todoist_id") or "")
        if todoist_id:
            mirrors[todoist_id] = record

    empty = []
    clashes = []
    pinned = 0
    for task in tasks:
        description = task.get("description") or ""
        real = realdue_mod.parse(description)
        if not real:
            continue
        pinned += 1
        task_id = str(task.get("id"))
        record = mirrors.get(task_id)
        if record is None:
            # Not synced yet. reconcile_todoist_assignments reports that.
            continue
        stored = record.get(realdue_mod.PB_FIELD)
        if _empty(stored):
            empty.append(
                "task %s is pinned %s with an empty mirror: %s"
                % (task_id, real.date.isoformat(),
                   (task.get("content") or "")[:60])
            )
            continue
        clash = realdue_mod.mismatch(description, stored)
        if clash:
            clashes.append(
                "task %s is pinned %s, PocketBase holds %s: %s"
                % (task_id, clash["pinned"], clash["pocketbase"],
                   (task.get("content") or "")[:60])
            )

    if empty or clashes:
        return result.failed(
            rule,
            "%d pinned tasks have no mirror and %d disagree with theirs"
            % (len(empty), len(clashes)),
            (clashes + empty)[:40],
        )
    return result.passed(
        rule, "all %d pinned tasks agree with their mirror" % pinned
    )


@check("stalled_has_no_date")
def stalled_has_no_date(rule, ctx):
    """No task in the Stalled column has a due date.

    Stalled holds work that has stopped moving. With no date, a task does
    not appear in Today, Upcoming or any overdue filter, so abandoned work
    stays out of the daily view without being deleted. The real due date
    is still on the pinned line.
    """
    if not ctx.todoist:
        return result.skipped(rule, "Todoist not configured")

    try:
        tasks = ctx.todoist_tasks()
    except Exception as exc:
        return result.errored(rule, "could not read Todoist: %s" % exc)

    problems = []
    counted = 0
    for task in tasks:
        if _section_name(ctx, task) != "stalled":
            continue
        counted += 1
        due = (task.get("due") or {}).get("date")
        if not due:
            continue
        problems.append(
            "stalled task %s still has a due date of %s, so it is still "
            "showing up in filters: %s"
            % (task.get("id"), due, (task.get("content") or "")[:60])
        )

    if problems:
        return result.failed(
            rule,
            "%d stalled tasks still have a due date" % len(problems),
            problems[:40],
        )
    return result.passed(rule, "all %d stalled tasks are dateless" % counted)


# 11. Task state triggers
#
# A trigger is a line in a task description, "On complete: job to quoted",
# mirrored in assignments.on_complete_job_status. When the task is
# completed, the trigger pass (core/triggers.py, run by daemon/triggers.py)
# moves the linked job forward to that status. Its four rules are:
#   R0  give a task the trigger its title implies
#   R1  on completion, move the job (forward only)
#   R2  create one chase task for a quote left unanswered
#   R3  close the chase task once the job has left quoted
#
# These eight checks read PocketBase and Todoist directly. core/triggers is
# imported only for field names, the forward order and line parsing, never
# for its decisions, so a check can disagree with the engine.


def _sensitive_clients(ctx):
    """Ids of clients marked critical. The trigger engine only reports on
    these and never acts, so the checks excuse them instead of failing."""
    return {c.get("id") for c in ctx.records("clients") if c.get("critical")}


def _job_index(ctx):
    return {j.get("id"): j for j in ctx.records("jobs") if j.get("id")}


def _status_of(job):
    return str((job or {}).get("status") or "").strip().lower()


def _rank(status):
    """Position on the forward line (quoting, quoted, won, invoicing,
    invoiced), or None for a status off the line."""
    try:
        return triggers_mod.FORWARD_ORDER.index(status)
    except ValueError:
        return None


def _at_or_past(current, target):
    """Whether a job has reached a status or gone beyond it. paid and
    completed count as past every target."""
    if current in triggers_mod.PAST_THE_LINE:
        return True
    here, there = _rank(current), _rank(target)
    if here is None or there is None:
        return False
    return here >= there


def _has_numbered_invoice(ctx, job_id):
    for invoice in ctx.records("invoices"):
        if str(invoice.get("job") or "") != str(job_id):
            continue
        if invoice.get("archived"):
            continue
        if not _empty(invoice.get("invoice_number")):
            return True
    return False


@check("trigger_line_matches_record")
def trigger_line_matches_record(rule, ctx):
    """The trigger line in the task and the field on the assignment agree.

    Two copies on purpose, like the real due date: the line is what a
    person reads in Todoist and can overwrite by accident, and the field
    is the durable copy that wins. A line with an empty field is reported
    too, because the trigger cannot fire until the field is set. This is
    common for a task the agent has just created, before the next trigger
    pass mirrors the line. A task with two different trigger lines is
    reported because neither can be read reliably.
    """
    if not ctx.todoist:
        return result.skipped(rule, "Todoist not configured")

    try:
        tasks = ctx.todoist_tasks()
    except Exception as exc:
        return result.errored(rule, "could not read Todoist: %s" % exc)

    mirrors = {}
    for record in ctx.records("assignments"):
        todoist_id = str(record.get("todoist_id") or "")
        if todoist_id:
            mirrors[todoist_id] = record

    problems = []
    checked = 0
    for task in tasks:
        record = mirrors.get(str(task.get("id")))
        if record is None:
            # Not synced yet. reconcile_todoist_assignments reports that.
            continue
        description = task.get("description") or ""
        stored = record.get(triggers_mod.PB_STATUS_FIELD)
        if triggers_mod.on_complete_count(description) > 1:
            problems.append(
                "task %s carries two different trigger lines, so nothing can "
                "read it: %s" % (task.get("id"), (task.get("content") or "")[:60]))
            continue
        clash = triggers_mod.mismatch(description, stored)
        if not clash:
            if triggers_mod.parse_on_complete(description):
                checked += 1
            continue
        problems.append(
            "task %s says %s and PocketBase says %s: %s"
            % (task.get("id"), clash["line"], clash["pocketbase"],
               (task.get("content") or "")[:60]))

    if problems:
        return result.failed(
            rule,
            "%d tasks disagree with their trigger record" % len(problems),
            problems[:40],
        )
    return result.passed(
        rule, "all %d triggered tasks agree with their record" % checked)


@check("trigger_tasks_link_a_job")
def trigger_tasks_link_a_job(rule, ctx):
    """Every non-archived assignment that carries a trigger is linked to a job.

    Without a job the trigger can never fire: when the task is completed
    the engine refuses it on every pass because there is nothing to move.
    """
    problems = []
    counted = 0
    for record in ctx.records("assignments"):
        if record.get("archived"):
            continue
        if _empty(record.get(triggers_mod.PB_STATUS_FIELD)):
            continue
        counted += 1
        if _empty(record.get("job")):
            problems.append(
                "task %s moves a project to %s and is not linked to one: %s"
                % (record.get("todoist_id") or record.get("id"),
                   record.get(triggers_mod.PB_STATUS_FIELD),
                   (record.get("content") or "")[:60]))

    if problems:
        return result.failed(
            rule,
            "%d of %d triggered tasks have no project link"
            % (len(problems), counted),
            problems[:40],
        )
    return result.passed(
        rule, "all %d triggered tasks are linked to a project" % counted)


@check("quote_tasks_carry_a_trigger")
def quote_tasks_carry_a_trigger(rule, ctx):
    """Tasks whose title implies a status change carry the matching trigger.

    Uses the same `triggers.title_patterns` from config/settings.yaml as
    rule R0 of the trigger pass. For example "Send the quote" on a job in
    quoting should carry "job to quoted". If it does not, R0 has not run
    since the task appeared, or declined to act, and completing the task
    will not move the job. The question here is whether the engine acted
    on its patterns, not whether the patterns are right.
    """
    rules_for = triggers_mod.Rules(getattr(ctx, "settings", None))
    jobs = _job_index(ctx)

    problems = []
    counted = 0
    for record in ctx.records("assignments"):
        if record.get("archived") or record.get("status") == "completed":
            continue
        job = jobs.get(str(record.get("job") or ""))
        if job is None or job.get("archived"):
            continue
        target, pattern = rules_for.match_title(
            record.get("content") or "", job.get("status"))
        if not target:
            continue
        counted += 1
        held = str(record.get(triggers_mod.PB_STATUS_FIELD) or "").lower()
        if held == target:
            continue
        problems.append(
            "task %s matches %r on a %s project and carries %s, expected %s: %s"
            % (record.get("todoist_id") or record.get("id"), pattern,
               _status_of(job), held or "no trigger", target,
               (record.get("content") or "")[:60]))

    if problems:
        return result.failed(
            rule,
            "%d of %d tasks that look like a state change carry no trigger"
            % (len(problems), counted),
            problems[:40],
        )
    return result.passed(
        rule, "all %d tasks that look like a state change carry one" % counted)


@check("trigger_change_was_logged")
def trigger_change_was_logged(rule, ctx):
    """Every job moved by a task has an interaction recording the move.

    Every automatic status change should read as a plain sentence in the
    client's history. A job whose `status_changed_by` starts with "task:"
    needs an interaction with a source_key of the form
    "trigger:r1:<assignment>:<status>" for its current status.
    """
    logged = set()
    for row in ctx.records("interactions"):
        key = str(row.get("source_key") or "")
        if key.startswith("%s:r1:" % triggers_mod.SOURCE):
            logged.add((str(row.get("job") or ""), key.rsplit(":", 1)[-1]))

    problems = []
    counted = 0
    for job in ctx.records("jobs"):
        by = str(job.get(triggers_mod.JOB_CHANGED_BY) or "")
        if not by.startswith("task:"):
            continue
        counted += 1
        if (str(job.get("id")), _status_of(job)) in logged:
            continue
        problems.append(
            "job '%s' says it was moved to %s by %s and no interaction records it"
            % (job.get("title") or job.get("id"), _status_of(job), by))

    if problems:
        return result.failed(
            rule,
            "%d of %d task driven status changes are missing from the client "
            "history" % (len(problems), counted),
            problems[:40],
        )
    return result.passed(
        rule, "all %d task driven status changes are in the client history"
        % counted)


#: How far back completed_trigger_landed looks, in days. Long enough to
#: keep watching a trigger that fired last week, short enough that tasks
#: completed before triggers existed are not reported indefinitely.
TRIGGER_LANDING_DAYS = 14


@check("completed_trigger_landed")
def completed_trigger_landed(rule, ctx):
    """A completed task with a trigger moved its job to at least that status.

    Looks at assignments completed in the last TRIGGER_LANDING_DAYS whose
    trigger names a valid target. A trigger that did nothing is the
    failure that matters most here, because the completed task makes the
    work look handled.

    The refusals the engine is supposed to make are counted as excused,
    not failed:
      - no job link (trigger_tasks_link_a_job reports that)
      - the job is missing or archived
      - the client is marked critical (sensitive)
      - the job's current status is off the forward line
      - the target is invoiced and the job has no numbered invoice

    These conditions are read directly from PocketBase here rather than
    taken from the engine, so this check can still catch the engine
    being wrong.
    """
    jobs = _job_index(ctx)
    sensitive = _sensitive_clients(ctx)
    cutoff = ctx.started_at - timedelta(days=TRIGGER_LANDING_DAYS)

    problems, excused = [], 0
    counted = 0
    for record in ctx.records("assignments"):
        target = str(record.get(triggers_mod.PB_STATUS_FIELD) or "").lower()
        if target not in triggers_mod.TARGETS:
            continue
        if record.get("status") != "completed":
            continue
        when = parse_time(record.get("updated"))
        if when is not None and when < cutoff:
            continue
        job_id = str(record.get("job") or "")
        if not job_id:
            # Reported by trigger_tasks_link_a_job. Failing it here too
            # would count one problem twice.
            excused += 1
            continue
        job = jobs.get(job_id)
        counted += 1
        if job is None or job.get("archived"):
            excused += 1
            continue
        if str(job.get("client") or "") in sensitive:
            excused += 1
            continue
        current = _status_of(job)
        if _at_or_past(current, target):
            continue
        if _rank(current) is None:
            excused += 1
            continue
        if target == "invoiced" and not _has_numbered_invoice(ctx, job_id):
            excused += 1
            continue
        problems.append(
            "task %s was completed and should have moved '%s' to %s, and it "
            "is %s: %s"
            % (record.get("todoist_id") or record.get("id"),
               job.get("title") or job_id, target, current or "unset",
               (record.get("content") or "")[:60]))

    if problems:
        return result.failed(
            rule,
            "%d of %d finished triggers did not land" % (len(problems), counted),
            problems[:40],
        )
    return result.passed(
        rule,
        "all %d finished triggers landed, %d were refused for a stated reason"
        % (counted, excused))


@check("quoted_jobs_have_a_chase")
def quoted_jobs_have_a_chase(rule, ctx):
    """A job quoted for `triggers.quoted_chase_after_days` (default 7) or
    longer has a chase task, open or completed.

    The chase task is the one rule R2 creates (auto_kind "quoted_chase").
    Once it exists, this rule is satisfied whatever its state, because the
    waiting engine then owns the follow-up. Jobs on sensitive (critical)
    accounts are counted, not failed, and jobs with no status_changed_at
    are left to job_status_change_is_stamped.
    """
    settings = getattr(ctx, "settings", None)
    days = int((settings.get("triggers.quoted_chase_after_days", 7)
                if settings else 7) or 7)
    sensitive = _sensitive_clients(ctx)

    chased = {str(r.get("job") or "") for r in ctx.records("assignments")
              if str(r.get(triggers_mod.PB_AUTO_FIELD) or "") == triggers_mod.QUOTED_CHASE}

    problems, reported_only, unstamped = [], 0, 0
    counted = 0
    for job in ctx.records("jobs"):
        if job.get("archived") or _status_of(job) != "quoted":
            continue
        changed = parse_time(job.get(triggers_mod.JOB_CHANGED_AT))
        if changed is None:
            # Reported by job_status_change_is_stamped.
            unstamped += 1
            continue
        waited = (ctx.started_at - changed).days
        if waited < days:
            continue
        counted += 1
        if str(job.get("id")) in chased:
            continue
        if str(job.get("client") or "") in sensitive:
            reported_only += 1
            continue
        problems.append(
            "job '%s' has been quoted %d days with no chase task"
            % (job.get("title") or job.get("id"), waited))

    if problems:
        return result.failed(
            rule,
            "%d of %d quotes older than %d days have no chase task"
            % (len(problems), counted, days),
            problems[:40],
        )
    return result.passed(
        rule,
        "all %d quotes older than %d days have a chase task, %d on sensitive "
        "accounts are reported not chased, %d quoted jobs carry no change date"
        % (counted, days, reported_only, unstamped))


@check("no_orphan_chase_tasks")
def no_orphan_chase_tasks(rule, ctx):
    """No open chase task belongs to a job that has left quoted.

    Rule R3 of the trigger pass should close the chase once the job moves
    on. An open chase on a job that is won, invoiced or lost would ask the
    client about a decision already made, so this blocks. Only tasks the
    engine created (auto_kind "quoted_chase") are checked; a chase task
    the operator wrote by hand is theirs to manage. Sensitive accounts are
    counted, not failed.
    """
    jobs = _job_index(ctx)
    sensitive = _sensitive_clients(ctx)

    problems, reported_only = [], 0
    counted = 0
    for record in ctx.records("assignments"):
        if str(record.get(triggers_mod.PB_AUTO_FIELD) or "") != triggers_mod.QUOTED_CHASE:
            continue
        if record.get("archived") or record.get("status") == "completed":
            continue
        counted += 1
        job = jobs.get(str(record.get("job") or ""))
        if job is None:
            problems.append(
                "chase task %s is not linked to a project that exists: %s"
                % (record.get("todoist_id") or record.get("id"),
                   (record.get("content") or "")[:60]))
            continue
        if _status_of(job) == "quoted":
            continue
        if str(job.get("client") or "") in sensitive:
            reported_only += 1
            continue
        problems.append(
            "chase task %s is still open and '%s' is %s: %s"
            % (record.get("todoist_id") or record.get("id"),
               job.get("title") or job.get("id"), _status_of(job) or "unset",
               (record.get("content") or "")[:60]))

    if problems:
        return result.failed(
            rule,
            "%d of %d open chase tasks belong to a project that is no longer "
            "quoted" % (len(problems), counted),
            problems[:40],
        )
    return result.passed(
        rule,
        "all %d open chase tasks belong to a quoted project, %d on sensitive "
        "accounts are reported not closed" % (counted, reported_only))


@check("job_status_change_is_stamped")
def job_status_change_is_stamped(rule, ctx):
    """Every non-archived job records when its status last changed.

    `status_changed_at` is what the chase clock runs from. Neither
    alternative works: `created` is the day the job was opened, and
    `updated` changes whenever any field on the record is edited.

    tools/backfill_status_changed.py fills the field in once for existing
    jobs. After that, whatever changes a status writes it.
    """
    problems = []
    counted = 0
    for job in ctx.records("jobs"):
        if job.get("archived"):
            continue
        counted += 1
        if not _empty(job.get(triggers_mod.JOB_CHANGED_AT)):
            continue
        problems.append(
            "job '%s' (%s) has no status_changed_at"
            % (job.get("title") or job.get("id"), _status_of(job) or "unset"))

    if problems:
        return result.failed(
            rule,
            "%d of %d live jobs never recorded when their status moved"
            % (len(problems), counted),
            problems[:40],
        )
    return result.passed(
        rule, "all %d live jobs record when their status moved" % counted)


# 12. Is the rules file itself current
@check("rules_freshness")
def rules_freshness(rule, ctx):
    """Every rule was reviewed within `meta.review_after_days`.

    Each rule records a `last_reviewed` date. This check fails when any of
    them is older than the review window, so the rules file is known to be
    current rather than assumed to be.
    """
    ruleset = getattr(ctx, "ruleset", None)
    if ruleset is None:
        return result.skipped(rule, "ruleset not attached to context")
    days = ruleset.review_after_days()
    stale = ruleset.stale_rules(today=ctx.started_at.date())
    if stale:
        lines = [
            "%s last reviewed %s" % (r.id, r.last_reviewed) for r in stale
        ]
        return result.failed(
            rule,
            "%d of %d rules have not been reviewed in %d days"
            % (len(stale), len(ruleset.rules), days),
            lines[:40],
        )
    oldest = min((r.last_reviewed for r in ruleset.rules if isinstance(r.last_reviewed, date)),
                 default=None)
    return result.passed(
        rule,
        "all %d rules reviewed within %d days, oldest is %s"
        % (len(ruleset.rules), days, oldest),
    )


# 13. Recharge margin
@check("recharge_margin_applied")
def recharge_margin_applied(rule, ctx):
    """Every shipping or parts bill is recharged to the client.

    For each such bill on a job that is not cancelled or lost, the
    non-cancelled invoices on the same job must add up to at least the
    bill. For example a freight bill of 900 against invoices totalling 700
    is reported as short. The check does not verify the size of the
    handling margin; an invoice total equal to the bill passes.
    """
    bills = [b for b in ctx.records("bills") if (b.get("type") or "") in ("shipping", "parts")]
    if not bills:
        return result.passed(rule, "no shipping or parts bills to recharge")

    jobs = ctx.index("jobs")
    problems = []
    for bill in bills:
        job_id = bill.get("job")
        if not job_id:
            continue
        job = jobs.get(job_id)
        if job and (job.get("status") or "") in ("cancelled", "lost"):
            continue
        amount = float(bill.get("amount") or 0)
        if amount <= 0:
            continue
        invoiced = sum(
            float(inv.get("amount") or 0) for inv in ctx.related("invoices", "job", job_id)
            if (inv.get("status") or "") != "cancelled"
        )
        if invoiced <= 0:
            problems.append(
                "bill %s of %.2f on job '%s' has no invoice against it at all"
                % (bill.get("description") or bill.get("id"), amount,
                   (job or {}).get("title", job_id))
            )
        elif invoiced < amount:
            problems.append(
                "bill %s of %.2f on job '%s' is only invoiced at %.2f, so the recharge "
                "is short before any margin"
                % (bill.get("description") or bill.get("id"), amount,
                   (job or {}).get("title", job_id), invoiced)
            )

    if problems:
        return result.failed(
            rule, "%d supplier bills are not fully recharged" % len(problems), problems[:40]
        )
    return result.passed(rule, "all %d shipping and parts bills are recharged" % len(bills))
