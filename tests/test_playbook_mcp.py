"""Tests for playbook_mcp, the MCP proxy in front of the files server's playbook routes.

The proxy forwards each call to the files server (default port 8092) and
returns the reply. It has almost no logic, so these tests cover the edges:
the files server being down, a reply that is not JSON, a JSON array reply,
and the HTTP status being carried through. In each case the proxy must
return a dict the agent can read, never raise.

The normal pass-through path is covered by the files server's own tests
(tests/test_port_server.py).

Nothing here touches the network: httpx.get and httpx.post are replaced
with fakes, which also makes it possible to simulate network failures.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import httpx
import pytest

from playbook_mcp import server as pb

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FakeResponse:
    """Stands in for an httpx response, including ones whose body is not JSON."""

    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise json.JSONDecodeError("no json", self.text or "", 0)
        return self._payload


@pytest.fixture
def calls(monkeypatch):
    """Record what the proxy sent, and set what the fake network returns."""
    recorded = {"get": [], "post": []}
    replies = {"get": None, "post": None}

    def fake_get(url, params=None, timeout=None):
        recorded["get"].append({"url": url, "params": params, "timeout": timeout})
        reply = replies["get"]
        if isinstance(reply, Exception):
            raise reply
        return reply

    def fake_post(url, json=None, timeout=None):
        recorded["post"].append({"url": url, "json": json, "timeout": timeout})
        reply = replies["post"]
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(pb.httpx, "get", fake_get)
    monkeypatch.setattr(pb.httpx, "post", fake_post)
    recorded["replies"] = replies
    return recorded


def test_a_dead_server_is_reported_not_raised(calls):
    # A stopped files server must come back as a readable error, so the
    # agent can tell it apart from a playbook step that failed validation.
    calls["replies"]["get"] = httpx.ConnectError("connection refused")
    out = pb._get("/api/playbook/specs")
    assert out["error"] == "upstream_unreachable"
    assert "connection refused" in out["detail"]


def test_the_unreachable_message_says_which_address_it_tried(calls):
    # The error names the address it tried, which is usually enough to see
    # that the files server is not running or is on another port.
    calls["replies"]["post"] = httpx.ConnectError("nope")
    out = pb._post("/api/playbook/start", {"playbook_id": "x"})
    assert out["url"].endswith("/api/playbook/start")
    assert pb.UPSTREAM in out["url"]


def test_a_non_json_reply_is_handed_back_as_text(calls):
    # A non-JSON reply (for example an HTML error page from another program
    # on the port) is returned as text instead of failing in json parsing.
    calls["replies"]["get"] = FakeResponse(502, payload=None, text="<html>bad gateway</html>")
    out = pb._get("/api/playbook/runs")
    assert out["raw"] == "<html>bad gateway</html>"
    assert out["_status"] == 502


def test_the_http_status_is_always_carried_through(calls):
    # A failed step comes back as 400 with the failed validators in the body.
    # The status must be kept, or a rejection would look like a success.
    calls["replies"]["post"] = FakeResponse(400, {"failed_validators": ["a"]})
    out = pb._post("/api/playbook/step", {"run_id": "r", "step_id": "s"})
    assert out["_status"] == 400
    assert out["failed_validators"] == ["a"]


def test_a_list_reply_is_wrapped_so_it_can_carry_a_status(calls):
    # Two endpoints answer with a bare JSON array. An array has nowhere to
    # hold the status code, so the proxy wraps it in a dict.
    calls["replies"]["get"] = FakeResponse(200, [{"run_id": "1"}, {"run_id": "2"}])
    out = pb._get("/api/playbook/runs")
    assert out["items"] == [{"run_id": "1"}, {"run_id": "2"}]
    assert out["_status"] == 200


def test_start_only_sends_the_fields_it_was_given(calls):
    # The files server fills in its own defaults for inputs, context and
    # trigger. Sending nulls would replace those defaults with null.
    calls["replies"]["post"] = FakeResponse(200, {"run_id": "r1"})
    pb.playbook__start("triage-inbound-email")
    sent = calls["post"][-1]["json"]
    assert sent == {"playbook_id": "triage-inbound-email"}


def test_start_passes_through_everything_it_was_given(calls):
    calls["replies"]["post"] = FakeResponse(200, {"run_id": "r1"})
    pb.playbook__start("p", inputs={"a": 1}, context={"b": 2}, trigger="email")
    sent = calls["post"][-1]["json"]
    assert sent == {"playbook_id": "p", "inputs": {"a": 1},
                    "context": {"b": 2}, "trigger": "email"}


def test_a_step_with_no_evidence_still_sends_an_object(calls):
    # The server reads evidence as a dict, so a missing evidence argument
    # is sent as {} rather than null (which would cause a 500).
    calls["replies"]["post"] = FakeResponse(200, {"ok": True})
    pb.playbook__step("run1", "step1")
    sent = calls["post"][-1]["json"]
    assert sent["evidence"] == {}
    assert sent["status"] == "done"


def test_listing_runs_without_a_filter_sends_no_filter(calls):
    # Sending status=None as a query parameter would filter on the literal
    # string "None" and return no runs.
    calls["replies"]["get"] = FakeResponse(200, [])
    pb.playbook__runs()
    assert calls["get"][-1]["params"] in (None, {})


def test_listing_runs_with_a_filter_sends_it(calls):
    calls["replies"]["get"] = FakeResponse(200, [])
    pb.playbook__runs("open")
    assert calls["get"][-1]["params"] == {"status": "open"}


def test_fetching_one_run_puts_the_id_in_the_path(calls):
    calls["replies"]["get"] = FakeResponse(200, {"run_id": "abc"})
    pb.playbook__run("abc")
    assert calls["get"][-1]["url"].endswith("/api/playbook/run/abc")


def test_health_says_not_ok_when_the_server_is_down(calls):
    calls["replies"]["get"] = httpx.ConnectError("refused")
    out = pb.playbook__health()
    assert out["ok"] is False
    assert out["upstream"] == pb.UPSTREAM


def test_health_says_ok_when_the_server_answers(calls):
    calls["replies"]["get"] = FakeResponse(200, [])
    out = pb.playbook__health()
    assert out["ok"] is True
    assert out["status_code"] == 200


def test_the_upstream_can_be_pointed_somewhere_else():
    # The default must match the port bin/start gives the files server
    # (8092), and PLAYBOOK_SERVER_URL must override it. UPSTREAM is read at
    # import time, so each case imports the module in a fresh interpreter.
    def upstream_with(env_value):
        env = {k: v for k, v in os.environ.items() if k != "PLAYBOOK_SERVER_URL"}
        if env_value is not None:
            env["PLAYBOOK_SERVER_URL"] = env_value
        out = subprocess.run(
            [sys.executable, "-c",
             "from playbook_mcp import server; print(server.UPSTREAM)"],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        return out.stdout.strip()

    assert upstream_with(None) == "http://127.0.0.1:8092"
    assert upstream_with("http://127.0.0.1:18092") == "http://127.0.0.1:18092"


def test_every_tool_is_registered_with_a_description():
    # The description is what tells the agent when to use a tool, so every
    # tool must have one.
    tools = {t.name: t for t in pb.mcp._tool_manager.list_tools()}
    expected = {"playbook__start", "playbook__step", "playbook__runs",
                "playbook__run", "playbook__specs", "playbook__health"}
    assert expected <= set(tools), sorted(tools)
    for name in expected:
        assert (tools[name].description or "").strip(), f"{name} has no description"


def test_the_module_can_be_started_the_way_the_client_starts_it():
    # MCP clients run `python3 -m playbook_mcp` with no shell and no working
    # directory, so importing the package must be enough.
    import importlib
    assert importlib.import_module("playbook_mcp").__doc__
