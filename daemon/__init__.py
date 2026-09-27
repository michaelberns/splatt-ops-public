"""
The daemon: the always-on half of splatt-ops.

It keeps the Todoist board and PocketBase in step and runs the daily
housekeeping (backup, overdue sweep, prune, validator runs) without anyone
typing a command. `python -m daemon loop --write` is the process that runs
permanently; daemon/loop.py has the schedule and daemon/cli.py lists every
command.
"""
