"""
The cleanup: find open Todoist tasks that the mail shows are already done,
and tick them off.

Usage
    python -m tools.tick_off                  report what it found, change nothing
    python -m tools.tick_off --write          tick off the tasks that are proven done
    python -m tools.tick_off --all            also list tasks with nothing to match on
    python -m tools.tick_off --evidence FILE  read the mail from FILE

It runs only when asked (by the operator, or by the agent on request). It
is not on the daemon loop or a timer, because completing a task takes work
off the board and should happen only when someone asked for it.

Where the mail comes from
    Not from this repo, which deliberately has no mailbox access: a
    background process that can both read mail and close tasks would be a
    lot of unattended authority. The agent, which already has mail access,
    fetches the mail and writes it to state/evidence.json:

    {
      "fetched_at": "2026-08-30T09:00:00Z",
      "since": "2026-06-01",
      "emails": [
        {"id": "18f0000000000001", "direction": "sent", "date": "2026-08-21",
         "from": "kyle@clearwaterbottling.example.co.nz",
         "subject": "Re: QU-7994 gripper assembly",
         "snippet": "approved, please proceed"}
      ]
    }

    A file whose fetched_at is more than STALE_HOURS old is refused.

What it will tick off
    Only a task whose quote, invoice or PO number (for example QU-7994)
    appears in an email going the right way for what the task was waiting
    on. core/evidence.py holds those rules and the reasons it refuses.
    Everything else is listed with whatever was found, and left alone, so
    the run shortens the operator's review rather than guessing.

What it will not do
    It never deletes and never reopens a task, and never closes one it
    cannot name a specific email for. The email used is printed next to
    every completion. Before completing a task it appends the proof to the
    task description, so the reason stays with the task in Todoist.

Each run writes its report to cleanup-YYYY-MM-DD.txt (cleanup-...-dryrun.txt
for a dry run) in the reports folder, paths.reports in config/settings.yaml
(by default ~/Documents/Splatt/reports). Every completion is also recorded
in the write ledger.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from core import evidence as evidence_mod
from core import realdue
from core.config import settings
from core.ledger import TODOIST_COLLECTION, WriteLedger, ledger_path
from core.logging_setup import setup
from core.todoist import TodoistClient
from daemon.waiting import WaitingRouter

log = setup("tick_off")

#: Where the SS agent drops the mail it fetched.
DEFAULT_EVIDENCE = Path("state") / "evidence.json"

#: An evidence file older than this is refused. An old file would miss
#: exactly the tasks finished since it was fetched, while the report looked
#: complete.
STALE_HOURS = 24


class EvidenceError(ValueError):
    """The evidence file is missing, malformed or too old to trust."""


def load_evidence(path, now=None):
    """The emails in an evidence file, or EvidenceError saying how to fix it.

    Accepts either a bare list of emails or an object with an "emails" key.
    A file with no fetched_at (a hand-written one) is read without an age
    check.
    """
    path = Path(path)
    if not path.exists():
        raise EvidenceError(
            "no evidence file at %s. Nothing can be ticked off without mail "
            "to check against. Ask the SS agent to run the cleanup, which "
            "fetches the mail and writes this file." % path
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EvidenceError("could not read %s: %s" % (path, exc))

    rows = data.get("emails") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise EvidenceError(
            "%s does not hold a list of emails. Expected either a list or an "
            "object with an 'emails' key." % path
        )

    age_hours = _age_hours(data, now)
    if age_hours is not None and age_hours > STALE_HOURS:
        raise EvidenceError(
            "%s was fetched %d hours ago, which is too old to close tasks "
            "against. Fetch the mail again." % (path, age_hours)
        )

    return load_evidence_rows(rows)


def load_evidence_rows(rows):
    """The emails, from a list of dicts already in memory.

    Separate from the file reading so tests, and callers that fetched the
    mail themselves, do not need to write a file first.
    """
    return [evidence_mod.Email.from_dict(row) for row in rows or []]


def _age_hours(data, now=None):
    if not isinstance(data, dict):
        return None
    stamp = str(data.get("fetched_at") or "").strip()
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return int((now - when).total_seconds() // 3600)


class Row:
    """One task and what the mail said about it: a line in the report."""

    def __init__(self, task, finding, age):
        self.task_id = str(task.get("id"))
        self.content = (task.get("content") or "")[:70]
        self.finding = finding
        self.age = age
        self.shared = 0
        self.result = ""

    @property
    def verdict(self):
        return self.finding.verdict

    def to_lines(self):
        lines = ["%-8s %4dd  %s" % (self.verdict, self.age, self.content)]
        lines.append("             %s" % self.finding.why)
        if self.finding.email:
            lines.append("             proof: %s" % self.finding.proof())
        if self.shared:
            lines.append(
                "             careful: %s is also on %d other open task%s, so "
                "this email may not be about this one"
                % (self.finding.reference, self.shared,
                   "" if self.shared == 1 else "s")
            )
        return lines


def plan(tasks, emails, today, router):
    """What the mail says about every open task. No writes, no network."""
    rows = []
    for task in tasks:
        real = realdue.resolve(task, today=today)
        finding = evidence_mod.look(
            task,
            emails,
            waiting_kind=router.classify(task),
            money_owed=router.is_money_owed(task),
            real_due=real.date,
        )
        rows.append(Row(task, finding, real.age(today)))
    _flag_shared_references(tasks, rows)
    return rows


def _flag_shared_references(tasks, rows):
    """Flag rows whose reference number is on other open tasks too.

    One quote usually has several tasks (send it, chase it, invoice it), so
    a reply quoting QU-7994 may close one of them and not the others, and
    the tool cannot tell which. This is not a reason to refuse, because the
    direction rule usually picks the right task, but the report prints a
    "careful" line so the operator can see the doubt and overrule it.
    """
    counts = {}
    for task in tasks:
        for ref in evidence_mod.references("%s\n%s" % (
            task.get("content") or "", task.get("description") or ""
        )):
            counts[ref] = counts.get(ref, 0) + 1
    for row in rows:
        ref = row.finding.reference
        if ref:
            row.shared = max(counts.get(ref, 1) - 1, 0)


def proof_note(row, today):
    return "%s: ticked off by the cleanup. %s. Proof: %s" % (
        today.isoformat(), row.finding.why, row.finding.proof()
    )


def apply_row(todoist, task, row, today, ledger=None):
    """Close one task, writing the reason into its description first.

    The note is added before completing because a completed task can no
    longer be updated, and a completion with no recorded reason cannot be
    checked later. The completion is also recorded in the write ledger.
    """
    description = "%s\n%s" % (
        task.get("description") or "", proof_note(row, today)
    )
    todoist.update_task(row.task_id, description=description.strip())
    todoist.complete_task(row.task_id)
    if ledger:
        ledger.record(
            "update", TODOIST_COLLECTION, row.task_id, ["description", "completed"],
            "tick_off", row.finding.why,
        )
    return "ticked off"


def report(rows, today, write, show_all=False):
    counts = {}
    for row in rows:
        counts[row.verdict] = counts.get(row.verdict, 0) + 1

    done = [r for r in rows if r.verdict == evidence_mod.DONE]
    chased = [r for r in rows if r.verdict == evidence_mod.CHASED]
    maybe = [r for r in rows if r.verdict == evidence_mod.MAYBE]
    unknown = [r for r in rows if r.verdict == evidence_mod.UNKNOWN]

    lines = [
        "Cleanup, %s" % today.isoformat(),
        "%s" % ("APPLIED" if write else "DRY RUN, nothing was changed"),
        "",
        "%d open tasks checked against the mail." % len(rows),
        "  %d proven done" % len(done),
        "  %d chased, waiting on a reply" % len(chased),
        "  %d worth a look" % len(maybe),
        "  %d nothing found" % len(unknown),
        "",
    ]

    def block(title, group):
        lines.append(title)
        lines.append("-" * 78)
        if not group:
            lines.append("  none")
        for row in sorted(group, key=lambda r: r.age, reverse=True):
            lines.extend("  " + line for line in row.to_lines())
        lines.append("")

    block("Done. These are what --write closes." if not write
          else "Done. These were closed.", done)
    block("Chased. Somebody owes you a reply, so these stay open.", chased)
    block("Worth a look. Your call, nothing was changed.", maybe)
    if show_all:
        block("Nothing found. No reference number to match on.", unknown)
    else:
        lines.append("%d tasks had nothing to match on. Run with --all to "
                     "list them." % len(unknown))
    return "\n".join(lines)


def report_path(conf, today, write):
    """Where this run's report is written. main() resolves it before any
    network call, so a bad path fails before any task is closed rather than
    after, when there would be no record of what was closed."""
    return conf.path("paths.reports") / (
        "cleanup-%s%s.txt" % (today.isoformat(), "" if write else "-dryrun")
    )


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    write = "--write" in argv
    show_all = "--all" in argv
    today = datetime.now(timezone.utc).date()

    conf = settings()
    path = report_path(conf, today, write)
    path.parent.mkdir(parents=True, exist_ok=True)

    evidence_file = _arg(argv, "--evidence") or (
        conf.repo_root / DEFAULT_EVIDENCE
    )
    try:
        emails = load_evidence(evidence_file)
    except EvidenceError as exc:
        print(str(exc))
        return 2

    todoist = TodoistClient(**conf.todoist())
    router = WaitingRouter(conf)

    tasks = todoist.tasks()
    rows = plan(tasks, emails, today, router)

    ledger = WriteLedger(ledger_path(conf)) if write else None
    failures = []
    if write:
        by_id = {str(t.get("id")): t for t in tasks}
        for row in rows:
            if not row.finding.actionable:
                continue
            try:
                row.result = apply_row(
                    todoist, by_id[row.task_id], row, today, ledger
                )
            except Exception as exc:
                row.result = "FAILED"
                failures.append((row.task_id, row.content, str(exc)))
                log.error("tick off failed on %s: %s", row.task_id, exc)

    text = report(rows, today, write, show_all)
    if failures:
        text += "\n\n%d tasks failed to close:\n" % len(failures)
        for task_id, content, why in failures:
            text += "  %s %s: %s\n" % (task_id, content, why)

    path.write_text(text, encoding="utf-8")
    print(text)
    print("")
    print("Written to %s" % path)
    if not write:
        print("")
        print("Nothing was changed. Read the list, then run again with --write.")
    return 1 if failures else 0


def _arg(argv, name):
    """The value after a flag, for the two flags that take one."""
    if name in argv:
        index = argv.index(name)
        if index + 1 < len(argv):
            return argv[index + 1]
    for item in argv:
        if item.startswith(name + "="):
            return item.split("=", 1)[1]
    return None


if __name__ == "__main__":
    raise SystemExit(main())
