"""
Run the JavaScript test files as part of the Python suite.

`pytest` is the one command that runs every test in the repo. Several
modules are JavaScript because the dashboard runs them in the browser and
the files server runs them in Node: server/financials_engine.js and the
dashboard engines (filter_engine.js, quality_engine.js, streamer_engine.js,
snapshot_shim.js). Their tests live in tests/test_*.js.

How it works
    Every tests/test_*.js file becomes one pytest case that runs
    `node --test <file>` from the repo root. A failure includes Node's own
    output, not just the exit code. A second test fails if a .js file in
    tests/ is not named test_*.js, because it would never be run.

The suite skips when Node is not installed, so the Python tests still run on
a machine without it.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(REPO, "tests")


def js_test_files():
    return sorted(
        f for f in os.listdir(TESTS)
        if f.startswith("test_") and f.endswith(".js")
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("name", js_test_files())
def test_javascript_suite_passes(name):
    result = subprocess.run(
        ["node", "--test", os.path.join(TESTS, name)],
        cwd=REPO, capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        pytest.fail(
            "node --test %s failed\n\n%s\n%s" % (name, result.stdout, result.stderr)
        )


def test_every_javascript_test_file_is_picked_up():
    """Every .js file in tests/ is named test_*.js, so the runner above
    picks it up."""
    stray = [
        f for f in os.listdir(TESTS)
        if f.endswith(".js") and not f.startswith("test_")
    ]
    assert not stray, (
        "these .js files in tests/ will never run, rename them to test_*.js: %s" % stray
    )
