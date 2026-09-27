"""Playbook MCP server: how the agent runs a playbook.

A playbook is a fixed sequence of steps for a routine job (for example
logging an interaction or updating a PROJECT.md), defined as a
`*.spec.json` file. The state machine that runs playbooks lives in
server/project-files-server.js. This package is a thin MCP proxy in front
of it: each tool forwards one call to the files server's
/api/playbook/* routes and returns the answer unchanged.

It does no validation of its own. The files server is the only place that
decides whether a step passed, so there is one set of rules and no chance
of two checkers disagreeing.

Start it with `python -m playbook_mcp`.
"""
