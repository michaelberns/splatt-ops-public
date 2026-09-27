---
name: splatt-todoist
description: Manage Splatt Engineering tasks in Todoist, the board the whole system runs on. Use this skill whenever the operator asks about Splatt work, tasks, quotes, client follow-ups, supplier coordination, or any Todoist operation for Splatt. Also use it when cross-referencing Gmail with Todoist, triaging late tasks, reorganising the Splatt board, scheduling work, or doing a daily or weekly review. Even a simple request such as "check my Splatt tasks" should use it: it holds the task description formats the daemon and the validator read, the seven board sections, and the scheduling rules.
---

# Splatt Todoist

All Splatt work lives in one Todoist project, called the board. The daemon mirrors the board into PocketBase and back, the overdue engine moves late tasks between sections, and the validator checks the board against PocketBase. All three read the formats in this skill, so follow them exactly.

## Where the ids live

The project id and the seven section ids are stored in `config/settings.yaml` and nowhere else:

```yaml
todoist:
  project_id: ...
  sections:
    today: ...
    overdue: ...
    upcoming: ...
    waiting_client: ...
    waiting_supplier: ...
    stalled: ...
    backlog: ...
```

Read them from that file every time you need one. Never copy an id into a skill, a note or a task description. On a new install `tools/todoist_setup.py` creates the project and sections and prints the ids to paste into the file.

## The board

| Section | What goes there | Who moves tasks in |
|---|---|---|
| `today` | Work due today | The agent, when planning the day |
| `overdue` | The operator's own work past its real due date | The overdue engine |
| `upcoming` | Work with a future date | The agent |
| `waiting_client` | Waiting on a client (a reply, a PO, a payment) | The agent or the engine |
| `waiting_supplier` | Waiting on a supplier (a quote, a lead time, a shipment) | The agent or the engine |
| `stalled` | Work 28 days past its real due date, or chased twice with no reply. Has no due date | The overdue engine only |
| `backlog` | Undated work with no commitment behind it | The agent |

How the overdue engine treats a late task (`python3 -m daemon sweep`, day counts from `overdue` in `config/settings.yaml`):

- Day 1: moved to `overdue` and parked on yesterday with no time.
- Day 3: tagged `@escalated`.
- Day 7: an alert on the ops Telegram channel.
- Day 14: tagged `@stall-warning`. It stays where it is.
- Day 28: moved to `stalled` and its due date cleared.

A waiting task never climbs that ladder. It gets a chase date instead: 5 business days for a client, 3 for a supplier, and after two chases with no reply it goes to `stalled`. The engine decides a task is waiting from its wording, so write it plainly: "Waiting on Kyle for the PO", "Waiting on Taponera for the lead time".

The engine never completes or deletes a task. Anything it will not decide goes on its `needs_decision` list for the operator.

## The description header

The first lines of a task description form its header. The daemon reads only these lines, in any order, and stops at the first line that is not one of them:

| Line | Meaning | Who writes it |
|---|---|---|
| `📌 Real due: YYYY-MM-DD` | The date the work was really due | The daemon, once |
| `⏱ Est: 15m` | Time estimate (15m or 45m) | The agent |
| `📁 Project: SS Folders/<Client>/<Project folder>/PROJECT.md` | The project the task belongs to | The agent |
| `🔁 On complete: job to <status>` | Finishing the task moves the job | The agent |
| `🔁 Auto: quoted_chase for job <id>` | A chase task the daemon created | The daemon |

Everything after the header is free text: context, dated updates, email references.

**Rule: never write above the pin, never write a second pin, never drop it.** The `📌 Real due:` line is the record of when the work was really due. Late work is parked on yesterday, so the date Todoist shows is always about one day old; the pin is what keeps a months-late task from reading as one day late. `core/realdue.py` reads it and the validator rules `real_due_is_pinned` and `real_due_not_duplicated` check it. When you edit a description that has a pin, put new header lines below it and carry the pin across unchanged.

**Rule: do not invent header lines.** A line the daemon does not recognise ends the header early, and everything below it, the pin included, is no longer read.

## Adding a task

When the operator says "add a task", "track this", or describes something that needs doing:

1. **Pick the section** from the table above: due today → `today`; a future date → `upcoming`; waiting on someone → `waiting_client` or `waiting_supplier`; no date and no commitment → `backlog`. Get its id from `config/settings.yaml`.
2. **Resolve the project.** Every task carries a project line. The daemon links the task to its job from it, and the Workload page groups by it.
   - Look at the client's folders under SS Folders and the client's live jobs (`search_jobs` in PocketBase).
   - Exactly one project fits: use it.
   - Two or more fit, or none fit but it is clearly client work: ask the operator. Offer the client's live projects, "New project: <suggested name>" and "Internal, no project". Never pick the only project just because it is the only one.
   - New project chosen: create the folder and `PROJECT.md` first (`splatt-project-files`), then the task.
   - Internal or admin work: `📁 Project: internal, no project`.
3. **Add a trigger line if finishing the task moves the job on.** Only four moves exist:

   | The task | The line |
   |---|---|
   | Send the quote | `🔁 On complete: job to quoted` |
   | The PO or order is confirmed | `🔁 On complete: job to won` |
   | Raise the invoice | `🔁 On complete: job to invoicing` |
   | Send the invoice | `🔁 On complete: job to invoiced` |

   When the task is completed, the trigger pass (`python3 -m daemon triggers --write`) moves the job and writes a line in the client history saying which task did it. The job must be linked through the project line, or the trigger can never fire (the validator rule `trigger_tasks_link_a_job` reports this). Tasks that chase, check, book or read do not move a status and get no trigger line.
4. **Estimate and schedule it** (see "Scheduling").
5. **Create it** with the Todoist add-tasks tool:

   ```json
   {
     "tasks": [{
       "content": "Moana Dairy / Ned — Send the service quote",
       "description": "⏱ Est: 45m\n📁 Project: SS Folders/Moana Dairy/Vela 12-12-1 Service/PROJECT.md\n🔁 On complete: job to quoted\n\nNed asked on 2 Sep for a price on the annual filler service. Parts list is in the project file.",
       "projectId": "<todoist.project_id>",
       "sectionId": "<todoist.sections.upcoming>",
       "priority": "p2",
       "dueString": "Sep 12 2026 at 9am",
       "duration": "45m",
       "labels": ["quote"]
     }]
   }
   ```

**Title format:** `Company / Contact — Action verb + object`. Examples:

- `Orchard Lane / Dan — Chase capper change parts delivery`
- `Clearwater Bottling / Nigel — Email freight options`
- `Tui Valley / Kyle — Order spare blower bearing cartridge`

The company name comes first because the daemon matches tasks to clients from the start of the title.

**Priority** is a colour. The agent sets it on a new task (p1 urgent or a contractual deadline, p2 this week, p3 needs attention, p4 backlog or waiting); after that the engine repaints it when it moves the task. No rule in the system reads priority to make a decision.

**Labels** in use: `quote`, `email`, `follow-up`, `waiting`, `urgent`, `client-promise` (a chase that guards a promise in a project file). `escalated` and `stall-warning` belong to the overdue engine; do not add or remove them by hand.

## Rules for task lifecycles

- **Never create a chase task by hand for a quoted job.** Seven days after a job moves to `quoted`, the daemon writes one chase task, books it into a free slot and links it to the job. A second, hand-made chase would run on a different clock.
- **Never complete an automatic chase by hand.** A task with `🔁 Auto: quoted_chase` belongs to the daemon. If the job has moved on, change the job status; the daemon then closes the chase with a comment saying why.
- **Never change a job status by hand when a task with a matching trigger exists.** Complete the task instead, so the change is stamped `task:<todoist id>`.
- **Never delete a task.** Complete it, so the history stays.
- **Split a task that holds two scopes of work** (for example filler parts and blower parts for the same client) into two tasks with the same `Company / Contact` prefix and their own project lines. Ask first if you are not sure it is two scopes, and copy the dated history into both.

## Triage

When the operator says "triage", "sort my tasks", "what's overdue" or asks for a daily or weekly review:

1. Fetch every task on the board (`projectId` from `config/settings.yaml`, `responsibleUserFiltering: all`, `limit: 100`).
2. Fetch overdue tasks from all projects (`filter: overdue`), since the operator's other projects also fill the calendar.
3. For each task, check the section against the board table. Leave `overdue` and `stalled` placement to the engine; fix only what the engine does not own (a dated task sitting in `backlog`, a waiting task sitting in `today`).
4. Move tasks by `sectionId` alone. Do not pass `projectId` and `sectionId` together in one update.
5. Show the result as a board: one column per section, cards with the title, contact, due date and a `waiting` badge where it applies.

## Cross-referencing Gmail

When the operator says "check emails", "anything missing" or "work review":

1. Fetch the board as in Triage.
2. Search sent mail: `from:<operator address> after:YYYY/MM/DD`. Search the inbox: `to:<operator address> after:YYYY/MM/DD is:unread`. Gmail dates use slashes (`after:2026/9/1 before:2026/9/3`).
3. Read each actionable message in full by its message id. Snippets are too short.
4. For each thread with no task, offer to create one. For each task the thread updates, append dated context.

## Removing duplicates

When the operator says "clean up duplicates" or "double ups":

1. Fetch the board, and any other project the operator names.
2. Match tasks with the same company and a similar action, or where one supersedes the other.
3. Complete the less detailed copy, after copying anything unique from it into the one that stays. Never delete.

## Updating a task after an event

When the operator says "I emailed them", "they replied" or describes a change:

1. Find the task (`searchText` with the client name, `responsibleUserFiltering: all`).
2. Append a dated line below the header, for example `Sep 05 update: Nigel confirmed the order, waiting on the delivery date.` Keep everything already there.
3. Now waiting on someone: move it to `waiting_client` or `waiting_supplier` and say who it is waiting on.
4. Finished: complete it. If it carries a trigger line, the job moves on its own.

## Scheduling

Todoist is the calendar: its Upcoming view is the operator's schedule. Every task you create or reschedule gets an estimate and a real slot.

**Estimates.** `⏱ Est: 15m` for quick actions (a short reply, a status check, a lookup). `⏱ Est: 45m` for bigger ones (quote preparation, reconciling systems, a technical write-up, coordinating several suppliers). If unsure, use 45m. Also set the task's `duration` to match.

**Finding a free slot:**

1. Pull every task scheduled on the target day, from all projects.
2. Sort by time and read each one's `⏱ Est:` (15 minutes if it has none).
3. Find the gaps inside working hours, 08:00 to 17:00 NZ time (`planner.working_hours` in `config/settings.yaml`).
4. Take the first gap that fits the task plus 15 minutes of buffer either side where possible.
5. Set both date and time, for example `"tomorrow at 14:15"`.

**Rules:**

- Never put two tasks at the same time unless the operator asks for it.
- Never put a 45 minute task into a smaller gap.
- Never change an existing due time without asking.
- Never schedule outside working hours without permission.
- If today has no gap, say so and offer the earliest free slot: "No free 45 minute slot today. The earliest is Tuesday at 10:30. Book it there, or move something from today?"
- Only Todoist is the calendar. Do not read or write any other calendar.

## Tool notes

- `find-tasks` returns only unassigned tasks and the operator's own unless you pass `responsibleUserFiltering: all`.
- `find-tasks-by-date` needs `overdueOption: include-overdue` to include late work.
- Change a date with `reschedule-tasks`. `update-tasks` replaces the whole due string, which destroys recurrence.
- Some versions of `update-tasks` expect `dueString` and `deadlineDate` even when you change other fields; pass the current values to keep them.
- A 401 from Todoist means the connector needs signing in again. Tell the operator.
