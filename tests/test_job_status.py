"""
Tests that the job status list in core/job_status.py matches every other
copy of it.

The same eleven statuses exist in three places besides the Python module:
the generated schema (core/schema.py), the dashboard (`JOB_STATUS_META` in
dashboard/index.html) and the live database. If any copy differs, a job
can be saved on a status the dashboard cannot display, or the dashboard
can offer a status the database refuses.

All tests except the last run anywhere, reading only committed files. The
last one talks to a live PocketBase and skips, with the reason shown, when
no server or credentials are configured (see
`tests.conftest_splatt.live_pocketbase`).
"""

from __future__ import annotations

import io
import os
import re

import pytest

from core import job_status, schema
from tests.conftest_splatt import live_pocketbase

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(REPO, "dashboard", "index.html")


def meta_keys():
    """The JOB_STATUS_META keys, in the order dashboard/index.html lists them.

    Parsed from the file rather than copied into the test, so the test
    cannot drift from the dashboard.
    """
    html = io.open(INDEX, encoding="utf-8").read()
    start = html.index("const JOB_STATUS_META = {")
    end = html.index("\n    };", start)
    block = html[start:end]
    return re.findall(r"^\s{6}([a-z_]+):\s*\{", block, re.M)


# the list itself

def test_there_are_eleven_and_they_are_unique():
    assert len(job_status.JOB_STATUSES) == 11
    assert len(set(job_status.JOB_STATUSES)) == 11


def test_the_dead_values_are_gone():
    """`paid` is recorded on the paid flag, not as a status, and `active`
    carries no stage information, so neither is a valid status."""
    assert "paid" not in job_status.JOB_STATUSES
    assert "active" not in job_status.JOB_STATUSES


def test_the_groups_cover_every_status_exactly_once():
    """Every status is in exactly one group.

    A status in no group would match none of the dashboard filters and
    vanish from the board; a status in two groups would be counted twice
    in the revenue forecast.
    """
    grouped = (job_status.OPEN + job_status.CONFIRMED + job_status.TERMINAL
               + job_status.PARKED + job_status.REENGAGE)
    assert sorted(grouped) == sorted(job_status.JOB_STATUSES)
    assert len(grouped) == len(set(grouped)), "a status is in two groups"


def test_pipeline_is_open_plus_confirmed():
    assert job_status.PIPELINE == job_status.OPEN + job_status.CONFIRMED


def test_the_default_is_a_real_status():
    assert job_status.is_valid(job_status.DEFAULT)


def test_check_names_the_allowed_values_when_it_refuses():
    """The error for a bad status names the bad value and lists all eleven
    valid ones, so the caller can fix it from the message alone."""
    with pytest.raises(ValueError) as exc:
        job_status.check("paid")
    message = str(exc.value)
    assert "paid" in message
    for status in job_status.JOB_STATUSES:
        assert status in message


# against the committed schema and the committed dashboard

def test_the_database_select_matches_the_code():
    """The `jobs.status` select in core/schema.py has the same values in
    the same order.

    core/schema.py is generated from the database by
    tools/introspect_schema.py, so a failure here means the database
    select was changed without updating core/job_status.py (or the other
    way round).
    """
    values = schema.SCHEMA["jobs"]["fields"]["status"]["values"]
    assert list(values) == list(job_status.JOB_STATUSES)


def test_the_dashboard_offers_the_same_eleven_in_the_same_order():
    """JOB_STATUS_META, the dashboard's copy of the list, has the same keys
    in the same order, so the status legend, the status picker and every
    badge match what the back end enforces."""
    assert meta_keys() == list(job_status.JOB_STATUSES)


# against the live server

def test_no_job_is_on_a_status_that_does_not_exist():
    """The live `jobs.status` select matches the code, and no stored job
    holds a value outside it (for example one left behind by a partial
    data migration)."""
    pb = live_pocketbase()
    jobs = pb.list_all("jobs") or []
    live_values = None
    for coll in pb.collections():
        if coll.get("name") != "jobs":
            continue
        for field in coll.get("fields") or []:
            if field.get("name") == "status":
                live_values = list(field.get("values") or [])
    pb.close()

    assert live_values == list(job_status.JOB_STATUSES), (
        "the live select is %s" % live_values)

    stray = sorted({(j.get("status") or "") for j in jobs} - set(job_status.JOB_STATUSES) - {""})
    assert not stray, "jobs are still on %s" % ", ".join(stray)
