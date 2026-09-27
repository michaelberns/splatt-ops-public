# Ops cycle checklist

A one-page checklist of the ops cycle in `SKILL.md`. The skill is the authority; this page is for ticking off the phases during a run. Run shell commands from the repo root.

## Phase 0: preflight

1. `tail -30 ~/.claude/skill-harness-sweep.log` (overnight sweep result).
2. `python3 -m tools.skill_harness preflight --operation ops`
   - 0: continue. 1: no route, stop. 2: ask the operator, default wait. 3: broken reference, stop and report.
3. `playbook__health`, then `playbook__runs` with `status: open`. Finish or close anything left open.

## Phase 1: gather (in parallel)

- Todoist: every task in the project `todoist.project_id` from `config/settings.yaml`, `responsibleUserFiltering: all`, `limit: 100`.
- Gmail inbox: `to:<operator address> after:YYYY/MM/DD is:unread`, last 5 days, up to 25.
- Gmail sent: `from:<operator address> after:YYYY/MM/DD`, last 5 days, up to 25.
- PocketBase: `get_daily_summary`.
- Xero connector: invoices awaiting payment; note the overdue ones.

Read full messages for anything actionable.

## Phase 2: analyse

- `get_client_history` for 30 days before deciding on a client.
- Per thread: sensitive (`critical`) → report only; no task → plan one; clearly done → plan completion; 5+ business days silent → Today and a follow-up email; new information → append context; untracked promise → plan a task; wrong section → plan a move; unpaid invoice → flag at 14+ days.
- Read the full sent thread before any ambiguous action.
- Project file check: has a project file → queue the playbook chain; qualifies but has none → ask; not a project → note it.

## Phase 3: execute

- Playbook-shaped actions through the playbook.
- Complete, create, update and move tasks as `splatt-todoist` describes (section ids from `config/settings.yaml`).
- Emails into the chat (`splatt-email`), `price-leak-guard` first when a client email has figures.
- Project files through `update-project-md` then `update-project-overview-html`.
- `log_interaction` for each meaningful interaction.
- Overdue invoices without a task get one. Paid invoices close their payment tasks.
- `task` Telegram after three or more task changes.

## Phase 4: sweep and triggers

- `python3 -m daemon sweep`
- `python3 -m daemon triggers --write`
- Keep both `needs_decision` lists for the report.

## Phase 5: verify, log, notify (in this order)

1. Close every playbook run opened this cycle (`playbook__runs`, `status: open`).
2. `bin/splatt-validate --json`
   - 0: success may be reported; repeat every warning.
   - 1: blocked; do not claim success; say what blocks.
   - 2: validator could not run; not a pass; run `python3 -m validator doctor`.
   - Read out every exception with its days left. Say what was skipped. Never grant a bypass.
3. `python3 -m tools.skill_harness invoke splatt-ss-agent --operation ops --result <success|partial|blocked|failed> --notes "..."`, with the result taken from the verdict.
4. `send_telegram.py --type ops_summary` with the verdict; `project_update` per project file changed; `alert` per overdue invoice flagged.

## Phase 6: report

Validator verdict, actions taken, sensitive clients, money, decisions needed, quick stats. Then offer next steps.
