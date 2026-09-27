"""
Static checks on dashboard/index.html: it compiles, and it keeps the rules
the rest of the dashboard depends on.

Why these tests exist
    dashboard/index.html is one large JSX file that the browser compiles at
    load time with Babel standalone. There is no build step, so a syntax
    error does not fail a build; it shows up in the browser as a blank page
    with the reason in the console. These tests catch that earlier.

What is checked
    1. Every text/babel block compiles with Babel. A failure reports the
       real line number in index.html.
    2. Content assertions on the source text:
       - money is read only from project_financials, never from the unused
         `bills` and `invoices` collections;
       - the task date helpers (real due date, parked, deadline) are present,
         and no overdue check reads the raw Todoist due_date;
       - every Todoist column has a filter;
       - the map places pins only from stored coordinates and uses a tile
         source that needs no API key and no Referer;
       - quality_engine.js is loaded and guarded.

The compile test skips, rather than fails, when Node or @babel/standalone is
not installed (`npm install` in the repo provides it).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.join(REPO, "dashboard", "index.html")

BABEL_BLOCK = re.compile(
    r'<script type="text/babel"[^>]*>(.*?)</script>', re.DOTALL
)


def read_dashboard() -> str:
    with open(DASHBOARD, encoding="utf-8") as fh:
        return fh.read()


def babel_cwd():
    """Where to run Node from so it can find @babel/standalone.

    Normally that is the repo, because `npm install` puts node_modules at
    the top of it. The temp dir is checked as a fallback so a globally
    installed copy still counts. Returns None when neither works, which is
    what makes the compile test skip instead of fail on a machine that has
    never run npm install.
    """
    if shutil.which("node") is None:
        return None
    for candidate in (REPO, tempfile.gettempdir()):
        probe = subprocess.run(
            ["node", "-e", "require.resolve('@babel/standalone')"],
            cwd=candidate, capture_output=True, text=True,
        )
        if probe.returncode == 0:
            return candidate
    return None


BABEL_CWD = babel_cwd()


def test_dashboard_file_exists():
    assert os.path.exists(DASHBOARD), "dashboard/index.html is missing"


def test_at_least_one_babel_block():
    """The regex finds at least one block, so the compile test below cannot
    pass by checking nothing."""
    blocks = BABEL_BLOCK.findall(read_dashboard())
    assert blocks, "found no <script type=\"text/babel\"> block to check"


@pytest.mark.skipif(
    BABEL_CWD is None,
    reason="@babel/standalone is not installed, run `npm install` in the repo",
)
def test_every_babel_block_compiles():
    html = read_dashboard()
    script = r"""
const fs = require('fs');
const Babel = require('@babel/standalone');
const html = fs.readFileSync(process.argv[1], 'utf8');
const re = /<script type="text\/babel"[^>]*>([\s\S]*?)<\/script>/g;
const failures = [];
let m;
while ((m = re.exec(html))) {
  const startLine = html.slice(0, m.index).split('\n').length;
  try {
    Babel.transform(m[1], { presets: ['react'] });
  } catch (e) {
    const loc = /\((\d+):(\d+)\)/.exec(e.message);
    failures.push({
      message: e.message,
      line: loc ? startLine + Number(loc[1]) - 1 : null,
    });
  }
}
console.log(JSON.stringify(failures));
"""
    result = subprocess.run(
        ["node", "-e", script, DASHBOARD],
        cwd=BABEL_CWD, capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stderr
    payload = [ln for ln in result.stdout.splitlines() if ln.startswith("[")]
    failures = json.loads(payload[-1]) if payload else []
    if failures:
        detail = "\n".join(
            "index.html line %s: %s" % (f["line"], f["message"]) for f in failures
        )
        pytest.fail("dashboard/index.html does not compile\n\n" + detail)


def test_dashboard_does_not_read_the_dead_money_tables():
    html = read_dashboard()
    offenders = []
    for pattern, why in (
        (r"data\.bills\b", "reads the empty `bills` collection"),
        (r"data\.invoices\b", "reads the empty `invoices` collection"),
        (r"/collections/bills/records", "writes to the empty `bills` collection"),
        (r"/collections/invoices/records", "writes to the empty `invoices` collection"),
    ):
        for match in re.finditer(pattern, html):
            line = html.count("\n", 0, match.start()) + 1
            offenders.append("line %d %s" % (line, why))
    assert not offenders, (
        "project_financials is the only money table. These reintroduce the "
        "$0 double up:\n  " + "\n  ".join(offenders)
    )


def test_dashboard_has_the_real_due_helpers():
    """The dashboard knows that a task's date is not always due_date.

    Tasks in the Stalled column have their Todoist due date cleared on
    purpose, so long-dead work stops turning up in Today, Upcoming and every
    overdue filter. Their real date is pinned in the description instead.
    Code that read only task.due_date would move those late tasks out of
    Overdue and into "No date". These four helpers read the real date.
    """
    html = read_dashboard()
    for helper in (
        "const realDueOf",
        "const sectionKind",
        "const isParked",
        "const isTaskOverdueLive",
    ):
        assert helper in html, "%s is gone, stalled tasks will read as undated" % helper


def test_the_two_dates_are_still_two_separate_helpers():
    """A task carries two dates and they answer different questions.

    The pin says how late the work is. The Todoist date says when to touch
    it next, which for a task in Waiting is a chase date in the future while
    the pin may be months in the past. actionableDateOf is the schedule,
    daysLateOf (through realDueOf) is the lateness, and both must exist.
    """
    html = read_dashboard()
    assert "const actionableDateOf" in html, (
        "scheduling has no date of its own again, chase dates will vanish"
    )
    assert "const daysLateOf" in html, "nothing works out how late a task is"


def test_the_deadline_is_read_all_the_way_through():
    """Todoist's deadline is a third date, and it is read, filterable and
    carried onto each row (deadlineOf, the 'deadline' key, _deadline)."""
    html = read_dashboard()
    assert "const deadlineOf" in html, "the deadline is not read at all"
    assert "'deadline'" in html, "the deadline has no filter or sort"
    assert "_deadline" in html, "the deadline never reaches a row"


def test_no_task_overdue_check_reads_the_raw_due_date():
    """No code compares a raw task due_date against today.

    A stalled task has no Todoist due date, so that comparison is wrong for
    every stalled task. isTaskOverdueLive is the one overdue check.
    """
    html = read_dashboard()
    pattern = re.compile(r"t\.due_date\s*&&\s*new Date\(t\.due_date\)\s*<\s*new Date\(\)")
    offenders = [
        "line %d" % (html.count("\n", 0, m.start()) + 1)
        for m in pattern.finditer(html)
    ]
    assert not offenders, (
        "use isTaskOverdueLive(t) instead, a stalled task has no Todoist "
        "due date:\n  " + "\n  ".join(offenders)
    )


def test_the_task_pills_cover_every_todoist_column():
    """Every Todoist column on the board has a filter key, including Waiting
    on Client, Waiting on Supplier, Stalled and Backlog."""
    html = read_dashboard()
    for key in (
        "'waiting_client'",
        "'waiting_supplier'",
        "'stalled'",
        "'backlog'",
    ):
        assert key in html, "the %s column has no filter" % key


def test_the_map_never_guesses_a_location_from_a_place_name():
    """A pin comes from stored coordinates, never from matching place names.

    A pin guessed from a town name in the address looks exactly like a real
    one but can be tens of kilometres out. A job with no coordinates belongs
    in the unmapped list, where it can be fixed. NZ_CITIES and
    resolveJobLocation were the place-name lookup and must not exist.
    """
    html = read_dashboard()
    for gone in ("NZ_CITIES", "resolveJobLocation"):
        assert gone not in html, (
            "%s is back, the map is guessing pins from place names again" % gone
        )


def test_the_map_tiles_need_no_api_key():
    """The map tiles come from a service that needs no account or API key.

    Keyed tile services stamp a watermark or refuse the tiles without a key.
    The dark look is a CSS filter (.map-tiles), so no styled tile service is
    needed for it.

    The tile server must also not require a Referer. The page is opened as
    file://, where the browser sends none, and OpenStreetMap's own tile
    servers answer such requests with a 403 "Access blocked" tile. OSM
    France serves the same style, keyless, without that rule.
    """
    html = read_dashboard()
    for keyed in ("cartocdn", "mapbox", "maptiler", "stadiamaps", "thunderforest",
                  "api_key=", "access_token="):
        assert keyed not in html.lower(), (
            "%s wants an account, the map will show a watermark" % keyed
        )
    assert "tile.openstreetmap.org" not in html, (
        "tile.openstreetmap.org is back, it blocks file:// pages"
    )
    assert "tile.openstreetmap.fr/osmfr" in html, "the map has no tile source"
    assert ".map-tiles" in html, "the dark map filter is gone"


def test_the_hosted_copy_sends_osm_a_referer():
    """The hosted copy does not strip the Referer from tile requests.

    OpenStreetMap's tile policy wants a valid Referer from browser apps and
    answers requests without one with a 403 "Access blocked" tile. The
    _headers file written by tools/snapshot_build.py therefore uses
    Referrer-Policy: strict-origin-when-cross-origin, which sends the origin
    and nothing else, and never no-referrer.
    """
    build = open(os.path.join(REPO, "tools", "snapshot_build.py"), encoding="utf-8").read()
    assert "Referrer-Policy: no-referrer" not in build, (
        "no-referrer is back, OSM will block every tile on the hosted copy"
    )
    assert "Referrer-Policy: strict-origin-when-cross-origin" in build, (
        "the hosted copy has no referrer policy, OSM needs the origin"
    )


def test_dashboard_loads_the_quality_engine():
    """The flags on the Workload page come from quality_engine.js, so the
    page loads it and refuses to start without window.SplattQuality."""
    html = read_dashboard()
    assert 'src="quality_engine.js"' in html, "quality_engine.js is not loaded"
    assert "window.SplattQuality" in html, "the fail loud guard is gone"
    assert os.path.exists(os.path.join(REPO, "dashboard", "quality_engine.js"))
