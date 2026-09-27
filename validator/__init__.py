"""
The validator: an independent, rule-based check on the business data.

What it does
    After the agent (Claude, driven by the skills in `skills/`) or the
    daemon has done some work, the validator reads PocketBase, the Todoist
    board and the client project folders directly and decides whether that
    work may be reported as a success. The agent reads one answer from it,
    `may_report_success`, or simply the exit code of `python -m validator run`.

How it is organised
    config/validation-rules.yaml   every rule, its severity and its wording
    rules.py                       loads and checks that file
    engine.py                      runs the rules and applies bypasses
    checks/standing.py             named checks (one function per `check:`)
    checks/requirements.py         building blocks for gates and money rules
    context.py                     cached, read-only access to the data
    report.py                      markdown, JSON and Telegram reports
    notifications.py               messages to the ops Telegram channel
    cli.py                         `python -m validator ...` and exit codes

Why it is separate
    The validator never imports the daemon. It shares only core/ with it
    (the PocketBase and Todoist clients, the schema and a few shared
    vocabularies such as the job statuses). If it reused the daemon's
    logic it would only prove that the code agrees with itself.
    docs/HARNESSES.md explains how it works alongside the write ledger,
    the playbook server and the skill harness.
"""
