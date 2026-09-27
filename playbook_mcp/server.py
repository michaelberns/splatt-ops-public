#!/usr/bin/env python3
"""
Playbook MCP server: forwards the agent's playbook calls to the files server.

What it does
    Each tool below sends one HTTP request to the playbook routes of
    server/project-files-server.js (default http://127.0.0.1:8092, override
    with PLAYBOOK_SERVER_URL) and returns the JSON answer.

Tools
    playbook__start   start a run of a playbook
    playbook__step    report one step as done, skipped or failed
    playbook__runs    list runs, optionally by status
    playbook__run     full detail of one run, with evidence and validation
    playbook__specs   list the playbook specs the server knows about
    playbook__health  check that the files server is answering

How a run works (all enforced by the files server, not here)
    1. playbook__start creates a run and returns its first step.
    2. Steps must be submitted in order. On status=done the server runs that
       step's validators (for example "a PocketBase record with these fields
       exists" or "this file was modified after the run started").
    3. A failed validator returns HTTP 400 with `failed_validators`, and the
       run stays on the same step.
    4. After the last step a final validation closes the run as `completed`
       or `failed_validation`.

Replies
    Every reply carries `_status`, the HTTP status code, so a 400 rejection
    never looks like a success. A list reply is wrapped as
    {"items": [...], "_status": 200}. If the files server cannot be reached
    the reply is {"error": "upstream_unreachable", "url": ..., "detail": ...}
    instead of an exception.
"""
from __future__ import annotations

import json
import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

UPSTREAM = os.environ.get("PLAYBOOK_SERVER_URL", "http://127.0.0.1:8092")
TIMEOUT = float(os.environ.get("PLAYBOOK_TIMEOUT", "30"))

mcp = FastMCP("splatt-playbook")


def _post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST a JSON payload upstream and return the reply with `_status` added."""
    url = f"{UPSTREAM}{path}"
    try:
        r = httpx.post(url, json=payload, timeout=TIMEOUT)
    except httpx.RequestError as e:
        return {"error": "upstream_unreachable", "url": url, "detail": str(e)}
    try:
        body = r.json()
    except json.JSONDecodeError:
        body = {"raw": r.text}
    body["_status"] = r.status_code
    return body


def _get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """GET from upstream and return the reply with `_status` added.

    A JSON array has nowhere to carry the status code, so it is wrapped
    as {"items": [...], "_status": ...}.
    """
    url = f"{UPSTREAM}{path}"
    try:
        r = httpx.get(url, params=params or {}, timeout=TIMEOUT)
    except httpx.RequestError as e:
        return {"error": "upstream_unreachable", "url": url, "detail": str(e)}
    try:
        body = r.json()
    except json.JSONDecodeError:
        body = {"raw": r.text}
    if isinstance(body, list):
        return {"_status": r.status_code, "items": body}
    body["_status"] = r.status_code
    return body


@mcp.tool()
def playbook__start(
    playbook_id: str,
    inputs: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    trigger: str | None = None,
) -> dict[str, Any]:
    """Start a new playbook run. Returns { run_id, step, total_steps, storage }.

    Only the arguments you pass are sent, so the files server's own defaults
    for inputs, context and trigger apply to the rest.
    """
    payload: dict[str, Any] = {"playbook_id": playbook_id}
    if inputs is not None:
        payload["inputs"] = inputs
    if context is not None:
        payload["context"] = context
    if trigger is not None:
        payload["trigger"] = trigger
    return _post("/api/playbook/start", payload)


@mcp.tool()
def playbook__step(
    run_id: str,
    step_id: str,
    status: str = "done",
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Submit the result of the current step. status is done, skip or fail.

    On status=done the server runs the step's validators. If any fail, the
    reply has _status 400 and failed_validators, and the run stays on this
    step. Read the reasons, fix the real thing they check (the record, the
    file, the task), then resubmit the same step_id. Do not change the
    evidence just to make a validator pass.
    """
    payload: dict[str, Any] = {
        "run_id": run_id,
        "step_id": step_id,
        "status": status,
        "evidence": evidence or {},
    }
    return _post("/api/playbook/step", payload)


@mcp.tool()
def playbook__runs(status: str | None = None) -> dict[str, Any]:
    """List playbook runs, newest first. status filter: open, completed or failed_validation.

    Call with status='open' at the start and end of an ops cycle to find
    runs that were started and never finished.
    """
    return _get("/api/playbook/runs", {"status": status} if status else None)


@mcp.tool()
def playbook__run(run_id: str) -> dict[str, Any]:
    """Fetch one run in full, including every step's evidence and validation results."""
    return _get(f"/api/playbook/run/{run_id}")


@mcp.tool()
def playbook__specs() -> dict[str, Any]:
    """List every playbook spec the server knows about, to discover what can be run."""
    return _get("/api/playbook/specs")


@mcp.tool()
def playbook__health() -> dict[str, Any]:
    """Check that the files server is answering. Returns ok, the upstream URL and the status code."""
    res = _get("/api/playbook/runs", {"status": "open"})
    if res.get("error"):
        return {"ok": False, "upstream": UPSTREAM, "detail": res}
    return {"ok": True, "upstream": UPSTREAM, "status_code": res.get("_status")}


if __name__ == "__main__":
    mcp.run()
