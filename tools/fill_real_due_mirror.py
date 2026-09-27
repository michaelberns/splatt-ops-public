"""
Copy each task's pinned real due date into the assignments.real_due field.

Usage
    python -m tools.fill_real_due_mirror              report what would change
    python -m tools.fill_real_due_mirror --write      write the missing dates

Background
    A task's "real due date" is the date it was originally due. The overdue
    engine moves late tasks to yesterday (it "parks" them), so the Todoist
    due date no longer says how late a task really is. core/realdue.py
    keeps the real date in two places:

    - the pinned line in the Todoist description ("Real due: 2026-07-12 |
      49 days late"), the copy a person reads, and
    - the real_due field on the PocketBase assignment (the "mirror"), the
      copy a person cannot overwrite by accident, used when the
      description has been edited.

    This tool fills the mirror from the pin wherever the mirror is empty,
    for example after the field was added or after an import.

What it will not do
    It never touches Todoist: not the description, the due date or a label.
    It changes one PocketBase field and nothing else.

    It never overwrites a date already stored. Where the mirror and the pin
    disagree, the disagreement is reported and the stored date is kept,
    because the stored date wins when the two are read, and silently
    changing it here would hide the kind of mismatch core/realdue.py exists
    to detect.

Each run writes its report to real-due-mirror-YYYY-MM-DD.txt (with -dryrun
added for a dry run) in the reports folder, paths.reports in
config/settings.yaml. Read the dry run before using --write.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

from core import realdue
from core.config import settings
from core.logging_setup import setup
from core.pb import PocketBaseClient

log = setup("fill_real_due_mirror")

COLLECTION = realdue.PB_COLLECTION
FIELD = realdue.PB_FIELD


class Row:
    """One assignment, and what should happen to it."""

    def __init__(self, record, pinned, stored, verdict, detail=""):
        self.record_id = record.get("id")
        self.todoist_id = str(record.get("todoist_id") or "")
        self.content = (record.get("content") or "")[:60]
        self.pinned = pinned
        self.stored = stored
        self.verdict = verdict
        self.detail = detail
        self.result = ""

    def to_line(self):
        return "%-10s %-12s %-12s %-24s %s" % (
            self.verdict,
            self.pinned.date.isoformat() if self.pinned else "-",
            self.stored or "-",
            self.result or "",
            self.content,
        )


def _stored_text(record):
    value = record.get(FIELD)
    return "" if value is None else str(value).strip()


def plan(records):
    """Decide what to do with each assignment. No writes, no network.

    One Row per record, with one of four verdicts:

      fill      pinned, mirror empty: --write fills it
      agrees    pinned, mirror already holds the same date
      disagrees pinned, mirror holds a different date: reported, kept
      no pin    nothing to mirror, which is not a problem on its own
    """
    rows = []
    for record in records:
        description = record.get("description") or ""
        pinned = realdue.parse(description)
        stored = _stored_text(record)

        if not pinned:
            rows.append(Row(record, None, stored, "no pin"))
            continue
        if not stored:
            rows.append(Row(record, pinned, stored, "fill"))
            continue

        clash = realdue.mismatch(description, stored)
        if clash:
            rows.append(Row(
                record, pinned, stored, "disagrees",
                "pin says %s, PocketBase holds %s, PocketBase wins"
                % (clash["pinned"], clash["pocketbase"]),
            ))
        else:
            rows.append(Row(record, pinned, stored, "agrees"))
    return rows


def apply_row(pb, row):
    """Write the pinned date into one record's real_due field.

    Returns "written", or "FAILED: <reason>" (the error is logged, not
    raised, so one failure does not stop the run).
    """
    try:
        pb.update(COLLECTION, row.record_id,
                  {FIELD: row.pinned.date.isoformat()})
    except Exception as exc:
        log.error("could not write %s: %s", row.record_id, exc)
        return "FAILED: %s" % exc
    return "written"


def report(rows, today, write):
    counts = {}
    for row in rows:
        counts[row.verdict] = counts.get(row.verdict, 0) + 1

    lines = [
        "Real due date mirror, %s" % today.isoformat(),
        "%s" % ("APPLIED" if write else "DRY RUN, nothing was changed"),
        "",
        "%d assignments." % len(rows),
        "  %-10s %d" % ("fill", counts.get("fill", 0)),
        "  %-10s %d" % ("agrees", counts.get("agrees", 0)),
        "  %-10s %d" % ("disagrees", counts.get("disagrees", 0)),
        "  %-10s %d" % ("no pin", counts.get("no pin", 0)),
        "",
    ]

    disagreements = [row for row in rows if row.verdict == "disagrees"]
    if disagreements:
        lines.append("Disagreements, left alone on purpose:")
        for row in disagreements:
            lines.append("  %s %s" % (row.content, row.detail))
        lines.append("")

    lines.append("verdict    pinned       stored       result"
                 "                   task")
    lines.append("-" * 100)
    for row in sorted(rows, key=lambda r: (r.verdict, r.content)):
        if row.verdict == "no pin" and not write:
            continue
        lines.append(row.to_line())
    lines.append("")
    lines.append("Tasks with no pin are not listed. They are not a problem "
                 "on their own,")
    lines.append("and real_due_is_pinned is the check that judges them.")
    return "\n".join(lines)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    write = "--write" in argv
    today = datetime.now(timezone.utc).date()

    conf = settings()
    path = conf.path("paths.reports") / (
        "real-due-mirror-%s%s.txt"
        % (today.isoformat(), "" if write else "-dryrun")
    )
    path.parent.mkdir(parents=True, exist_ok=True)

    pb = PocketBaseClient(**conf.pocketbase())
    if FIELD not in set(pb.field_names(COLLECTION) or []):
        print("The %s collection has no %s field." % (COLLECTION, FIELD))
        print("Add it before running this. Nothing was changed.")
        return 2

    records = pb.list_all(COLLECTION)
    rows = plan(records)
    todo = [row for row in rows if row.verdict == "fill"]

    if write and todo:
        print("Writing %d assignments." % len(todo), flush=True)
        for number, row in enumerate(todo, start=1):
            row.result = apply_row(pb, row)
            print("  [%3d/%d] %s %s"
                  % (number, len(todo), row.result, row.content), flush=True)
        print("", flush=True)

    text = report(rows, today, write)
    path.write_text(text, encoding="utf-8")
    print(text)
    print("")
    print("Written to %s" % path)
    if not write:
        print("")
        print("Nothing was changed. Read the report, then run again with "
              "--write.")
    return 1 if any(row.result.startswith("FAILED") for row in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
