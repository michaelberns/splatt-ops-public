"""
Tests that every Todoist caller in the repo uses the current API (v1).

What is checked
    1. Each known caller uses `api.todoist.com/api/v1` and contains no
       `/rest/vN` URL. The retired REST v2 endpoints answer 410 Gone, a
       status most code does not check for, so a leftover v2 call can
       fail without anyone noticing (in the browser it only shows in the
       developer console).
    2. No other file in the repo calls Todoist without being on the list
       of known callers, so a new caller is always covered.
    3. The files server reads completion the v1 way. v1 has no
       `is_completed`; a task is completed when `checked` is true (with
       `completed_at` set). Reading `is_completed` from a v1 response gives
       `undefined`, which would make every task look open.
    4. The dashboard names the API host once and reports Todoist failures
       (including 410 and 401) to the user.

These are text checks over the source files; nothing calls Todoist.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# Every file that talks to Todoist. If another caller appears and is not
# listed, test_no_caller_is_missing_from_this_list fails and names it.
CALLERS = [
    "core/todoist.py",
    "tools/skill_harness.py",
    "tools/todoist_setup.py",
    "server/project-files-server.js",
    "dashboard/index.html",
]

# Files that contain the Todoist host without calling it.
# dashboard/snapshot_shim.js wraps window.fetch on the hosted read-only
# copy of the dashboard and blocks any request addressed to Todoist, so it
# contains the host string only in order to refuse it.
NOT_CALLERS = {"dashboard/snapshot_shim.js"}

SEARCHED = ("*.py", "*.js", "*.html")
# Skipped directories:
#   tests  this file contains the host string in order to search for it,
#          and nothing under tests calls the real API.
#   dist   the hosted dashboard build written by tools/snapshot_build.py
#          (gitignored). Its files are copies of files already scanned,
#          and scanning them would make the result depend on whether a
#          build has been run.
SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__",
             "backups", "logs", "dist", "tests"}


def source_files():
    for pattern in SEARCHED:
        for path in REPO.rglob(pattern):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            yield path


def read(rel):
    return (REPO / rel).read_text(encoding="utf-8")


def code_only(text):
    """The same text with whole-line comments removed.

    The code checks below search this instead of the raw file, so a
    comment that mentions `task.checked` cannot make a test pass when the
    code no longer uses it.
    """
    return re.sub(r"^\s*(#|//|\*|/\*).*$", "", text, flags=re.M)


@pytest.mark.parametrize("rel", CALLERS)
def test_the_caller_is_on_v1(rel):
    text = read(rel)
    assert "api.todoist.com/api/v1" in text, (
        "%s calls Todoist but not on /api/v1" % rel)


@pytest.mark.parametrize("rel", CALLERS)
def test_the_caller_has_no_retired_endpoint_left(rel):
    """Not a single retired `/rest/vN` URL may remain in a caller; one
    missed call is enough to break that feature."""
    text = read(rel)
    stale = re.findall(r"api\.todoist\.com/rest/v\d+", text)
    assert not stale, "%s still calls a retired endpoint: %s" % (rel, stale)


def test_nothing_anywhere_calls_the_retired_endpoint():
    offenders = []
    for path in source_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        if re.search(r"api\.todoist\.com/rest/v\d+", text):
            offenders.append(str(path.relative_to(REPO)))
    offenders = [o for o in offenders if o not in NOT_CALLERS]
    assert not offenders, offenders


def test_no_caller_is_missing_from_this_list():
    """The set of files containing the Todoist host equals CALLERS (minus
    NOT_CALLERS), so a new caller cannot escape the checks above."""
    found = set()
    for path in source_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        rel = str(path.relative_to(REPO))
        if "api.todoist.com" in text and rel not in NOT_CALLERS:
            found.add(rel)
    assert found == set(CALLERS), (
        "callers changed\n  expected: %s\n  found:    %s"
        % (sorted(CALLERS), sorted(found)))


def test_the_files_server_reads_completion_the_v1_way():
    """The files server works out completion from `task.checked` and
    `task.completed_at`, the v1 fields, not from `is_completed`."""
    code = code_only(read("server/project-files-server.js"))
    assert re.search(r"task\.checked", code), (
        "the files server never reads `task.checked`, so it cannot tell "
        "whether a v1 task is completed")
    assert re.search(r"task\.completed_at", code), (
        "completion is not worked out from `checked` and `completed_at`")


def test_the_files_server_says_something_useful_when_the_api_moves_again():
    """The files server handles a 410 explicitly, so a future API
    retirement is reported by name rather than as a generic failure."""
    code = code_only(read("server/project-files-server.js"))
    assert re.search(r"status\s*===\s*410", code), (
        "a retired endpoint would report as a bare failure")


def test_the_dashboard_names_the_api_in_one_place():
    """The dashboard defines the API base once (TODOIST_API), so a future
    version change is a one-line edit that cannot miss a call."""
    text = read("dashboard/index.html")
    hosts = re.findall(r"api\.todoist\.com", text)
    assert len(hosts) == 1, (
        "the dashboard repeats the Todoist host %d times, so the next "
        "version bump can miss one" % len(hosts))
    assert re.search(r"TODOIST_API\s*=", text), (
        "the dashboard has no single constant for the API base")


def test_the_dashboard_reports_a_todoist_failure_instead_of_hiding_it():
    """The dashboard has a shared `todoistProblem` path that explains a
    failed Todoist call (including 410 and 401) to the user instead of
    only logging it to the browser console."""
    text = read("dashboard/index.html")
    assert "todoistProblem" in text, (
        "the dashboard has no shared way to explain a Todoist failure")
    for status in ("410", "401"):
        assert status in text, (
            "the dashboard cannot explain a %s from Todoist" % status)
