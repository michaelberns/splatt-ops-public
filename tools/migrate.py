"""
Schema migrations: small, repeatable changes to the PocketBase schema.

Usage
    python -m tools.migrate              show what would change
    python -m tools.migrate --write      apply every change not applied yet

How it works
    Each entry in MIGRATIONS pairs a check ("is this change already in the
    database?") with the function that makes the change. Without --write
    the tool only runs the checks and lists what it would apply. With
    --write it applies each missing change and then runs the check again,
    so a change PocketBase accepted but did not keep is reported as FAILED.
    Exit code 0 when nothing failed, 1 otherwise.

    Every check reads the live schema, so running the tool twice is
    harmless and gives the same answer on any machine. A database created
    from pb_migrations/ already contains every change listed here, so on a
    fresh install each one reports "already done".

Why not click in the PocketBase admin UI
    A change made by hand leaves no record, and core/schema.py (which the
    code validates its writes against) would silently stop matching the
    database. After applying anything, regenerate that file:

        python -m tools.introspect_schema --write

    and commit the diff, so the history of the schema lives in git.
"""

from __future__ import annotations

import sys

from core.config import settings
from core.job_status import JOB_STATUSES
from core.logging_setup import setup
from core.pb import PocketBaseClient

log = setup("migrate")


class Migration:
    """One schema change.

    name        short label printed in the report
    why         one line saying what goes wrong without the change
    is_applied  fn(pb) -> bool, reads the live schema
    apply       fn(pb), makes the change
    """

    def __init__(self, name, why, applied, apply):
        self.name = name
        self.why = why
        self.is_applied = applied
        self.apply = apply


def _collection(pb, name):
    for col in pb.collections():
        if col.get("name") == name:
            return col
    raise LookupError("collection '%s' does not exist" % name)


def _has_field(pb, collection, field_name):
    col = _collection(pb, collection)
    return any(f.get("name") == field_name for f in col.get("fields") or [])


def _has_index(pb, collection, index_name):
    col = _collection(pb, collection)
    return any(index_name in idx for idx in col.get("indexes") or [])


def _select_values(pb, collection, field_name):
    """The values a select field currently offers, in order."""
    col = _collection(pb, collection)
    for field in col.get("fields") or []:
        if field.get("name") == field_name:
            return list(field.get("values") or [])
    raise LookupError("%s.%s does not exist" % (collection, field_name))


def _patch(pb, collection, changes):
    """PATCH a collection definition. `changes` replaces whole keys, so
    callers send the full new list of fields or indexes."""
    col = _collection(pb, collection)
    return pb._request(
        "PATCH", "/api/collections/%s" % col["id"], payload=changes
    )


# The migrations, in the order they are applied.

def add_supplier_to_interactions(pb):
    """Add interactions.supplier, a relation to the suppliers collection.

    An interaction (an email, call or note) about a freight forwarder or an
    equipment maker has no client to point at. Without this field it could
    only stay unlinked, which the validator reports as broken, or be linked
    to an unrelated client.
    """
    suppliers = _collection(pb, "suppliers")
    col = _collection(pb, "interactions")
    fields = list(col.get("fields") or [])
    fields.append(
        {
            "name": "supplier",
            "type": "relation",
            "required": False,
            "collectionId": suppliers["id"],
            "maxSelect": 1,
            "cascadeDelete": False,
        }
    )
    _patch(pb, "interactions", {"fields": fields})
    log.info("added interactions.supplier pointing at suppliers")


def add_source_key_to_interactions(pb):
    """Add interactions.source_key, a stable name for what the record mirrors.

    Written as source and id, for example `todoist:6Xtask0000000001` or
    `gmail:18f0000000000001`. A sync pass looks the key up before creating
    a record, so running the same pass twice updates the existing record
    instead of adding a duplicate.
    """
    col = _collection(pb, "interactions")
    fields = list(col.get("fields") or [])
    fields.append({"name": "source_key", "type": "text", "required": False})
    _patch(pb, "interactions", {"fields": fields})
    log.info("added interactions.source_key")


def unique_source_key_on_interactions(pb):
    """Add a unique index on interactions.source_key.

    A check in code holds only while every caller remembers it; the index
    makes the database refuse a duplicate even when the code is wrong.

    The index skips blank keys (`WHERE source_key != ''`), because records
    written before the field existed have no key, and a plain unique index
    would treat all of those as duplicates of each other and fail to build.
    """
    col = _collection(pb, "interactions")
    indexes = list(col.get("indexes") or [])
    indexes.append(
        "CREATE UNIQUE INDEX `idx_interactions_source_key` "
        "ON `interactions` (`source_key`) WHERE `source_key` != ''"
    )
    _patch(pb, "interactions", {"indexes": indexes})
    log.info("added the unique index on interactions.source_key")


def add_supplier_to_assignments(pb):
    """Add assignments.supplier, a relation to the suppliers collection.

    A task such as "chase the freight forwarder for a quote" belongs to a
    supplier, not to a client. Without this field it could only be left
    unlinked, and unlinked tasks drop out of the per-client views. It comes
    before any rule that requires a link, so supplier tasks never have to
    be pointed at an unrelated client to pass it.
    """
    suppliers = _collection(pb, "suppliers")
    col = _collection(pb, "assignments")
    fields = list(col.get("fields") or [])
    fields.append(
        {
            "name": "supplier",
            "type": "relation",
            "required": False,
            "collectionId": suppliers["id"],
            "maxSelect": 1,
            "cascadeDelete": False,
        }
    )
    _patch(pb, "assignments", {"fields": fields})
    log.info("added assignments.supplier pointing at suppliers")


def add_site_address_to_jobs(pb):
    """Add jobs.site_address, the address of the plant a job is at.

    The dashboard's project screen writes this field (the address box and
    its "Use client" button). PocketBase accepts a write to an unknown
    field with HTTP 200 and silently drops the value, so without the field
    the address never saves and the "No address set" warning never clears.

    The map reads job.site_address first and falls back to the client's
    address, so two jobs at different plants of one client get their own
    pins.
    """
    col = _collection(pb, "jobs")
    fields = list(col.get("fields") or [])
    fields.append({"name": "site_address", "type": "text", "required": False})
    _patch(pb, "jobs", {"fields": fields})
    log.info("added jobs.site_address")


def add_real_due_to_assignments(pb):
    """Add assignments.real_due, the date a task was originally due.

    assignments.due_date is not reliable for that: the overdue engine
    "parks" every late task on yesterday's date, so due_date always shows a
    late task as about one day late, however late it really is.

    The real due date is recorded once and then treated as read only. It
    is kept in two places: here, and in a pinned line of the Todoist task
    description (see core/realdue.py). The description is the copy a person
    can overwrite by accident, so when the two disagree this field wins.
    """
    col = _collection(pb, "assignments")
    fields = list(col.get("fields") or [])
    fields.append({"name": "real_due", "type": "date", "required": False})
    _patch(pb, "assignments", {"fields": fields})
    log.info("added assignments.real_due")


def add_on_complete_to_assignments(pb):
    """Add assignments.on_complete_job_status, the job status a task sets.

    When a task carrying a value is completed, the trigger engine
    (daemon/triggers.py) moves the task's job to that status, so ticking
    off "Send quote" moves the job from quoting to quoted. Empty, the normal
    case, changes nothing. Only the four forward moves listed are allowed,
    so a trigger cannot put a job into an unexpected status.
    """
    col = _collection(pb, "assignments")
    fields = list(col.get("fields") or [])
    fields.append({
        "name": "on_complete_job_status",
        "type": "select",
        "required": False,
        "maxSelect": 1,
        "values": ["quoted", "won", "invoicing", "invoiced"],
    })
    _patch(pb, "assignments", {"fields": fields})
    log.info("added assignments.on_complete_job_status")


def add_auto_kind_to_assignments(pb):
    """Add assignments.auto_kind, which marks tasks the engine created.

    The automation never completes or deletes a task, with one exception:
    a chase task the trigger engine created itself, on a job that has since
    moved on, is closed. Only a task carrying an auto_kind can be closed
    this way, so a task the operator wrote with the same title is never
    touched.
    """
    col = _collection(pb, "assignments")
    fields = list(col.get("fields") or [])
    fields.append({
        "name": "auto_kind",
        "type": "select",
        "required": False,
        "maxSelect": 1,
        "values": ["quoted_chase"],
    })
    _patch(pb, "assignments", {"fields": fields})
    log.info("added assignments.auto_kind")


def add_status_changed_at_to_jobs(pb):
    """Add jobs.status_changed_at, the time the status last changed.

    The seven-day chase clock needs a start. `created` is when the job was
    opened, which can be months before the quote went out, and `updated`
    moves on every edit, so a quote sent three weeks ago and corrected
    yesterday would look one day old. Whatever changes the status writes
    this field at the same time.
    """
    col = _collection(pb, "jobs")
    fields = list(col.get("fields") or [])
    fields.append({"name": "status_changed_at", "type": "date", "required": False})
    _patch(pb, "jobs", {"fields": fields})
    log.info("added jobs.status_changed_at")


def add_status_changed_by_to_jobs(pb):
    """Add jobs.status_changed_by: what changed the status.

    One of `task:<todoist id>`, `mcp`, `dashboard` or `manual`. The trigger
    engine writes the id of the task that fired, so the client history can
    say which task moved a job, not only that it moved.
    """
    col = _collection(pb, "jobs")
    fields = list(col.get("fields") or [])
    fields.append({"name": "status_changed_by", "type": "text", "required": False})
    _patch(pb, "jobs", {"fields": fields})
    log.info("added jobs.status_changed_by")


def set_jobs_status_to_eleven(pb):
    """Limit jobs.status to the eleven values in core/job_status.py.

    Those eleven are the statuses the dashboard has a label and a colour
    for. A job saved with any other value shows as a grey "Unknown" chip.
    With the select limited, PocketBase rejects such a write with HTTP 400
    at the moment it happens.

    PocketBase lets a select value be removed while records still hold it,
    and those records keep the now-invalid string. So the migration first
    counts jobs on a status that is not in the list and refuses to run
    while there are any.
    """
    stuck = {}
    for job in pb.list_all("jobs") or []:
        status = (job.get("status") or "").strip()
        if status and status not in JOB_STATUSES:
            stuck[status] = stuck.get(status, 0) + 1
    if stuck:
        raise RuntimeError(
            "%s still on a status that is being removed. Give each of them "
            "one of the statuses in core/job_status.py first (from the "
            "dashboard or the PocketBase admin UI), then run this again"
            % ", ".join("%d job%s on '%s'" % (n, "" if n == 1 else "s", s)
                        for s, n in sorted(stuck.items())))

    col = _collection(pb, "jobs")
    fields = []
    for field in col.get("fields") or []:
        field = dict(field)
        if field.get("name") == "status":
            field["values"] = list(JOB_STATUSES)
        fields.append(field)
    _patch(pb, "jobs", {"fields": fields})
    log.info("jobs.status now offers %d values", len(JOB_STATUSES))


MIGRATIONS = [
    Migration(
        "interactions.supplier",
        "supplier interactions have nowhere to point, so they are orphans forever",
        lambda pb: _has_field(pb, "interactions", "supplier"),
        add_supplier_to_interactions,
    ),
    Migration(
        "interactions.source_key",
        "nothing identifies where an interaction came from, so every pass wrote a new one",
        lambda pb: _has_field(pb, "interactions", "source_key"),
        add_source_key_to_interactions,
    ),
    Migration(
        "interactions.source_key unique index",
        "the database should refuse a duplicate even when the code forgets to check",
        lambda pb: _has_index(pb, "interactions", "idx_interactions_source_key"),
        unique_source_key_on_interactions,
    ),
    Migration(
        "assignments.supplier",
        "a supplier task has nowhere to point, so it sits on the board unlinked",
        lambda pb: _has_field(pb, "assignments", "supplier"),
        add_supplier_to_assignments,
    ),
    Migration(
        "jobs.site_address",
        "the dashboard writes this field on every project save and PocketBase "
        "drops unknown fields, so the address never saves",
        lambda pb: _has_field(pb, "jobs", "site_address"),
        add_site_address_to_jobs,
    ),
    Migration(
        "assignments.real_due",
        "due_date is rewritten every time a late task is parked, so nothing "
        "records how late a task really is",
        lambda pb: _has_field(pb, "assignments", "real_due"),
        add_real_due_to_assignments,
    ),
    Migration(
        "assignments.on_complete_job_status",
        "nothing links finishing a task to the job moving on, so a job stays "
        "in quoting after the quote is sent",
        lambda pb: _has_field(pb, "assignments", "on_complete_job_status"),
        add_on_complete_to_assignments,
    ),
    Migration(
        "assignments.auto_kind",
        "the engine has no way to tell a task it created from one the "
        "operator wrote, so it cannot safely close either of them",
        lambda pb: _has_field(pb, "assignments", "auto_kind"),
        add_auto_kind_to_assignments,
    ),
    Migration(
        "jobs.status_changed_at",
        "no field records when a status last moved, so a chase clock would "
        "have to run off `updated`, which any edit resets",
        lambda pb: _has_field(pb, "jobs", "status_changed_at"),
        add_status_changed_at_to_jobs,
    ),
    Migration(
        "jobs.status_changed_by",
        "a status change leaves no trace of what made it, so the client "
        "history cannot say which task moved the job",
        lambda pb: _has_field(pb, "jobs", "status_changed_by"),
        add_status_changed_by_to_jobs,
    ),
    Migration(
        "jobs.status eleven values",
        "the select offers `paid` and `active`, which no screen in the "
        "dashboard has a label or a colour for, so either one saves as a "
        "grey Unknown chip",
        lambda pb: _select_values(pb, "jobs", "status") == list(JOB_STATUSES),
        set_jobs_status_to_eleven,
    ),
]


def run(pb, write=False):
    """Check every migration and, with write=True, apply the missing ones.

    Returns three lists: names done (or that would be done), names already
    applied, and (name, reason) pairs that failed.
    """
    done, skipped, failed = [], [], []
    for migration in MIGRATIONS:
        try:
            already = migration.is_applied(pb)
        except Exception as exc:
            failed.append((migration.name, "could not check: %s" % exc))
            continue
        if already:
            skipped.append(migration.name)
            continue
        if not write:
            done.append(migration.name)
            continue
        try:
            migration.apply(pb)
            # Check again: PocketBase can accept a change and not keep it.
            if not migration.is_applied(pb):
                failed.append((migration.name, "applied but not there on re-read"))
            else:
                done.append(migration.name)
        except Exception as exc:
            failed.append((migration.name, str(exc)))
    return done, skipped, failed


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    write = "--write" in argv

    conf = settings()
    pb = PocketBaseClient(**conf.pocketbase())
    pb.auth_admin()

    done, skipped, failed = run(pb, write=write)

    for name in skipped:
        print("already done   %s" % name)
    for name in done:
        print("%s   %s" % ("applied      " if write else "would apply  ", name))
    for name, why in failed:
        print("FAILED         %s: %s" % (name, why))

    if not write and done:
        print("")
        print("Nothing was changed. Run again with --write to apply this.")
    if write and done:
        print("")
        print("Now run: python -m tools.introspect_schema --write")
        print("then review and commit the diff to core/schema.py")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
