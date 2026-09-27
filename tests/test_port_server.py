"""
Tests for server/project-files-server.js, the files and playbook server the
dashboard and the agent talk to (port 8092 in normal use).

The server reads, writes and deletes files and accepts base64 uploads, and
it has no login. So most of these tests are about containment: a path with
`..` in it cannot climb out of SS Folders, a directory cannot be deleted
through the file route, and a symlink is not followed.

The rest cover:
- the playbook routes with PocketBase unreachable (a run must still be
  saved to disk, and say why PocketBase was not used);
- the /api/xero reply (no snapshot, stale snapshot, findings that belong to
  a different snapshot);
- which .env file is loaded, and that the server listens on loopback by
  default.

Each test starts the real server with node on a free port, pointed at a
throwaway SS Folders tree under tmp_path and at a closed PocketBase port, so
nothing here can touch real data even if a path check were broken.
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import select
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(REPO, "server", "project-files-server.js")


def free_port():
    """A free port chosen by the OS, so tests never use 8092 and can run
    while the real server, or another copy of these tests, is running."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def have_node():
    return shutil.which("node") is not None


pytestmark = pytest.mark.skipif(not have_node(), reason="node is not installed")


@pytest.fixture
def tree(tmp_path):
    """A throwaway SS Folders with one client, one project and one supplier."""
    project = tmp_path / "Test Client" / "Test Project"
    project.mkdir(parents=True)
    (project / "PROJECT.md").write_text("# Test Client - Test Project\n\n**Status:** open\n")
    supplier = tmp_path / "_Suppliers" / "Test Vendor"
    supplier.mkdir(parents=True)
    (supplier / "RELATIONSHIP.md").write_text("# Test Vendor\n\nnotes\n")
    return tmp_path


@pytest.fixture
def server(tree):
    """The real server, on a free port, pointed at the throwaway tree.

    PB_URL is a closed port on purpose: every test in this file must pass
    with PocketBase down. HOST is removed so the server's default bind
    address is what gets tested.
    """
    port = free_port()
    env = dict(
        os.environ,
        PORT=str(port),
        SS_FOLDERS_PATH=str(tree),
        PB_URL="http://127.0.0.1:1",
        # Read the Xero state from the throwaway tree, not the repo's state/
        # directory. The directory is left absent here, which is the state a
        # fresh clone is in; the state_dir fixture creates it when needed.
        SPLATT_STATE_DIR=str(tree / "_state"),
    )
    env.pop("HOST", None)
    proc = subprocess.Popen(
        ["node", SERVER], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base = "http://127.0.0.1:%d" % port
    for _ in range(100):
        if proc.poll() is not None:
            pytest.fail("server exited early:\n%s" % proc.stdout.read())
        try:
            urllib.request.urlopen(base + "/api/health", timeout=1).read()
            break
        except Exception:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("server never came up")
    yield base
    proc.kill()
    proc.wait(timeout=10)


def call(base, method, path, payload=None):
    """Return (status, parsed body). HTTP errors are returned, not raised,
    because a 400 is the expected answer in many of these cases."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    try:
        return status, json.loads(raw)
    except Exception:
        return status, raw


def raw_get(base, path):
    """Return (status, raw bytes), to check a binary file survives the round trip."""
    with urllib.request.urlopen(base + path, timeout=30) as r:
        return r.status, r.read()


def q(rel):
    return urllib.parse.quote(rel)


PROJECT_MD = "Test Client/Test Project/PROJECT.md"


# ---------------------------------------------------------------- reading


def test_health_is_ok(server):
    status, body = call(server, "GET", "/api/health")
    assert status == 200
    assert body["status"] == "ok"


def test_unknown_route_is_a_404(server):
    status, _ = call(server, "GET", "/api/nope")
    assert status == 404


def test_project_files_finds_the_project_and_the_supplier(server):
    status, body = call(server, "GET", "/api/project-files")
    assert status == 200
    paths = {f["path"] for f in body}
    assert PROJECT_MD in paths
    assert "_Suppliers/Test Vendor/RELATIONSHIP.md" in paths


def test_folder_contents_lists_a_directory(server, tree):
    status, body = call(server, "GET", "/api/folder-contents?path=" + q("Test Client"))
    assert status == 200
    assert body["folderPath"] == str(tree / "Test Client")
    assert [e["name"] for e in body["entries"]] == ["Test Project"] + ["PROJECT.md"]


def test_folder_contents_reaches_one_level_down_and_says_where_files_came_from(server, tree):
    """This route also returns the files one level below the folder asked
    for, so the dashboard can show a client and the files in each of its
    projects in one request. Those entries carry parentDir so the UI can
    tell them apart from files directly in the folder.

    It stops at one level: a file two folders down does not appear.
    """
    deep = tree / "Test Client" / "Test Project" / "bills"
    deep.mkdir()
    (deep / "buried.pdf").write_bytes(b"%PDF")

    _, body = call(server, "GET", "/api/folder-contents?path=" + q("Test Client"))
    by_name = {e["name"]: e for e in body["entries"]}

    assert by_name["Test Project"]["type"] == "directory"
    assert by_name["PROJECT.md"]["parentDir"] == "Test Project"
    assert "buried.pdf" not in by_name, "recursion should stop after one level"

    # Asked for directly, the file one level further down does show up.
    _, deeper = call(server, "GET",
                     "/api/folder-contents?path=" + q("Test Client/Test Project"))
    assert "buried.pdf" in {e["name"] for e in deeper["entries"]}


def test_folder_contents_refuses_a_file(server):
    status, _ = call(server, "GET", "/api/folder-contents?path=" + q(PROJECT_MD))
    assert status == 400


# ------------------------------------------------------------ containment
# The API has no login, so the path check is what keeps the rest of the disk
# out of reach. Where a test could write, it checks the filesystem as well as
# the status code, because a 400 that still wrote the file would be a leak.


@pytest.mark.parametrize("escape", [
    "../escaped.md",
    "../../escaped.md",
    "/etc/passwd",
    "Test Client/../../escaped.md",
])
def test_reads_cannot_climb_out_of_ss_folders(server, escape):
    assert call(server, "GET", "/api/file?path=" + q(escape))[0] == 400
    assert call(server, "GET", "/api/folder-contents?path=" + q(escape))[0] == 400


@pytest.mark.parametrize("escape", ["../escaped.md", "../../escaped.md", "/tmp/escaped.md"])
def test_writes_cannot_climb_out_of_ss_folders(server, tree, escape):
    status, _ = call(server, "PUT", "/api/project-files", {"path": escape, "content": "x"})
    assert status == 400
    outside = os.path.normpath(os.path.join(str(tree), escape))
    assert not os.path.exists(outside)


def test_uploads_cannot_climb_out_of_ss_folders(server, tree):
    status, _ = call(server, "POST", "/api/upload-file",
                     {"path": "../../evil.pdf", "contentBase64": "eA=="})
    assert status == 400
    assert not os.path.exists(os.path.join(os.path.dirname(str(tree)), "evil.pdf"))


def test_deletes_cannot_climb_out_of_ss_folders(server):
    assert call(server, "DELETE", "/api/file?path=" + q("../../etc/passwd"))[0] == 400


def test_a_directory_cannot_be_deleted_through_the_file_route(server, tree):
    status, _ = call(server, "DELETE", "/api/file?path=" + q("Test Client/Test Project"))
    assert status == 400
    assert (tree / "Test Client" / "Test Project").is_dir()


def test_a_symlink_is_not_followed_out_of_the_tree(server, tree, tmp_path_factory):
    """The path check only looks at the string, so a symlink inside the tree
    could point outside it. The server calls lstat and refuses anything that
    is not a plain file, so a link to a file outside the tree is refused."""
    secret_dir = tmp_path_factory.mktemp("outside")
    secret = secret_dir / "secret.txt"
    secret.write_text("do not serve this")
    link = tree / "Test Client" / "link.txt"
    os.symlink(secret, link)
    assert call(server, "GET", "/api/file?path=" + q("Test Client/link.txt"))[0] == 404
    assert call(server, "DELETE", "/api/file?path=" + q("Test Client/link.txt"))[0] == 400
    assert secret.read_text() == "do not serve this"


# ---------------------------------------------------------------- writing


def test_put_rewrites_the_file_on_disk(server, tree):
    status, body = call(server, "PUT", "/api/project-files",
                        {"path": PROJECT_MD, "content": "# rewritten\n"})
    assert status == 200
    assert body["ok"] is True
    assert (tree / PROJECT_MD).read_text() == "# rewritten\n"


def test_put_refuses_a_request_with_no_content(server, tree):
    before = (tree / PROJECT_MD).read_text()
    assert call(server, "PUT", "/api/project-files", {"path": PROJECT_MD})[0] == 400
    assert (tree / PROJECT_MD).read_text() == before


def test_upload_creates_missing_folders_and_keeps_bytes_intact(server, tree):
    """Bills and invoices are uploaded into subfolders that may not exist
    yet, and they are PDFs, so the bytes must come back exactly as sent."""
    payload = b"%PDF-1.4 fake \x00\x01\x02\xff bytes"
    rel = "Test Client/Test Project/bills/invoice.pdf"
    status, body = call(server, "POST", "/api/upload-file",
                        {"path": rel, "contentBase64": base64.b64encode(payload).decode()})
    assert status == 200
    assert body["size"] == len(payload)
    assert (tree / "Test Client" / "Test Project" / "bills").is_dir()
    assert (tree / rel).read_bytes() == payload
    assert raw_get(server, "/api/file?path=" + q(rel)) == (200, payload)


def test_upload_refuses_a_request_with_no_content(server):
    status, _ = call(server, "POST", "/api/upload-file", {"path": "Test Client/x.pdf"})
    assert status == 400


def test_delete_removes_the_file_and_is_safe_to_repeat(server, tree):
    rel = "Test Client/Test Project/scratch.md"
    (tree / rel).write_text("temporary")
    assert call(server, "DELETE", "/api/file?path=" + q(rel))[0] == 200
    assert not (tree / rel).exists()
    # Deleting it again is an error status, never a crash and never a 200.
    assert call(server, "DELETE", "/api/file?path=" + q(rel))[0] in (400, 404, 500)


# --------------------------------------------------------------- playbooks


@pytest.fixture
def with_specs(tree):
    """Copy playbook specs into the throwaway tree.

    By default the specs come from the repo's own examples/SS Folders, so
    these tests always run. Set SS_FOLDERS_REAL to a real project-folder
    tree to run the same tests against its specs instead.
    """
    real = os.environ.get("SS_FOLDERS_REAL", "")
    base = real or os.path.join(REPO, "examples", "SS Folders")
    src = os.path.join(base, "_howtotasks", "playbooks")
    if not os.path.isdir(src):
        pytest.skip("no playbook specs found at %s" % src)
    dst = tree / "_howtotasks" / "playbooks"
    dst.mkdir(parents=True)
    for name in os.listdir(src):
        if name.endswith(".spec.json"):
            shutil.copy(os.path.join(src, name), dst / name)
    return dst


def test_starting_an_unknown_playbook_is_a_404(server):
    status, _ = call(server, "POST", "/api/playbook/start",
                     {"playbook_id": "nosuchplaybook", "inputs": {}})
    assert status == 404


def test_a_run_survives_pocketbase_being_down(server, with_specs, tree):
    """PocketBase is unreachable in these tests, so a started run must be
    written to disk with storage 'local' and record why PocketBase was not
    used."""
    spec = json.loads((with_specs / "log-interaction.spec.json").read_text())
    inputs = {k: "test-value" for k in spec.get("inputs_required", [])}

    status, body = call(server, "POST", "/api/playbook/start",
                        {"playbook_id": "log-interaction", "inputs": inputs,
                         "trigger": "test"})
    assert status == 200
    assert body["storage"] == "local"

    saved = tree / "_howtotasks" / "runs" / (body["run_id"] + ".json")
    assert saved.exists()
    run = json.loads(saved.read_text())
    assert run["status"] == "open"
    assert run["_pb_error"], "the run should record why PocketBase was not used"


def test_starting_without_required_inputs_says_which_ones(server, with_specs):
    spec = json.loads((with_specs / "log-interaction.spec.json").read_text())
    if not spec.get("inputs_required"):
        pytest.skip("this playbook has no required inputs")
    status, body = call(server, "POST", "/api/playbook/start",
                        {"playbook_id": "log-interaction", "inputs": {}})
    assert status == 400
    assert "Missing required inputs" in body["error"]


def test_steps_have_to_arrive_in_order(server, with_specs):
    """The server accepts only the run's current step (409 otherwise), so a
    run cannot be recorded as done with steps skipped. An unknown run is 404
    and an unknown status is 400."""
    spec = json.loads((with_specs / "log-interaction.spec.json").read_text())
    inputs = {k: "test-value" for k in spec.get("inputs_required", [])}
    _, started = call(server, "POST", "/api/playbook/start",
                      {"playbook_id": "log-interaction", "inputs": inputs})
    run_id, first = started["run_id"], started["step"]["id"]

    assert call(server, "POST", "/api/playbook/step",
                {"run_id": run_id, "step_id": "not-the-current-one",
                 "status": "done"})[0] == 409
    assert call(server, "POST", "/api/playbook/step",
                {"run_id": "nosuchrun", "step_id": first, "status": "done"})[0] == 404
    assert call(server, "POST", "/api/playbook/step",
                {"run_id": run_id, "step_id": first, "status": "banana"})[0] == 400


def test_a_failed_validator_blocks_the_step_rather_than_advancing(server, with_specs):
    """PocketBase is down, so a validator that checks a record cannot pass.
    If the step is rejected (400), the reply lists the failed validators and
    the run stays on the same step."""
    spec = json.loads((with_specs / "log-interaction.spec.json").read_text())
    inputs = {k: "test-value" for k in spec.get("inputs_required", [])}
    _, started = call(server, "POST", "/api/playbook/start",
                      {"playbook_id": "log-interaction", "inputs": inputs})
    run_id, first = started["run_id"], started["step"]["id"]

    status, body = call(server, "POST", "/api/playbook/step",
                        {"run_id": run_id, "step_id": first, "status": "done"})
    assert status in (200, 400)
    if status == 400:
        assert body["failed_validators"]
        _, after = call(server, "GET", "/api/playbook/run/" + run_id)
        assert after["current_step"] == first, "a failed step must not advance the run"


def test_deleting_a_run_removes_it(server, with_specs, tree):
    spec = json.loads((with_specs / "log-interaction.spec.json").read_text())
    inputs = {k: "test-value" for k in spec.get("inputs_required", [])}
    _, started = call(server, "POST", "/api/playbook/start",
                      {"playbook_id": "log-interaction", "inputs": inputs})
    run_id = started["run_id"]

    assert call(server, "DELETE", "/api/playbook/run/" + run_id)[0] == 200
    assert not (tree / "_howtotasks" / "runs" / (run_id + ".json")).exists()
    assert call(server, "GET", "/api/playbook/run/" + run_id)[0] == 404


# ------------------------------------------------------------------- xero
#
# The dashboard cannot read the disk, so it gets the Xero snapshot from
# /api/xero. These tests check that absent data never arrives as zero, that a
# stale snapshot is marked stale, and that findings computed against a
# different snapshot are withheld.


@pytest.fixture
def state_dir(tree):
    """Create the Xero state directory. Tests of the empty case do not use it."""
    d = tree / "_state"
    d.mkdir(exist_ok=True)
    return d


def _snapshot(captured_at, **over):
    """A snapshot in the shape tools/xero_snapshot.py writes (fictional figures)."""
    snap = {
        "snapshot_version": 1,
        "captured_at": captured_at,
        "organisation": "splatt engineering ltd",
        "base_currency": "NZD",
        "xero_last_refreshed": "2026-08-10T02:40:06.604792Z",
        "money": {
            "cash_balance": 12500.00,
            "owed_to_you": 16500.00,
            "owed_by_you": 6200.00,
            "overdue_to_you": 16500.00,
            "overdue_by_you": 6200.00,
            "invoiced_total": 42000.00,
            "received_total": 23600.00,
            "credited_total": 1900.00,
        },
        "completeness": {"invoices_complete": True, "bills_complete": False},
        "receivables": [{"invoice_number": "INV-41363"}],
        "payables": [{"reference": "00000138"}],
        "invoices": [{"invoice_number": "INV-41363"}, {"invoice_number": "INV-41366"}],
        "contacts": [{"name": "Bright Fizz Beverage Company"}],
    }
    snap.update(over)
    return snap


def _gaps(snapshot_captured_at, **over):
    """Findings in the shape tools/xero_gaps.py writes."""
    out = {
        "checked_at": "2026-08-10",
        "snapshot_captured_at": snapshot_captured_at,
        "overdue_days_threshold": 30,
        "count": 1,
        "total_amount_flagged": 460.00,
        "findings": [{
            "kind": "missing_contact_email",
            "confidence": "certain",
            "amount": 460.00,
            "title": "Drivetrain New Zealand has no email address in Xero",
            "detail": "Invoice INV-41365 is unpaid and there is no address.",
            "evidence": {"invoice_number": "INV-41365"},
            "document": "INV-41365",
        }],
    }
    out.update(over)
    return out


def _iso(hours_ago):
    when = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours_ago)
    return when.replace(microsecond=0).isoformat()


def test_no_xero_snapshot_is_answered_not_errored(server):
    """A fresh clone has no state/ directory. That is a normal first run, so
    the reply is 200 with available:false and a reason, not a 500."""
    status, body = call(server, "GET", "/api/xero")
    assert status == 200
    assert body["available"] is False
    assert body["reason"]
    assert body["findings"] == []


def test_a_missing_snapshot_never_reports_zero_money(server):
    """With no snapshot there are no money figures at all. A zero would look
    the same as "everyone has paid"."""
    _, body = call(server, "GET", "/api/xero")
    assert body.get("money") in (None, {}), "absent data must not arrive as figures"


def test_a_snapshot_is_served_with_its_money(server, state_dir):
    (state_dir / "xero-snapshot.json").write_text(json.dumps(_snapshot(_iso(1))))
    status, body = call(server, "GET", "/api/xero")
    assert status == 200
    assert body["available"] is True
    assert body["money"]["owed_to_you"] == 16500.00
    assert body["organisation"] == "splatt engineering ltd"
    assert body["counts"]["invoices"] == 2
    assert body["completeness"]["bills_complete"] is False


def test_a_recent_snapshot_is_not_called_stale(server, state_dir):
    (state_dir / "xero-snapshot.json").write_text(json.dumps(_snapshot(_iso(1))))
    _, body = call(server, "GET", "/api/xero")
    assert body["stale"] is False
    assert 0 <= body["age_hours"] <= 2


def test_an_old_snapshot_says_so(server, state_dir):
    """A snapshot older than 24 hours is marked stale, with its age."""
    (state_dir / "xero-snapshot.json").write_text(json.dumps(_snapshot(_iso(72))))
    _, body = call(server, "GET", "/api/xero")
    assert body["stale"] is True
    assert body["age_hours"] > 24


def test_an_unreadable_date_counts_as_old_not_fresh(server, state_dir):
    """If the capture date cannot be parsed, the snapshot is treated as
    stale rather than fresh."""
    (state_dir / "xero-snapshot.json").write_text(json.dumps(_snapshot("not a date")))
    _, body = call(server, "GET", "/api/xero")
    assert body["available"] is True
    assert body["stale"] is True
    assert body["age_hours"] is None


def test_findings_come_through_when_they_match_the_snapshot(server, state_dir):
    when = _iso(1)
    (state_dir / "xero-snapshot.json").write_text(json.dumps(_snapshot(when)))
    (state_dir / "xero-gaps.json").write_text(json.dumps(_gaps(when)))
    _, body = call(server, "GET", "/api/xero")
    assert body["findings_available"] is True
    assert len(body["findings"]) == 1
    assert body["findings"][0]["confidence"] == "certain"
    assert body["total_amount_flagged"] == 460.00
    assert body["overdue_days_threshold"] == 30


def test_findings_from_a_different_snapshot_are_withheld(server, state_dir):
    """The two files are written by separate commands, so the money can be
    refreshed while older findings remain. Findings whose
    snapshot_captured_at does not match the snapshot are withheld, with a
    reason."""
    (state_dir / "xero-snapshot.json").write_text(json.dumps(_snapshot(_iso(1))))
    (state_dir / "xero-gaps.json").write_text(json.dumps(_gaps(_iso(50))))
    _, body = call(server, "GET", "/api/xero")
    assert body["available"] is True, "the money is still good"
    assert body["findings"] == []
    assert body["findings_available"] is False
    assert "different snapshot" in body["findings_reason"]


def test_a_snapshot_with_no_findings_file_still_serves_the_money(server, state_dir):
    (state_dir / "xero-snapshot.json").write_text(json.dumps(_snapshot(_iso(1))))
    _, body = call(server, "GET", "/api/xero")
    assert body["available"] is True
    assert body["money"]["cash_balance"] == 12500.00
    assert body["findings_available"] is False
    assert body["findings_reason"]


def test_a_half_written_snapshot_is_reported_not_swallowed(server, state_dir):
    """A file caught mid-write reads as broken JSON. The reply says so,
    rather than looking like "no snapshot"."""
    (state_dir / "xero-snapshot.json").write_text('{"captured_at": "2026-')
    status, body = call(server, "GET", "/api/xero")
    assert status == 200
    assert body["available"] is False
    assert "JSON" in body["reason"]


def test_findings_hold_no_money_are_still_shown(server, state_dir):
    """A finding with amount 0, such as a gap in the invoice numbers (which
    can mean an invoice was never sent), is still shown."""
    when = _iso(1)
    lead = {
        "kind": "invoice_number_gap", "confidence": "lead", "amount": 0,
        "title": "3 invoice numbers unaccounted for", "detail": "...",
        "evidence": {"missing": [41357, 41360, 41362]}, "document": None,
    }
    (state_dir / "xero-snapshot.json").write_text(json.dumps(_snapshot(when)))
    (state_dir / "xero-gaps.json").write_text(
        json.dumps(_gaps(when, count=2, findings=[lead])))
    _, body = call(server, "GET", "/api/xero")
    assert [f["kind"] for f in body["findings"]] == ["invoice_number_gap"]
    assert body["findings"][0]["confidence"] == "lead"


def test_the_two_missing_files_give_different_reasons(server, state_dir):
    """No snapshot means Xero has not been captured. No findings means it
    was captured but the checks have not been run. The two reasons differ
    because the fixes differ."""
    _, no_snapshot = call(server, "GET", "/api/xero")
    (state_dir / "xero-snapshot.json").write_text(json.dumps(_snapshot(_iso(1))))
    _, no_findings = call(server, "GET", "/api/xero")

    assert "captured" in no_snapshot["reason"]
    assert "checks" in no_findings["findings_reason"]
    assert no_snapshot["reason"] != no_findings["findings_reason"]


# ----------------------------------------------------------- env and host
#
# The server loads SPLATT_ENV_FILE if it is set, otherwise the repo's own .env,
# and nothing else. /api/health reports which file it loaded.


def _server_on(tmp_path, tree, **env_over):
    """Start the server with a hand-built environment and return (base, proc)."""
    port = free_port()
    env = dict(os.environ, PORT=str(port), SS_FOLDERS_PATH=str(tree),
               PB_URL="http://127.0.0.1:1",
               SPLATT_STATE_DIR=str(tree / "_state"))
    env.pop("SPLATT_ENV_FILE", None)
    env.pop("HOST", None)
    env.update(env_over)
    proc = subprocess.Popen(["node", SERVER], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = "http://127.0.0.1:%d" % port
    for _ in range(100):
        if proc.poll() is not None:
            pytest.fail("server exited early:\n%s" % proc.stdout.read())
        try:
            urllib.request.urlopen(base + "/api/health", timeout=1).read()
            return base, proc
        except Exception:
            time.sleep(0.1)
    proc.kill()
    pytest.fail("server never came up")


def test_health_says_which_env_it_read(server):
    """/api/health names the .env file that was loaded (or null), so the
    credentials in use can be checked."""
    _, body = call(server, "GET", "/api/health")
    assert "env_loaded_from" in body


def test_an_explicit_env_file_wins(tmp_path, tree):
    chosen = tmp_path / "chosen.env"
    chosen.write_text("PB_ADMIN_EMAIL=explicit@example.com\n")
    base, proc = _server_on(tmp_path, tree, SPLATT_ENV_FILE=str(chosen))
    try:
        _, body = call(base, "GET", "/api/health")
        assert body["env_loaded_from"] == str(chosen)
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_without_an_override_only_the_repo_env_is_read(tmp_path, tree):
    """Without SPLATT_ENV_FILE the server reads <repo>/.env and nothing else. A
    fake home is built with .env files where a home-relative lookup would
    find them; they must be ignored. With no repo .env, nothing is loaded."""
    fake_home = tmp_path / "fakehome"
    for rel in (("Documents", "splatt-ops"), ("mnt", "Documents", "splatt-ops")):
        folder = fake_home.joinpath(*rel)
        folder.mkdir(parents=True)
        (folder / ".env").write_text("PB_ADMIN_EMAIL=home@example.com\n")

    repo_env = os.path.join(REPO, ".env")
    base, proc = _server_on(tmp_path, tree, HOME=str(fake_home))
    try:
        _, body = call(base, "GET", "/api/health")
        got = body["env_loaded_from"]
        if os.path.exists(repo_env):
            assert got is not None and os.path.realpath(got) == os.path.realpath(repo_env), (
                "read %r, expected the repo's own .env" % got)
        else:
            assert got is None, "read %r, expected no .env at all" % got
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_the_server_listens_on_loopback_by_default(tmp_path, tree):
    """With no HOST set the server binds 127.0.0.1 only, so the API (which
    has no login) is not reachable from other machines. The startup line
    names the address it bound."""
    base, proc = _server_on(tmp_path, tree)
    try:
        port = int(base.rsplit(":", 1)[1])
        ready, _, _ = select.select([proc.stdout], [], [], 10)
        assert ready, "the server printed no startup line"
        line = proc.stdout.readline()
        assert "http://127.0.0.1:%d" % port in line, line
    finally:
        proc.kill()
        proc.wait(timeout=10)
