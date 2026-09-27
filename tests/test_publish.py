"""
Tests for the dashboard publish path.

Three parts are covered:

1. The guard on POST /api/publish in the files server. Every other route
   on that server reads or writes a file inside SS Folders; this one runs
   bin/publish-dashboard, which uploads a copy of the business data to the
   internet. The server binds to 127.0.0.1 by default, and the route also
   refuses any request that does not come from this machine, in case the
   server is ever started with HOST=0.0.0.0. The test starts it that way
   and connects over the machine's own LAN address, so the request looks
   exactly like one from another computer.

2. tools/publish_log.py must never break a publish. It talks to
   PocketBase, which may be down. Every call here points at a closed port
   on purpose and must still exit 0.

3. tools/snapshot_build.py must copy every local script the dashboard page
   loads, or the hosted copy refuses to start.

No test here reaches the branch that actually starts bin/publish-dashboard:
each request is refused first, by the lock or by the address it came from.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time

import pytest

from tests.test_port_server import call, free_port, have_node, server, tree  # noqa: F401
from tools import snapshot_build

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def lan_address():
    """This machine's own non-loopback address, or None.

    First try: "connecting" a UDP socket towards an outside address sends
    nothing, it only makes the kernel pick the interface it would use, which
    is the address another machine would see. That fails with no default
    route, so a hostname lookup is the second try and the interface list
    (ifconfig on macOS, ip on Linux) the third.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            ip = s.getsockname()[0]
        if not ip.startswith("127."):
            return ip
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith("127."):
                return ip
    except OSError:
        pass
    for cmd in (["ifconfig"], ["ip", "-4", "-o", "addr"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        for found in re.findall(r"inet (?:addr:)?(\d+\.\d+\.\d+\.\d+)", out):
            if not found.startswith("127."):
                return found
    return None


def run_log_tool(*args, env=None):
    """Call tools/publish_log.py with PocketBase pointed at a closed port."""
    environment = dict(
        os.environ,
        PB_URL="http://127.0.0.1:1",
        # An env file that does not exist, so the tool cannot pick up admin
        # credentials from a .env in the repo.
        SPLATT_ENV_FILE=os.path.join(REPO, "does-not-exist.env"),
    )
    environment.pop("PB_ADMIN_EMAIL", None)
    environment.pop("PB_ADMIN_PASSWORD", None)
    if env:
        environment.update(env)
    return subprocess.run(
        [sys.executable, "-m", "tools.publish_log", *args],
        cwd=REPO, env=environment, capture_output=True, text=True, timeout=60,
    )


# --------------------------------------------------------------------------
# The route guard
# --------------------------------------------------------------------------

@pytest.mark.skipif(not have_node(), reason="node is not installed")
def test_status_is_readable_and_says_nothing_is_running(server, tree):
    status, body = call(server, "GET", "/api/publish/status")
    assert status == 200
    assert body["running"] is False
    # Reading the status is allowed from anywhere. Only starting one is not.
    assert body["can_publish"] is True


@pytest.mark.skipif(not have_node(), reason="node is not installed")
def test_a_live_lock_refuses_a_second_publish(server, tree):
    """The lock holds this test's own pid, which is certainly alive."""
    state = tree / "_state"
    state.mkdir(exist_ok=True)
    (state / "publish.lock").write_text(str(os.getpid()))

    status, body = call(server, "GET", "/api/publish/status")
    assert body["running"] is True
    assert body["pid"] == os.getpid()

    status, body = call(server, "POST", "/api/publish")
    assert status == 409
    assert "already running" in body["error"]


@pytest.mark.skipif(not have_node(), reason="node is not installed")
def test_a_dead_pid_in_the_lock_does_not_block_forever(server, tree):
    """A killed run leaves the lock behind. That must not wedge publishing.

    Pid 2**22 is above every default pid_max on Linux and macOS, so it
    cannot belong to a real process on the machine running this.
    """
    state = tree / "_state"
    state.mkdir(exist_ok=True)
    (state / "publish.lock").write_text("4194304")

    status, body = call(server, "GET", "/api/publish/status")
    assert status == 200
    assert body["running"] is False


@pytest.fixture
def lan_server(tree):
    """The files server listening on every interface (HOST=0.0.0.0).

    The default bind address is 127.0.0.1, where no other machine can reach
    the server at all. This fixture opens it up on purpose so the publish
    route's own address check can be tested the way it would be met if
    someone did run the server on a network.
    """
    ip = lan_address()
    if not ip:
        pytest.skip("this machine has no non-loopback address to test against")
    port = free_port()
    env = dict(
        os.environ, HOST="0.0.0.0", PORT=str(port), SS_FOLDERS_PATH=str(tree),
        PB_URL="http://127.0.0.1:1", SPLATT_STATE_DIR=str(tree / "_state"),
    )
    proc = subprocess.Popen(
        ["node", os.path.join(REPO, "server", "project-files-server.js")], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    remote = "http://%s:%d" % (ip, port)
    try:
        for _ in range(50):
            if proc.poll() is not None:
                pytest.fail("server exited early:\n%s" % proc.stdout.read())
            try:
                with socket.create_connection((ip, port), timeout=1):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.skip("this machine cannot connect to its own LAN address %s" % ip)
        yield remote
    finally:
        proc.kill()
        proc.wait(timeout=10)


@pytest.mark.skipif(not have_node(), reason="node is not installed")
def test_publishing_is_refused_from_another_machine(lan_server):
    """A request that did not come from this machine is refused with 403.

    The request goes to the machine's own LAN address rather than
    127.0.0.1, so to the server it looks exactly like one from another
    computer. Without this guard, anyone on the same network could start a
    publish, which uploads a copy of the business data.
    """
    status, body = call(lan_server, "GET", "/api/publish/status")
    assert status == 200
    assert body["can_publish"] is False

    status, body = call(lan_server, "POST", "/api/publish")
    assert status == 403
    assert "Publishing can only be started from" in body["error"]


# --------------------------------------------------------------------------
# The log tool, with PocketBase down
# --------------------------------------------------------------------------

def test_start_says_nothing_and_still_exits_zero_without_credentials():
    r = run_log_tool("start", "--trigger", "cron")
    assert r.returncode == 0
    assert r.stdout.strip() == ""          # no id, so finish knows to do nothing
    assert "will not be recorded" in r.stderr


def test_finish_with_an_empty_id_is_a_no_op():
    r = run_log_tool("finish", "--id", "", "--status", "success")
    assert r.returncode == 0
    assert r.stderr.strip() == ""          # it never even tried to sign in


def test_finish_survives_pocketbase_being_down(tmp_path):
    """An id it cannot use, because nothing is listening. Still exit 0."""
    out = tmp_path / "run.log"
    out.write_text("some output\n")
    r = run_log_tool(
        "finish", "--id", "abc123", "--status", "failed",
        "--error", "the upload failed", "--output-file", str(out),
        env={"PB_ADMIN_EMAIL": "x@example.com", "PB_ADMIN_PASSWORD": "not-real"},
    )
    assert r.returncode == 0
    assert "could not sign in" in r.stderr


def test_stats_reads_the_numbers_the_history_row_shows(tmp_path):
    snap = tmp_path / "snapshot.json"
    snap.write_text(json.dumps({
        "generated_at": "2026-09-10T22:00:00Z",
        "collections": {"clients": [{}, {}, {}], "quotes": [{}], "empty": []},
        "endpoints": {},
    }))
    r = run_log_tool("stats", "--snapshot", str(snap))
    assert r.returncode == 0
    stats = json.loads(r.stdout)
    assert stats["generated_at"] == "2026-09-10T22:00:00Z"
    assert stats["records"] == 4       # three clients and one quote
    assert stats["collections"] == 3   # the empty one still counts as captured


def test_stats_on_a_missing_file_fails_loudly_rather_than_printing_zeroes():
    """A row saying 0 records would look like a real, empty snapshot, so a
    missing file is an error and prints nothing on stdout."""
    r = run_log_tool("stats", "--snapshot", "/nowhere/snapshot.json")
    assert r.returncode == 1
    assert r.stdout.strip() == ""


# --------------------------------------------------------------------------
# The hosted copy carries every script the page loads
# --------------------------------------------------------------------------

def local_scripts(html):
    """Every <script src="..."> in the page that is served from the page's
    own folder: remote scripts (http:// or https://) are left out, and so
    is config.local.js, which snapshot_build writes rather than copies."""
    found = []
    for src in re.findall(r"""<script\b[^>]*\bsrc\s*=\s*["']([^"']+)["']""", html,
                          flags=re.IGNORECASE):
        if re.match(r"^(https?:)?//", src, flags=re.IGNORECASE):
            continue
        if src.split("?", 1)[0] == "config.local.js":
            continue
        found.append(src.split("?", 1)[0])
    return found


def test_every_local_script_in_the_page_is_copied_into_the_hosted_build():
    with open(os.path.join(REPO, "dashboard", "index.html"), encoding="utf-8") as fh:
        html = fh.read()
    scripts = local_scripts(html)
    assert scripts, "found no local scripts in dashboard/index.html"
    missing = [src for src in scripts if src not in snapshot_build.STATIC_FILES]
    assert not missing, (
        "dashboard/index.html loads %s but tools/snapshot_build.py does not "
        "copy it, so the hosted page would fail to start" % missing)
    for name in snapshot_build.STATIC_FILES:
        assert os.path.exists(os.path.join(REPO, "dashboard", name)), name


def test_the_script_finder_skips_remote_and_generated_scripts():
    html = (
        '<script src="https://cdn.example.com/react.js"></script>\n'
        '<script crossorigin src="//cdn.example.com/other.js"></script>\n'
        '<script src="config.local.js"></script>\n'
        "<script src='local_engine.js'></script>\n"
        '<script type="text/babel">const x = 1;</script>\n'
    )
    assert local_scripts(html) == ["local_engine.js"]
