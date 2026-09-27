"""
Validator tests: the rules file, the standing checks, gates, money rules,
the engine's guarantees and the JSON report.

Most checks are tested in both directions: a case where the check must
fail and a case where it must pass. The failing case matters most, since
a check that can never fail provides no protection.

All data is fictional and held in the fakes from tests/fakes.py; nothing
here talks to PocketBase or Todoist.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.ledger import WriteLedger
from tests.fakes import FakePB, FakeTodoist
from validator import result
from validator.context import Context
from validator.engine import BypassStore, Engine
from validator.rules import Rule, RuleError, RuleSet

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
RULES = REPO_ROOT / "config" / "validation-rules.yaml"


def make_rule(**kwargs):
    raw = {
        "id": "test_rule",
        "severity": "block",
        "bypassable": False,
        "check": "noop",
        "last_reviewed": datetime.now(timezone.utc).date(),
    }
    raw.update(kwargs)
    return Rule(kwargs.pop("section", "standing"), raw)


def make_ctx(data=None, tasks=None, ledger=None, collections=None):
    return Context(
        pb=FakePB(data or {}, collections),
        todoist=FakeTodoist(tasks or []) if tasks is not None else None,
        ledger=ledger,
        started_at=datetime.now(timezone.utc),
        run_id="testrun",
    )


# The rules file itself
def test_shipped_rules_file_loads():
    ruleset = RuleSet(RULES)
    assert len(ruleset.rules) > 15
    assert ruleset.by_id("writes_landed").severity == "block"
    assert ruleset.by_id("writes_landed").bypassable is False


def test_every_named_check_is_implemented():
    """Every `check:` in the shipped rules file names a registered function,
    so the rules file cannot drift away from the code."""
    from validator.checks import standing

    ruleset = RuleSet(RULES)
    missing = [
        rule.id for rule in ruleset.rules
        if rule.check and rule.check not in standing.REGISTRY
    ]
    assert not missing, "rules name checks that do not exist: %s" % missing


def test_every_requirement_type_is_implemented():
    from validator.checks import requirements

    ruleset = RuleSet(RULES)
    missing = set()

    def walk(specs):
        for spec in specs:
            kind = spec.get("type")
            if kind not in requirements.REGISTRY:
                missing.add(kind)
            if kind in ("any_of", "all_of"):
                walk(spec.get("options") or [])

    for rule in ruleset.rules:
        walk(rule.requires)
    assert not missing, "rules use requirement types that do not exist: %s" % sorted(missing)


def test_money_rules_are_never_bypassable():
    """Anything that can lose cash cannot be waived."""
    ruleset = RuleSet(RULES)
    waivable = [r.id for r in ruleset.money() if r.bypassable]
    assert not waivable, "money rules must not be bypassable: %s" % waivable


def test_rule_without_last_reviewed_is_rejected(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text(
        "meta: {version: 1}\n"
        "standing:\n"
        "  - id: x\n"
        "    severity: warn\n"
        "    check: rules_freshness\n"
    )
    with pytest.raises(RuleError) as exc:
        RuleSet(path)
    assert "last_reviewed" in str(exc.value)


def test_duplicate_rule_id_is_rejected(tmp_path):
    path = tmp_path / "dupe.yaml"
    path.write_text(
        "meta: {version: 1}\n"
        "standing:\n"
        "  - {id: x, severity: warn, check: rules_freshness, last_reviewed: 2026-08-08}\n"
        "  - {id: x, severity: warn, check: rules_freshness, last_reviewed: 2026-08-08}\n"
    )
    with pytest.raises(RuleError):
        RuleSet(path)


# writes_landed
def test_writes_landed_catches_a_write_that_never_arrived(tmp_path):
    from validator.checks.standing import reread_claimed_writes

    ledger = WriteLedger(tmp_path / "writes.jsonl", run_id="testrun")
    ledger.record("create", "clients", "abc123", ["name"], "sync")
    ctx = make_ctx({"clients": []}, ledger=ledger)
    res = reread_claimed_writes(make_rule(), ctx)
    assert res.status == result.FAIL
    assert "does not exist" in " ".join(res.evidence)


def test_writes_landed_catches_a_field_that_came_back_empty(tmp_path):
    from validator.checks.standing import reread_claimed_writes

    ledger = WriteLedger(tmp_path / "writes.jsonl", run_id="testrun")
    ledger.record("update", "clients", "abc123", ["notes"], "agent")
    ctx = make_ctx({"clients": [{"id": "abc123", "name": "X", "notes": ""}]}, ledger=ledger)
    res = reread_claimed_writes(make_rule(), ctx)
    assert res.status == result.FAIL
    assert "came back empty" in " ".join(res.evidence)


def test_writes_landed_passes_when_the_write_is_real(tmp_path):
    from validator.checks.standing import reread_claimed_writes

    ledger = WriteLedger(tmp_path / "writes.jsonl", run_id="testrun")
    ledger.record("create", "clients", "abc123", ["name"], "agent")
    ctx = make_ctx({"clients": [{"id": "abc123", "name": "Clearwater Bottling"}]}, ledger=ledger)
    res = reread_claimed_writes(make_rule(), ctx)
    assert res.status == result.PASS


# schema drift
def test_schema_drift_reports_a_missing_field():
    from tests.fakes import collection
    from validator.checks.standing import compare_schema_to_live

    ctx = make_ctx(collections=[collection("clients", ["name"])])
    res = compare_schema_to_live(make_rule(), ctx)
    assert res.status == result.FAIL
    joined = " ".join(res.evidence)
    assert "clients.aliases" in joined or "aliases" in joined


# todoist and pocketbase agreement
def test_todoist_task_without_an_assignment_is_caught():
    from validator.checks.standing import reconcile_todoist_assignments

    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    ctx = make_ctx(
        {"assignments": []},
        tasks=[{"id": "6abc", "content": "Chase Clearwater Bottling", "created_at": old}],
    )
    res = reconcile_todoist_assignments(make_rule(), ctx)
    assert res.status == result.FAIL
    assert "no assignment record" in " ".join(res.evidence)


def test_brand_new_task_is_given_grace():
    from validator.checks.standing import reconcile_todoist_assignments

    now = datetime.now(timezone.utc).isoformat()
    rule = make_rule(tolerance={"grace_seconds": 300})
    ctx = make_ctx({"assignments": []}, tasks=[{"id": "6abc", "content": "New", "created_at": now}])
    res = reconcile_todoist_assignments(rule, ctx)
    assert res.status == result.PASS


def test_assignment_pointing_at_a_dead_task_is_caught():
    from validator.checks.standing import reconcile_todoist_assignments

    ctx = make_ctx(
        {"assignments": [{"id": "a1", "todoist_id": "6gone", "status": "open", "content": "x"}]},
        tasks=[],
    )
    res = reconcile_todoist_assignments(make_rule(), ctx)
    assert res.status == result.FAIL
    assert "no longer exists" in " ".join(res.evidence)


# orphans
def test_orphan_assignment_is_caught_by_alias():
    from validator.checks.standing import assignment_client_links

    ctx = make_ctx({
        "clients": [{"id": "c1", "name": "Clearwater Bottling", "aliases": "ClearwaterOne, Clearwater"}],
        "assignments": [
            {"id": "a1", "content": "Send ClearwaterOne the quote", "status": "open", "client": ""},
        ],
    })
    res = assignment_client_links(make_rule(severity="warn"), ctx)
    assert res.status == result.FAIL


# project links on assignments
JOB_CTX = {
    "clients": [{"id": "c1", "name": "Bright Fizz Kombucha", "aliases": "Bright Fizz"}],
    "jobs": [{"id": "j1", "title": "Bright Fizz / cable drying system install",
              "client": "c1", "status": "active", "archived": False}],
}


def _job_ctx(assignments):
    data = dict(JOB_CTX)
    data["assignments"] = assignments
    return make_ctx(data)


def test_a_task_whose_client_has_projects_but_names_none_of_them_is_raised():
    """An unlinked task whose client has live projects is reported, so it
    does not sit on the board as client work with no project."""
    from validator.checks.standing import assignment_job_links

    ctx = _job_ctx([{"id": "a1", "content": "Bright Fizz / call about the invoice",
                     "status": "open", "client": "c1", "job": ""}])
    res = assignment_job_links(make_rule(severity="warn"), ctx)

    assert res.status == result.FAIL
    assert "names any of the client's projects" in " ".join(res.evidence)


def test_a_task_the_matcher_can_place_but_nobody_linked_is_raised():
    from validator.checks.standing import assignment_job_links

    ctx = _job_ctx([{"id": "a1", "content": "Bright Fizz / cable drying system spares",
                     "status": "open", "client": "c1", "job": ""}])
    res = assignment_job_links(make_rule(severity="warn"), ctx)

    assert res.status == result.FAIL
    assert "can be filed under a project" in " ".join(res.evidence)


def test_two_projects_matching_equally_well_are_raised_by_name():
    """When two projects match equally well the matcher does not pick,
    and the task is reported with both names."""
    from validator.checks.standing import assignment_job_links

    data = dict(JOB_CTX)
    data["jobs"] = [
        {"id": "j1", "title": "Bright Fizz / cable drying system north",
         "client": "c1", "status": "active", "archived": False},
        {"id": "j2", "title": "Bright Fizz / cable drying system south",
         "client": "c1", "status": "active", "archived": False},
    ]
    data["assignments"] = [{"id": "a1", "content": "Bright Fizz / cable drying system spares",
                            "status": "open", "client": "c1", "job": ""}]
    res = assignment_job_links(make_rule(severity="warn"), make_ctx(data))

    assert res.status == result.FAIL
    assert "two projects match this equally well" in " ".join(res.evidence)


def test_a_task_whose_client_has_no_live_project_is_counted_not_failed():
    """The client has no live project to file the task under, so the task
    is counted in the summary rather than failed."""
    from validator.checks.standing import assignment_job_links

    data = dict(JOB_CTX)
    data["jobs"] = [{"id": "j1", "title": "Bright Fizz / cable drying system install",
                     "client": "c1", "status": "paid", "archived": False}]
    data["assignments"] = [{"id": "a1", "content": "Bright Fizz / call about the invoice",
                            "status": "open", "client": "c1", "job": ""}]
    res = assignment_job_links(make_rule(severity="warn"), make_ctx(data))

    assert res.status == result.PASS
    assert "1 have no live project" in res.summary


def test_a_task_already_filed_under_a_project_passes():
    from validator.checks.standing import assignment_job_links

    ctx = _job_ctx([{"id": "a1", "content": "Bright Fizz / call about the invoice",
                     "status": "open", "client": "c1", "job": "j1"}])
    res = assignment_job_links(make_rule(severity="warn"), ctx)

    assert res.status == result.PASS
    assert "1 linked" in res.summary


def test_a_closed_task_is_not_asked_for_a_project():
    from validator.checks.standing import assignment_job_links

    ctx = _job_ctx([{"id": "a1", "content": "Bright Fizz / call about the invoice",
                     "status": "completed", "client": "c1", "job": ""}])
    res = assignment_job_links(make_rule(severity="warn"), ctx)

    assert res.status == result.PASS


def test_orphan_quote_is_caught():
    from validator.checks.standing import quote_job_links

    ctx = make_ctx({
        "jobs": [{"id": "j1", "title": "Filler upgrade"}],
        "quotes": [{"id": "q1", "title": "QU-1001", "job": ""}],
    })
    res = quote_job_links(make_rule(severity="warn"), ctx)
    assert res.status == result.FAIL


def test_quote_pointing_at_a_deleted_job_is_caught():
    from validator.checks.standing import quote_job_links

    ctx = make_ctx({"jobs": [], "quotes": [{"id": "q1", "title": "QU-1001", "job": "jgone"}]})
    res = quote_job_links(make_rule(severity="warn"), ctx)
    assert res.status == result.FAIL
    assert "does not exist" in " ".join(res.evidence)


# gates
def test_gate_paid_fails_when_the_money_does_not_reconcile():
    """The job is completed and ticked as paid.

    gate_paid is triggered by the paid flag (on_flag_true: paid), because
    paid is a flag on the job rather than a status. The flag is the claim,
    so the flag is what has to be backed up by paid_amount.
    """
    ruleset = RuleSet(RULES)
    ctx = make_ctx({
        "jobs": [{"id": "j1", "title": "Capper rebuild", "status": "completed",
                  "paid": True,
                  "value": 10000, "paid_amount": 4000, "client": "c1"}],
        "clients": [{"id": "c1", "name": "Orchard Lane"}],
        "quotes": [], "invoices": [], "bills": [], "interactions": [],
        "assignments": [], "playbook_runs": [], "suppliers": [],
    })
    engine = Engine(ruleset, ctx)
    engine.run_gates()
    paid = [r for r in engine.results if r.rule_id == "gate_paid"]
    assert paid and paid[0].status == result.FAIL
    assert "gap of" in " ".join(paid[0].evidence)


def test_gate_paid_passes_within_tolerance():
    ruleset = RuleSet(RULES)
    ctx = make_ctx({
        "jobs": [{"id": "j1", "title": "Capper rebuild", "status": "completed",
                  "paid": True,
                  "value": 10000, "paid_amount": 9900, "client": "c1"}],
        "clients": [{"id": "c1", "name": "Orchard Lane"}],
        "quotes": [], "invoices": [], "bills": [], "interactions": [],
        "assignments": [], "playbook_runs": [], "suppliers": [],
    })
    engine = Engine(ruleset, ctx)
    engine.run_gates()
    paid = [r for r in engine.results if r.rule_id == "gate_paid"]
    assert paid and paid[0].status == result.PASS


def test_gate_paid_is_not_run_on_a_job_nobody_has_ticked_as_paid():
    """The flag is the trigger, so an unpaid job is not asked to reconcile."""
    ruleset = RuleSet(RULES)
    ctx = make_ctx({
        "jobs": [{"id": "j1", "title": "Capper rebuild", "status": "invoiced",
                  "value": 10000, "paid_amount": 0, "client": "c1"}],
        "clients": [{"id": "c1", "name": "Orchard Lane"}],
        "quotes": [], "invoices": [], "bills": [], "interactions": [],
        "assignments": [], "playbook_runs": [], "suppliers": [],
    })
    engine = Engine(ruleset, ctx)
    engine.run_gates()
    assert not [r for r in engine.results if r.rule_id == "gate_paid"]


def test_gate_paid_still_catches_a_paid_tick_after_the_job_moved_on():
    """The gate follows the flag, not the status, so a paid tick with no
    amount behind it is still caught after the job moves to completed."""
    ruleset = RuleSet(RULES)
    ctx = make_ctx({
        "jobs": [{"id": "j1", "title": "Capper rebuild", "status": "completed",
                  "paid": True, "value": 10000, "paid_amount": 0, "client": "c1"}],
        "clients": [{"id": "c1", "name": "Orchard Lane"}],
        "quotes": [], "invoices": [], "bills": [], "interactions": [],
        "assignments": [], "playbook_runs": [], "suppliers": [],
    })
    engine = Engine(ruleset, ctx)
    engine.run_gates()
    paid = [r for r in engine.results if r.rule_id == "gate_paid"]
    assert paid and paid[0].status == result.FAIL


def test_gate_quoted_fails_with_no_quote_record():
    ruleset = RuleSet(RULES)
    ctx = make_ctx({
        "jobs": [{"id": "j1", "title": "New line", "status": "quoted", "client": "c1"}],
        "clients": [{"id": "c1", "name": "Orchard Lane"}],
        "quotes": [], "invoices": [], "bills": [], "interactions": [],
        "assignments": [], "playbook_runs": [], "suppliers": [],
    })
    engine = Engine(ruleset, ctx)
    engine.run_gates()
    quoted = [r for r in engine.results if r.rule_id == "gate_quoted"]
    assert quoted and quoted[0].status == result.FAIL


def test_gate_quoted_reports_and_does_not_block():
    """gate_quoted warns, because a completed task may move a job to
    quoted before the quote document is filed. gate_invoiced stays
    blocking and cannot be bypassed."""
    ruleset = RuleSet(RULES)
    assert ruleset.by_id("gate_quoted").severity == "warn"
    assert ruleset.by_id("gate_invoiced").severity == "block"
    assert ruleset.by_id("gate_invoiced").bypassable is False


# money
def test_shipping_that_was_never_recharged_blocks():
    ruleset = RuleSet(RULES)
    ctx = make_ctx({
        "jobs": [{"id": "j1", "title": "Import", "status": "invoiced", "client": "c1",
                  "has_shipping": True, "shipping_paid": False}],
        "clients": [{"id": "c1", "name": "Orchard Lane"}],
        "quotes": [], "invoices": [], "bills": [], "interactions": [],
        "assignments": [], "playbook_runs": [], "suppliers": [],
    }, tasks=[])
    engine = Engine(ruleset, ctx)
    engine.run_money()
    ship = [r for r in engine.results if r.rule_id == "shipping_not_forgotten"]
    assert ship and ship[0].status == result.FAIL
    assert not engine.may_report_success()


def test_shipping_recharge_satisfied_by_a_bill():
    ruleset = RuleSet(RULES)
    ctx = make_ctx({
        "jobs": [{"id": "j1", "title": "Import", "status": "invoiced", "client": "c1",
                  "has_shipping": True, "shipping_paid": False}],
        "clients": [{"id": "c1", "name": "Orchard Lane"}],
        "bills": [{"id": "b1", "job": "j1", "type": "shipping",
                   "description": "Coastline freight", "amount": 900}],
        "invoices": [{"id": "i1", "job": "j1", "amount": 1200, "status": "sent"}],
        "quotes": [], "interactions": [], "assignments": [],
        "playbook_runs": [], "suppliers": [],
    }, tasks=[])
    engine = Engine(ruleset, ctx)
    engine.run_money()
    ship = [r for r in engine.results if r.rule_id == "shipping_not_forgotten"]
    assert ship and ship[0].status == result.PASS


def test_recharge_short_of_the_bill_is_caught():
    from validator.checks.standing import recharge_margin_applied

    ctx = make_ctx({
        "jobs": [{"id": "j1", "title": "Import", "status": "invoiced"}],
        "bills": [{"id": "b1", "job": "j1", "type": "shipping",
                   "description": "Coastline freight", "amount": 900}],
        "invoices": [{"id": "i1", "job": "j1", "amount": 700, "status": "sent"}],
    })
    res = recharge_margin_applied(make_rule(), ctx)
    assert res.status == result.FAIL
    assert "short before any margin" in " ".join(res.evidence)


# playbook runs
#
# playbook_runs_closed decides from a run's status whether it is still open.
# 'failed_validation' is a closed run, just like 'completed'.
SERVER_JS = REPO_ROOT / "server" / "project-files-server.js"


def a_run(status, days_ago=0, at=None):
    started = at or (datetime.now(timezone.utc) - timedelta(days=days_ago))
    return {"id": "r1", "playbook_id": "update-project-md",
            "status": status, "started_at": started.isoformat()}


def test_a_run_that_closed_as_failed_validation_is_not_dangling():
    # failed_validation is a terminal status: the run has finished and
    # kept its evidence, so there is nothing to chase.
    from validator.checks.standing import playbook_runs_closed

    ctx = make_ctx({"playbook_runs": [a_run("failed_validation", 21)]})
    res = playbook_runs_closed(make_rule(), ctx)
    assert res.status == result.PASS, res.evidence


def test_a_run_left_genuinely_open_is_still_caught():
    # A run that really is still open must still be reported.
    from validator.checks.standing import playbook_runs_closed

    ctx = make_ctx({"playbook_runs": [a_run("open", 21)]})
    res = playbook_runs_closed(make_rule(), ctx)
    assert res.status == result.FAIL
    assert "never closed out" in " ".join(res.evidence)


def test_a_run_opened_this_cycle_and_left_open_is_caught():
    # The run starts after the cycle did, so the "during this cycle" branch
    # reports it. A run that started shortly before the cycle and is under
    # a day old is not reported by either branch (documented in the check).
    from validator.checks.standing import playbook_runs_closed

    runs = []
    ctx = make_ctx({"playbook_runs": runs})
    runs.append(a_run("open", at=ctx.started_at + timedelta(seconds=5)))

    res = playbook_runs_closed(make_rule(), ctx)
    assert res.status == result.FAIL
    assert "during this cycle" in " ".join(res.evidence)


def test_completed_runs_pass():
    from validator.checks.standing import playbook_runs_closed

    ctx = make_ctx({"playbook_runs": [a_run("completed", 400)]})
    res = playbook_runs_closed(make_rule(), ctx)
    assert res.status == result.PASS


def test_the_terminal_statuses_are_exactly_what_the_server_writes():
    """Every status treated as closed must be one the server really writes,
    so the list cannot contain statuses nothing ever sets."""
    from validator.checks.standing import TERMINAL_RUN_STATUSES

    assert set(TERMINAL_RUN_STATUSES) == {"completed", "failed_validation"}, \
        TERMINAL_RUN_STATUSES

    body = SERVER_JS.read_text()
    for status in TERMINAL_RUN_STATUSES:
        assert "'%s'" % status in body, \
            "%r is treated as closed but the server never writes it" % status


def test_open_is_never_treated_as_closed():
    from validator.checks.standing import TERMINAL_RUN_STATUSES

    assert "open" not in TERMINAL_RUN_STATUSES


# engine behaviour
def test_a_check_that_raises_becomes_an_error_not_a_pass():
    from validator.checks import standing

    standing.REGISTRY["explodes"] = lambda rule, ctx: 1 / 0
    ruleset = RuleSet(RULES)
    engine = Engine(ruleset, make_ctx())
    res = engine._run_named_check(make_rule(check="explodes"))
    assert res.status == result.ERROR
    assert not res.status == result.PASS
    del standing.REGISTRY["explodes"]


def test_unimplemented_check_is_an_error():
    ruleset = RuleSet(RULES)
    engine = Engine(ruleset, make_ctx())
    res = engine._run_named_check(make_rule(check="does_not_exist"))
    assert res.status == result.ERROR


def test_blocking_failure_stops_the_agent_reporting_success():
    ruleset = RuleSet(RULES)
    engine = Engine(ruleset, make_ctx())
    engine.results = [result.failed(make_rule(severity="block"), "boom")]
    assert engine.may_report_success() is False


def test_warning_does_not_stop_the_agent():
    ruleset = RuleSet(RULES)
    engine = Engine(ruleset, make_ctx())
    engine.results = [result.failed(make_rule(severity="warn"), "meh")]
    assert engine.may_report_success() is True


def test_a_skipped_check_is_not_counted_as_a_pass():
    ruleset = RuleSet(RULES)
    engine = Engine(ruleset, make_ctx())
    engine.results = [result.skipped(make_rule(), "Xero was not reachable")]
    assert engine.skipped()
    assert engine.blocking_failures() == []
    assert engine.warnings() == []


def test_the_json_the_reporting_agent_reads_keeps_its_shape(tmp_path):
    """The agent reads this JSON and cannot see the code, so these keys are
    a contract. Renaming one would break its reporting."""
    import json

    from datetime import date

    from validator import report

    # A one-rule file with an active temporary exception, so the
    # "exceptions" list has an entry to check.
    today = date.today()
    rules = tmp_path / "rules.yaml"
    rules.write_text(
        "meta: {version: 1}\n"
        "standing:\n"
        "  - id: softened\n"
        "    severity: block\n"
        "    bypassable: true\n"
        "    check: rules_freshness\n"
        "    last_reviewed: %s\n"
        "    exception:\n"
        "      until: %s\n"
        "      severity: warn\n"
        "      reason: waiting for the client links to be backfilled\n"
        % (today.isoformat(), (today + timedelta(days=30)).isoformat())
    )
    ruleset = RuleSet(rules)
    engine = Engine(ruleset, make_ctx())
    engine.results = [
        result.failed(make_rule(severity="block"), "boom"),
        result.failed(make_rule(severity="warn"), "meh"),
        result.skipped(make_rule(), "could not look"),
        result.passed(make_rule(), "fine"),
    ]
    payload = json.loads(report.to_json(engine, "testrun"))

    for key in ("run_id", "verdict", "may_report_success", "counts",
                "blocking", "warnings", "bypassed", "skipped", "exceptions",
                "notes"):
        assert key in payload, "the agent reads '%s' and it is gone" % key

    # Each exception carries its countdown, so the agent can say how long
    # is left as well as that a rule has been softened.
    assert payload["exceptions"], "the fixture rule is under an exception"
    for item in payload["exceptions"]:
        for key in ("rule_id", "really", "treated_as", "until", "days_left", "reason"):
            assert key in item, "exceptions are missing '%s'" % key
    softened = payload["exceptions"][0]
    assert softened["rule_id"] == "softened"
    assert (softened["really"], softened["treated_as"]) == ("block", "warn")
    assert softened["days_left"] == 30

    assert payload["may_report_success"] is False
    assert payload["verdict"] == "BLOCKED"
    assert len(payload["blocking"]) == 1
    assert len(payload["warnings"]) == 1
    assert len(payload["skipped"]) == 1
    for entry in payload["blocking"] + payload["warnings"] + payload["skipped"]:
        assert "rule_id" in entry
        assert "status" in entry
        assert "summary" in entry


def test_bypass_clears_a_failure_and_records_why(tmp_path):
    ruleset = RuleSet(RULES)
    store = BypassStore(tmp_path / "bypasses.json", ttl_hours=72)
    store.grant("test_rule", "client confirmed by phone, nothing lost")
    engine = Engine(ruleset, make_ctx(), store)
    engine._collect(result.failed(make_rule(bypassable=True), "boom"))
    assert engine.results[0].status == result.BYPASSED
    assert "nothing lost" in " ".join(engine.results[0].evidence)
    assert "bypassed by operator" in " ".join(engine.results[0].evidence)
    assert engine.may_report_success() is True


def test_expired_bypass_stops_working(tmp_path):
    ruleset = RuleSet(RULES)
    store = BypassStore(tmp_path / "bypasses.json", ttl_hours=72)
    store.grant("test_rule", "temporary")
    store.data["test_rule|"]["granted_at"] = (
        datetime.now(timezone.utc) - timedelta(days=30)
    ).isoformat()
    engine = Engine(ruleset, make_ctx(), store)
    engine._collect(result.failed(make_rule(bypassable=True), "boom"))
    assert engine.results[0].status == result.FAIL


def test_non_bypassable_failure_ignores_a_waiver(tmp_path):
    ruleset = RuleSet(RULES)
    store = BypassStore(tmp_path / "bypasses.json", ttl_hours=72)
    store.grant("test_rule", "please just let me through")
    engine = Engine(ruleset, make_ctx(), store)
    engine._collect(result.failed(make_rule(bypassable=False), "money would be lost"))
    assert engine.results[0].status == result.FAIL
    assert engine.may_report_success() is False


def test_gate_won_evaluates_gate_quoted_even_though_the_job_is_no_longer_quoted():
    """gate_won requires gate_quoted, but gates run against the status a
    job is in, and a won job is no longer quoted. The engine evaluates
    gate_quoted on demand, so the dependency is checked rather than
    reported as 'not evaluated'."""
    ruleset = RuleSet(RULES)
    ctx = make_ctx({
        "jobs": [{"id": "j1", "title": "Line upgrade", "status": "won", "client": "c1"}],
        "clients": [{"id": "c1", "name": "Orchard Lane"}],
        "quotes": [], "invoices": [], "bills": [], "interactions": [],
        "assignments": [], "playbook_runs": [], "suppliers": [],
    }, tasks=[])
    engine = Engine(ruleset, ctx)
    engine.run_gates()
    won = [r for r in engine.results if r.rule_id == "gate_won"]
    assert won and won[0].status == result.FAIL
    joined = " ".join(won[0].evidence)
    assert "gate_quoted failed" in joined
    assert "not evaluated" not in joined


def test_a_circular_gate_dependency_does_not_hang(tmp_path):
    path = tmp_path / "circular.yaml"
    path.write_text(
        "meta: {version: 1, review_after_days: 180}\n"
        "gates:\n"
        "  - id: gate_a\n"
        "    severity: warn\n"
        "    last_reviewed: 2026-08-08\n"
        "    on_transition_to: won\n"
        "    requires:\n"
        "      - {type: gate_passed, gate: gate_b}\n"
        "  - id: gate_b\n"
        "    severity: warn\n"
        "    last_reviewed: 2026-08-08\n"
        "    on_transition_to: won\n"
        "    requires:\n"
        "      - {type: gate_passed, gate: gate_a}\n"
    )
    ruleset = RuleSet(path)
    ctx = make_ctx({"jobs": [{"id": "j1", "title": "x", "status": "won"}]})
    engine = Engine(ruleset, ctx)
    engine.run_gates()
    assert engine.results


def test_schema_drift_reports_an_unmodelled_collection():
    from tests.fakes import collection
    from validator.checks.standing import compare_schema_to_live
    from core import schema

    live = [
        collection(name, sorted(spec["fields"]))
        for name, spec in schema.SCHEMA.items()
    ]
    live.append(collection("something_new", ["a", "b"]))
    ctx = make_ctx(collections=live)
    res = compare_schema_to_live(make_rule(), ctx)
    assert res.status == result.FAIL
    assert "something_new" in " ".join(res.evidence)


def test_schema_drift_ignores_collections_marked_not_ops():
    from tests.fakes import collection
    from validator.checks.standing import compare_schema_to_live
    from core import schema

    live = [
        collection(name, sorted(spec["fields"]))
        for name, spec in schema.SCHEMA.items()
    ]
    for name in schema.NOT_OPS:
        live.append(collection(name, ["whatever"]))
    ctx = make_ctx(collections=live)
    res = compare_schema_to_live(make_rule(), ctx)
    assert res.status == result.PASS


def test_promise_guard_ignores_a_past_correspondence_row(tmp_path):
    from validator.checks.standing import promise_task_guard

    folder = tmp_path / "Orchard Lane"
    folder.mkdir()
    (folder / "PROJECT.md").write_text(
        "# Communications Log\n"
        "| 2026-04-01 | IN | Supplier | Re: parts | Will send pricing next week |\n"
        "| 2026-04-02 | OUT | Splatt | Re: parts | Told them we promised delivery |\n"
    )
    ctx = make_ctx()
    ctx.project_root = tmp_path
    res = promise_task_guard(make_rule(), ctx)
    assert res.status == result.PASS


def test_promise_guard_catches_an_open_item_with_no_task(tmp_path):
    from validator.checks.standing import promise_task_guard

    folder = tmp_path / "Orchard Lane"
    folder.mkdir()
    (folder / "PROJECT.md").write_text(
        "# Open Items\n"
        "- We will send the revised quote by Friday\n"
    )
    ctx = make_ctx()
    ctx.project_root = tmp_path
    res = promise_task_guard(make_rule(), ctx)
    assert res.status == result.FAIL
    assert "no Todoist task id" in " ".join(res.evidence)


def test_promise_guard_accepts_an_open_item_that_carries_a_live_task(tmp_path):
    from validator.checks.standing import promise_task_guard

    folder = tmp_path / "Orchard Lane"
    folder.mkdir()
    (folder / "PROJECT.md").write_text(
        "# Open Items\n"
        "- We will send the revised quote by Friday (6Xtask0000000001)\n"
    )
    ctx = make_ctx(tasks=[{"id": "6Xtask0000000001", "content": "Send quote"}])
    ctx.project_root = tmp_path
    res = promise_task_guard(make_rule(), ctx)
    assert res.status == result.PASS


def test_promise_guard_catches_a_task_id_that_is_dead(tmp_path):
    from validator.checks.standing import promise_task_guard

    folder = tmp_path / "Orchard Lane"
    folder.mkdir()
    (folder / "PROJECT.md").write_text(
        "# Open Items\n"
        "- We will send the revised quote by Friday (6Xtask0000000001)\n"
    )
    ctx = make_ctx(tasks=[])
    ctx.project_root = tmp_path
    res = promise_task_guard(make_rule(), ctx)
    assert res.status == result.FAIL
    assert "no longer exists" in " ".join(res.evidence)


def test_a_skip_is_never_counted_as_a_pass():
    res = result.skipped(make_rule(), "could not check")
    assert res.status == result.SKIP
    assert res.blocking is False
    assert res.failed is False


# invoicing jobs with no invoice record
def _invoicing_ctx(invoices, bills=None, quotes=None):
    return make_ctx({
        "jobs": [
            {"id": "j1", "title": "Capper Remediation", "status": "invoicing",
             "client": "c1", "value": 0},
            {"id": "j2", "title": "Bearing Cartridge", "status": "invoicing",
             "client": "c1", "value": 1200},
            {"id": "j3", "title": "Archived one", "status": "invoicing",
             "client": "c1", "value": 500, "archived": True},
            {"id": "j4", "title": "Still quoting", "status": "quoting",
             "client": "c1", "value": 900},
        ],
        "clients": [{"id": "c1", "name": "Kowhai Health NZ Ltd"}],
        "quotes": quotes or [],
        "invoices": invoices,
        "bills": bills or [],
        "interactions": [], "assignments": [], "playbook_runs": [], "suppliers": [],
    })


def _run_invoicing_rule(ctx):
    from validator.checks import standing
    rule = RuleSet(RULES).by_id("invoicing_job_has_an_invoice")
    return standing.REGISTRY["invoicing_job_has_an_invoice"](rule, ctx)


def test_invoicing_job_with_an_invoice_record_is_not_listed():
    ctx = _invoicing_ctx(
        invoices=[{"id": "i1", "invoice_number": "SE INV-41374", "job": "j2"}],
        bills=[{"id": "b1", "job": "j1", "description": "Taponera parts"}],
        quotes=[{"id": "q1", "title": "QU-8473 Capper parts", "job": "j1"}],
    )
    res = _run_invoicing_rule(ctx)
    assert res.status == result.FAIL
    joined = "\n".join(res.evidence)
    assert "Bearing Cartridge" not in joined
    assert "Archived one" not in joined
    assert "Still quoting" not in joined
    assert res.evidence == [
        "job 'Capper Remediation' (Kowhai Health NZ Ltd) is in invoicing with "
        "no invoice record. value 0, quote QU-8473, bills on file: 1"
    ]


def test_invoicing_rule_passes_when_every_invoicing_job_has_an_invoice():
    ctx = _invoicing_ctx(invoices=[
        {"id": "i1", "invoice_number": "INV-41367", "job": "j1"},
        {"id": "i2", "invoice_number": "INV-41370", "job": "j2"},
    ])
    res = _run_invoicing_rule(ctx)
    assert res.status == result.PASS


def test_invoicing_rule_is_a_warning_and_does_not_block():
    ruleset = RuleSet(RULES)
    rule = ruleset.by_id("invoicing_job_has_an_invoice")
    assert rule.severity == "warn"
    assert rule.bypassable is True
    ctx = _invoicing_ctx(invoices=[])
    engine = Engine(ruleset, ctx)
    engine.run_standing()
    mine = [r for r in engine.results if r.rule_id == "invoicing_job_has_an_invoice"]
    assert mine and mine[0].status == result.FAIL
    assert not mine[0].blocking


# ==================================================================
# Task state triggers
#
# Eight rules on one mechanism (core/triggers.py). Each has at least one
# test where it must fail and one where it must pass.
# ==================================================================
from core import triggers as triggers_mod  # noqa: E402

TRIGGER_LINE = triggers_mod.render_on_complete("quoted")


def _trigger_ctx(assignments=(), jobs=(), clients=None, tasks=None,
                 interactions=(), invoices=()):
    return make_ctx(
        {
            "assignments": list(assignments),
            "jobs": list(jobs),
            "clients": list(clients if clients is not None
                            else [{"id": "c1", "name": "Kauri Springs"}]),
            "interactions": list(interactions),
            "invoices": list(invoices),
            "quotes": [], "bills": [], "playbook_runs": [], "suppliers": [],
        },
        tasks=tasks,
    )


def _run_rule(rule_id, ctx):
    from validator.checks import standing
    rule = RuleSet(RULES).by_id(rule_id)
    return standing.REGISTRY[rule.check](rule, ctx)


def _asg(record_id="a1", todoist_id="6aaaaaaaaaaaaaaa", content="Send the quote",
         job="j1", status="open", on_complete="", auto_kind="", archived=False):
    return {
        "id": record_id, "todoist_id": todoist_id, "content": content,
        "job": job, "status": status, "archived": archived,
        triggers_mod.PB_STATUS_FIELD: on_complete,
        triggers_mod.PB_AUTO_FIELD: auto_kind,
    }


def _job(job_id="j1", status="quoting", client="c1", title="Filler upgrade",
         changed=None, by="", archived=False):
    return {
        "id": job_id, "title": title, "status": status, "client": client,
        "archived": archived,
        triggers_mod.JOB_CHANGED_AT: changed or "",
        triggers_mod.JOB_CHANGED_BY: by,
    }


def _ago(days):
    moment = datetime.now(timezone.utc) - timedelta(days=days)
    return moment.strftime("%Y-%m-%d %H:%M:%S.000Z")


def _task(todoist_id="6aaaaaaaaaaaaaaa", description=""):
    return {"id": todoist_id, "content": "Send the quote",
            "description": description}


# trigger_line_matches_record
def test_a_trigger_line_with_an_empty_mirror_is_reported():
    ctx = _trigger_ctx(
        assignments=[_asg(on_complete="")],
        tasks=[_task(description=TRIGGER_LINE)],
    )
    res = _run_rule("trigger_line_matches_record", ctx)
    assert res.status == result.FAIL
    assert "PocketBase says none" in "\n".join(res.evidence)


def test_a_trigger_line_that_matches_its_mirror_passes():
    ctx = _trigger_ctx(
        assignments=[_asg(on_complete="quoted")],
        tasks=[_task(description=TRIGGER_LINE)],
    )
    assert _run_rule("trigger_line_matches_record", ctx).status == result.PASS


# trigger_tasks_link_a_job
def test_a_trigger_on_a_task_with_no_project_is_reported():
    ctx = _trigger_ctx(assignments=[_asg(job="", on_complete="quoted")],
                       jobs=[_job()])
    res = _run_rule("trigger_tasks_link_a_job", ctx)
    assert res.status == result.FAIL
    assert "not linked to one" in "\n".join(res.evidence)


def test_a_trigger_on_a_linked_task_passes():
    ctx = _trigger_ctx(assignments=[_asg(on_complete="quoted")], jobs=[_job()])
    assert _run_rule("trigger_tasks_link_a_job", ctx).status == result.PASS


# quote_tasks_carry_a_trigger
def test_a_send_quote_task_on_a_quoting_job_with_no_trigger_is_reported():
    ctx = _trigger_ctx(assignments=[_asg()], jobs=[_job(status="quoting")])
    res = _run_rule("quote_tasks_carry_a_trigger", ctx)
    assert res.status == result.FAIL
    assert "expected quoted" in "\n".join(res.evidence)


def test_a_send_quote_task_that_carries_its_trigger_passes():
    ctx = _trigger_ctx(assignments=[_asg(on_complete="quoted")],
                       jobs=[_job(status="quoting")])
    assert _run_rule("quote_tasks_carry_a_trigger", ctx).status == result.PASS


# trigger_change_was_logged
def test_a_task_driven_status_change_with_no_interaction_is_reported():
    ctx = _trigger_ctx(
        jobs=[_job(status="quoted", by="task:6aaaaaaaaaaaaaaa")],
        interactions=[],
    )
    res = _run_rule("trigger_change_was_logged", ctx)
    assert res.status == result.FAIL
    assert "no interaction records it" in "\n".join(res.evidence)


def test_a_task_driven_status_change_that_was_logged_passes():
    ctx = _trigger_ctx(
        jobs=[_job(status="quoted", by="task:6aaaaaaaaaaaaaaa")],
        interactions=[{"id": "i1", "job": "j1",
                       "source_key": "trigger:r1:a1:quoted"}],
    )
    assert _run_rule("trigger_change_was_logged", ctx).status == result.PASS


# completed_trigger_landed
def test_a_finished_trigger_that_did_not_move_the_job_blocks():
    ctx = _trigger_ctx(
        assignments=[_asg(status="completed", on_complete="quoted")],
        jobs=[_job(status="quoting")],
    )
    res = _run_rule("completed_trigger_landed", ctx)
    assert res.status == result.FAIL
    assert res.rule.severity == "block"
    assert "should have moved" in "\n".join(res.evidence)


def test_a_finished_trigger_whose_job_moved_passes():
    ctx = _trigger_ctx(
        assignments=[_asg(status="completed", on_complete="quoted")],
        jobs=[_job(status="won")],
    )
    assert _run_rule("completed_trigger_landed", ctx).status == result.PASS


def test_a_refusal_the_engine_was_right_to_make_is_not_a_failure():
    """Moving a job to invoiced needs an invoice record. Without one the
    engine correctly refuses the move, and that refusal must not be
    reported as a trigger that failed to land."""
    ctx = _trigger_ctx(
        assignments=[_asg(status="completed", on_complete="invoiced")],
        jobs=[_job(status="invoicing")],
        invoices=[],
    )
    assert _run_rule("completed_trigger_landed", ctx).status == result.PASS


def test_a_sensitive_account_never_makes_this_rule_block():
    ctx = _trigger_ctx(
        assignments=[_asg(status="completed", on_complete="quoted")],
        jobs=[_job(status="quoting", client="c_sensitive")],
        clients=[{"id": "c_sensitive", "name": "Kowhai Health NZ Ltd", "critical": True}],
    )
    assert _run_rule("completed_trigger_landed", ctx).status == result.PASS


# quoted_jobs_have_a_chase
def test_a_quote_older_than_seven_days_with_no_chase_is_reported():
    ctx = _trigger_ctx(jobs=[_job(status="quoted", changed=_ago(9))])
    res = _run_rule("quoted_jobs_have_a_chase", ctx)
    assert res.status == result.FAIL
    assert "no chase task" in "\n".join(res.evidence)


def test_a_quote_with_a_chase_task_passes_even_once_it_is_completed():
    ctx = _trigger_ctx(
        jobs=[_job(status="quoted", changed=_ago(9))],
        assignments=[_asg(record_id="a_chase", content="Chase quote",
                          status="completed",
                          auto_kind=triggers_mod.QUOTED_CHASE)],
    )
    assert _run_rule("quoted_jobs_have_a_chase", ctx).status == result.PASS


# no_orphan_chase_tasks
def test_a_chase_left_open_on_a_job_that_moved_on_blocks():
    ctx = _trigger_ctx(
        assignments=[_asg(record_id="a_chase", content="Chase quote",
                          auto_kind=triggers_mod.QUOTED_CHASE)],
        jobs=[_job(status="invoicing")],
    )
    res = _run_rule("no_orphan_chase_tasks", ctx)
    assert res.status == result.FAIL
    assert res.rule.severity == "block"


def test_a_chase_on_a_job_still_quoted_passes():
    ctx = _trigger_ctx(
        assignments=[_asg(record_id="a_chase", content="Chase quote",
                          auto_kind=triggers_mod.QUOTED_CHASE)],
        jobs=[_job(status="quoted", changed=_ago(1))],
    )
    assert _run_rule("no_orphan_chase_tasks", ctx).status == result.PASS


def test_a_task_the_operator_wrote_is_not_an_orphan_chase():
    """Only auto_kind makes a task the engine's. A chase task the operator
    wrote by hand, with the same title, is left alone."""
    ctx = _trigger_ctx(
        assignments=[_asg(record_id="a_chase", content="Chase quote")],
        jobs=[_job(status="invoicing")],
    )
    assert _run_rule("no_orphan_chase_tasks", ctx).status == result.PASS


# job_status_change_is_stamped
def test_a_job_with_no_change_date_is_reported():
    ctx = _trigger_ctx(jobs=[_job(status="quoted", changed="")])
    res = _run_rule("job_status_change_is_stamped", ctx)
    assert res.status == result.FAIL
    assert "no status_changed_at" in "\n".join(res.evidence)


def test_a_stamped_job_passes():
    ctx = _trigger_ctx(jobs=[_job(status="quoted", changed=_ago(2))])
    assert _run_rule("job_status_change_is_stamped", ctx).status == result.PASS


def test_the_eight_trigger_rules_are_all_in_the_shipped_file():
    """All eight trigger rules must be in the shipped file, or they never run."""
    ruleset = RuleSet(RULES)
    wanted = [
        "trigger_line_matches_record", "trigger_tasks_link_a_job",
        "quote_tasks_carry_a_trigger", "trigger_change_was_logged",
        "completed_trigger_landed", "quoted_jobs_have_a_chase",
        "no_orphan_chase_tasks", "job_status_change_is_stamped",
    ]
    missing = [name for name in wanted if ruleset.by_id(name) is None]
    assert not missing, missing
    blocking = [name for name in wanted if ruleset.by_id(name).blocking]
    assert sorted(blocking) == ["completed_trigger_landed", "no_orphan_chase_tasks"]
