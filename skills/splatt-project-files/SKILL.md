---
name: splatt-project-files
description: Manage the Splatt Engineering project files, the PROJECT.md (and its PROJECT-overview.html companion) kept for every client project under SS Folders. Use this skill whenever a project file is created, updated or read, and whenever an email, task or quote during an ops or morning run belongs to a project. Trigger when the operator says "update the project file", "check the project log", "create a project for [client]", "what's the status of [project]", or mentions project documentation, project folders or SS Folders. During a run, use it to decide whether a project file needs an update. Project files are only ever changed through the update-project-md and update-project-overview-html playbooks.
---

# Splatt project files

Every client project has a folder under SS Folders holding a `PROJECT.md` (the living record: contacts, quotes, orders, a communications log, payments, decisions, open items, timeline) and a `PROJECT-overview.html` (a visual page built from the same content, which is what the operator usually reads).

SS Folders is the operator's document folder. Its location comes from `config/settings.yaml` (`paths.root` plus `paths.ss_folders`, by default `~/Documents/Splatt/SS Folders`, or `SPLATT_ROOT` if set). Playbooks take paths relative to the SS Folders root, for example `Clearwater Bottling/Filler Service 2026`.

## Rule: project files change only through the playbook chain

The only way to change `PROJECT.md` or `PROJECT-overview.html` is the paired playbook chain, driven through the `splatt-playbook` MCP tools:

1. `update-project-md`, then
2. `update-project-overview-html`, straight after.

Editing either file outside a playbook run is not allowed, including "tiny" changes such as one log row or a status badge. Why: each run records a `run_id` and the playbook server checks the file really changed after the run started, so every change has an audit trail and the end-of-cycle checks can prove both halves happened.

**No half chains.** If the Markdown run completes but the HTML run does not, the two files disagree and the operator reads the stale one. If you cannot do the HTML update, do not start the Markdown update; report the blocker first.

### Driving the chain

1. `playbook__start` with `playbook_id: "update-project-md"` and inputs:
   - `project_path`: the project folder relative to the SS Folders root,
   - `update_type`: one of `comms_log_only`, `new_quote`, `supplier_order_change`, `key_decision`, `document_added`, `status_change`,
   - `update_summary`: one line saying what changed.
2. Walk the steps with `playbook__step`, in order. The step that applies the edits is where you read `PROJECT.md` and append the new rows with the file tools. That step is the only place the file tools touch the file.
3. On an HTTP 400, read `failed_validators`, fix the real problem (wrong path, edit not saved, edit made before the run started) and resubmit the same `step_id`. Never change the evidence to satisfy a validator, and never use `skip` on a step that is not marked skippable.
4. When the Markdown run completes, start `update-project-overview-html` with the same project and the Markdown run's section changes, and walk it the same way.
5. If a step cannot be made to pass this cycle, submit it with `status: fail` to close the run and report it under Decisions Needed. Never leave a run open: the validator rule `no_dangling_playbook_runs` blocks the cycle.

### When the playbook server is down

If `playbook__health` reports the server as down, stop. Do not edit the file by hand as a workaround. Tell the operator the project file update is blocked, log the run as `partial`, and leave the change for the next cycle.

## Templates and playbook specs

Templates and playbook specs live in SS Folders under `_howtotasks/`:

| Resource | Purpose |
|---|---|
| `_howtotasks/templates/PROJECT.md.template` | The skeleton for a new `PROJECT.md` (also in `references/TEMPLATE.md` in this skill) |
| `_howtotasks/templates/PROJECT-overview.html.template` | The skeleton for the HTML companion |
| `_howtotasks/playbooks/*.spec.json` | The playbook specs the server runs. `playbook__specs` lists them |

The repo's `examples/SS Folders/` holds a small fictional SS Folders tree with example specs.

## What is a project

**A project** (it gets a `PROJECT.md`):

- a quote request from a client, accepted or not,
- a confirmed order or job,
- a supply job (sourcing and delivering parts or equipment),
- an installation or commissioning job,
- a repair or remediation scope,
- a capital equipment proposal,
- an ongoing parts supply arrangement with a defined scope,
- any work where money is quoted, invoiced or paid for a deliverable.

**Not a project:**

- relationship emails (introductions, catch-ups, greetings),
- invoice queries with no job behind them,
- supplier price list updates not tied to a client job,
- internal coordination not tied to a client deliverable,
- one-off information requests,
- business admin (insurance, compliance).

**Ask the operator** when it is a grey area: an enquiry that might become a quote, a recurring supply whose scope has changed a lot, or work where Splatt subcontracts to another company. During a run, flag it: "This looks like it could be a project. Create a file for it?"

## Folder structure

```
SS Folders/
├── <Client>/
│   ├── <Project>/
│   │   ├── PROJECT.md
│   │   ├── PROJECT-overview.html
│   │   └── attachments and documents
│   └── <Another project>/
└── <Another client>/
```

- Client folder: the company's everyday name, for example `Clearwater Bottling` or `Orchard Lane`.
- Project folder: a short name for the scope, for example `Filler Service 2026` or `Capper Change Parts`.
- The file is always called `PROJECT.md`.
- A client with one project still gets the client and project levels.

## Creating a new project file

Create one when all three are true: the work is a project, no project file exists for this scope yet (check the client's folder), and you know at least the client, a description and one contact.

1. Run the `onboard-new-client` playbook for a new client; for a new project under an existing client, create the project folder.
2. Fill in `PROJECT.md` from the template. Empty sections say `*No entries yet.*`.
3. Add a first Communications Log row: `| YYYY-MM-DD | — | System | Project file created | Set up from <source> |`.
4. Create the HTML companion from its template.
5. Create any Todoist task for the project with its `📁 Project:` line pointing at the new file.

### Promoting a PocketBase task group

When a task group in PocketBase is promoted to a project, start the file from the group's history:

1. `get_task_group` for the name, client, description and Todoist task ids.
2. Read each of those Todoist tasks for their content and dated updates.
3. `search_interactions` for the client, for Communications Log rows.
4. Create the `PROJECT.md` with the Overview, Communications Log and Timeline filled from that history.
5. `update_task_group` with `project_path` set to the new file.

## When to update a project file

Run the chain when any of these happens on a project:

- a new email in a project thread (Communications Log),
- a quote sent, revised, accepted or declined (Quotes, and the status badge),
- a supplier order placed or confirmed, or a delivery date set or changed (Supplier Orders, Timeline),
- an invoice sent or a payment received (Payments and Invoicing),
- a status change (the badge in the header),
- a key decision by the operator or the client (Key Decisions),
- a new document received or created (Documents),
- a new person involved (Contacts).

Do not update for trivial emails ("thanks", "got it").

When you edit: read the file first, append new rows at the bottom of each table (the Communications Log is in date order), update the header badge and Last Updated date, and never rewrite or remove an existing row.

## Finding the right project file

1. Match the client name to a folder under SS Folders. PocketBase `search_clients` resolves aliases and email domains.
2. Match the subject or scope to a project folder.
3. No match: decide whether it is a new project (see above).
4. Client exists but the project does not: it may be a new scope for that client.
5. Still unsure: skip the update and flag it for the operator.

## Status badges

| Badge | Meaning |
|---|---|
| `🟡 Quoting` | Quote requested or sent, not yet accepted |
| `🟢 Active` | Work in progress: order confirmed, parts on the way, installation planned |
| `🔵 On Hold` | Paused, waiting on a decision or a delay |
| `✅ Completed` | Delivered and invoiced |
| `🔴 Cancelled` | Cancelled or abandoned |
| `⚠️ Sensitive` | The client is flagged `critical` in PocketBase; report only |

**PocketBase is the state; the badge is the display.** A task with a `🔁 On complete: job to <status>` line moves its job in PocketBase when it is completed (see `splatt-todoist`). The trigger engine never writes project files, so the badge catches up on the next `update-project-md` run. Until then the two can disagree and PocketBase is right. When you next touch the file, check the job's status in PocketBase and bring the badge in line.

## Rule: every client promise has a Todoist task

A project file must not hold an open promise that nothing will chase. Any Timeline row with a future Splatt commitment, and any Open Items row marked `⚪ Waiting` or `🟡 Open`, must carry the id of a live Todoist task that owns the chase. The validator rule `promises_are_guarded` checks the Change Log, Open Items, Timeline and Supplier Outreach Tracker sections for this and blocks the cycle if a promise has no task.

When the chain writes or keeps such a row, create the task in the same operation, with:

- `dueString`: the chase date (usually three business days before the promised date, 09:00),
- `deadlineDate`: the promised date itself,
- labels `client-promise` and a client tag,
- priority p2, `⏱ Est: 15m` for a chase or `45m` for a booking,
- the project line pointing at this `PROJECT.md`,
- in the description: the PocketBase client id, a one-line summary of the promise, the Gmail message id of the email it came from, and what to do if a reply arrives first, if the other side stays silent, or if the date slips.

Row formats (the task id goes inside `<code>…</code>`, written here as `TASK_ID`):

- A past milestone: `| YYYY-MM-DD | <what happened> |`, no task needed.
- A future milestone with a Splatt commitment: `| YYYY-MM-DD (chase) / YYYY-MM-DD (deadline) | <promise> — Todoist <code>TASK_ID</code> |`.
- An Open Items row: the `Reminder Task` column holds the task id. It may not be empty while the status is `⚪ Waiting` or `🟡 Open`.
- A milestone that cannot be dated yet: `| TBC | <what> — gated by Todoist <code>PARENT_TASK_ID</code> |`, where the parent task creates the dated task when the gate clears.

## During ops and morning runs

- **Phase 2:** for each client that will get an action, classify: has a project file (queue the chain), qualifies but has none (ask about onboarding), or not a project (note it).
- **Phase 3:** drive every queued chain through the playbook tools.
- **Phase 5:** check every queued change has both halves (`update-project-md` and `update-project-overview-html`, each with a `run_id`). A missing half is reported under Decisions Needed as project file drift, and the run is logged as `partial`.
- Send a `project_update` Telegram for each project file changed. Never put a supplier price in it.

## Standalone requests

The operator can ask directly, for example:

- "Create a project file for Tui Valley's blower overhaul."
- "Update the Clearwater Bottling project with today's email."
- "What's the status of the Orchard Lane change parts?"
- "Show me the comms log for Filler Service 2026."
