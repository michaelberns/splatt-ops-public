---
name: splatt-morning-commands
description: The operator's morning sequence for Splatt Engineering. Use this skill whenever the operator says "ssagent morning", "morning", "morning check", "morning commands", "what's open", "check my sent and inbox", "plan today", or opens the day with something like "there are some open tasks, some got done yesterday, some haven't been touched, look at the sent mail". It is a thin orchestrator: it fixes the order of the morning (open work from the database first so nothing is forgotten, then sent mail, then the inbox, then reconcile, tick off, plan the day, verify) and hands every step to the splatt-ss-agent skill, which holds the rules and does the work.
---

# Splatt morning commands

This skill sets the order of the operator's morning run and nothing else. Every rule, format and safety boundary comes from `splatt-ss-agent`, which does the work. If the two ever disagree, `splatt-ss-agent` wins.

Why the order matters: Todoist's views and the inbox show what is recent. The database shows every open task, including the ones nobody has touched for weeks, which are the ones that get forgotten. So the database goes first, and its list is the checklist the run must account for by the end.

Run shell commands from the root of the splatt-ops repo.

## Step 0: load the engine and preflight

1. Load the `splatt-ss-agent` skill.
2. Run the skill harness preflight for the morning route:

   ```bash
   python3 -m tools.skill_harness preflight --operation morning
   ```

   Read the exit code with the table in `splatt-ss-agent` Phase 0. On exit 3 (a broken reference) or exit 2 (database down), stop and tell the operator.

## Step 1: every open task from the database

Before reading any email, pull every open task from PocketBase:

```
search_assignments   status: open   per_page: 50
search_assignments   status: open   overdue_only: true
get_daily_summary
```

Keep this list. It is the master checklist: every item on it must be ticked off, rescheduled, or left open with a one-line reason by the end of the run.

## Step 2: sent mail

Search the operator's sent mail for the last five days (`from:<operator address> after:YYYY/MM/DD`, up to 25). Sent mail shows whether an "open" task is already handled.

Read the full thread for anything ambiguous. Never assume the operator has not replied because an inbox message looks unanswered.

## Step 3: inbox

Search unread mail to the operator for the last five days (`to:<operator address> after:YYYY/MM/DD is:unread`, up to 25). Read the full message for anything actionable.

## Step 4: reconcile

Route every open task from Step 1 and every thread from Steps 2 and 3 through the `splatt-ss-agent` system router and its Phase 2 list:

- Open task, and sent mail shows it done: tick it off (Step 5).
- Open task with no movement and no email: it is stale. Surface it and propose a chase date.
- Email with no matching task: offer a new task.
- A promise in sent mail with nothing tracking it: create a task.
- A task out of date against a new email: append dated context.
- A sensitive client (`critical` in PocketBase): report only. No completions, moves or emails.

## Step 5: tick off what is done

1. Run the `splatt-ss-agent` cleanup procedure ("Cleanup: tick off what is already done"): fetch the evidence, dry run `python3 -m tools.tick_off`, show the operator the proven list, and only then run it with `--write`.
2. For a task the cleanup cannot judge (no reference number in its title), complete it only when the full email thread confirms it is done.
3. A sent invoice does not finish a job that still has an order to place or a follow-up to make. Move such a task to a waiting section instead. When in doubt, move the task to `today` and let the operator decide.

## Step 6: plan today

Sort everything still open into its section and give each task you touch an estimate and a real slot, using the `splatt-todoist` board table and scheduling rules. Section ids come from `config/settings.yaml`.

Keep `today` to 5 to 8 tasks. If more qualify, keep the most urgent and tell the operator what was pushed back.

## Step 7: account for the master checklist

Go back to the Step 1 list. Every item must now be ticked off, rescheduled with a slot, or left open with a reason. Name any open task still untouched. That is the failure this skill exists to prevent.

## Step 8: sweep, verify, report

1. Run `splatt-ss-agent` Phase 4 (`python3 -m daemon sweep`, then `python3 -m daemon triggers --write`) and keep their `needs_decision` lists.
2. Run `splatt-ss-agent` Phase 5: close your playbook runs, run `bin/splatt-validate --json` and report from its exit code, then log the run with `python3 -m tools.skill_harness invoke splatt-ss-agent --operation morning --result <from the verdict>`, then send the `ops_summary` Telegram.
3. Report in the `splatt-ss-agent` Phase 6 structure, starting with the validator verdict, and show the board with one column per section.

## Phrases that start this sequence

- `ssagent morning`, `morning`, `morning check`, `morning commands`: the whole sequence.
- `what's open`: emphasise Step 1.
- `check my sent and inbox`: Steps 2 and 3.
- `plan today`: Step 6.

"Tick off" or "cleanup" on its own runs the `splatt-ss-agent` cleanup procedure directly.

## Inherited rules

This skill never relaxes a `splatt-ss-agent` rule. In particular:

- Never send an email or leave a draft in Gmail. Emails go in the chat.
- Sensitive clients are surface only.
- No Xero writes without an explicit instruction.
- Ask when unsure.
- The validator decides whether the run succeeded.
